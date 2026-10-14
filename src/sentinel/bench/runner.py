"""Run recorded footage through the production pipeline and score it.

Offline mode decodes each clip on a virtual clock at the camera's configured
fps, so results are reproducible and long recordings run faster than real
time. Each frame goes through the same stages as `pipeline.CameraWorker`:
privacy mask -> motion gate -> detector -> filter -> tracker -> runtime/engine
-> incidents -> alert policy.

Where box annotations exist, a reference pass feeds the annotated people (with
their annotated identities) through the same zones and rules. Its events are
what Sentinel *should* report given perfect perception; comparing the two
isolates errors added by detection and tracking. Hand-written expected events
in the manifest are matched the same way."""

from __future__ import annotations

import copy
import json
import math
import os
import platform
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import load_config, redact_url, sensitivity_adjustments
from ..detectors import create_detector, detection_thresholds, resolve_detector_settings
from ..engine import ZoneMap
from ..models import Notification, Track, effective_categories
from ..notify import CallbackNotifier
from ..pipeline import filter_detections, with_category_classes
from ..health import HEALTH_EVENT_TYPES
from ..runtime import SentinelRuntime
from ..tracker import IoUTracker, TrackerConfig
from ..vision import Frame, MotionGate, appearance_vector, apply_privacy_masks
from . import degrade as degrade_module
from .labels import GroundTruth, load_labels
from .metrics import DetectionStats, ExpectedEvent, TrackingStats, count_group_boxes, match_boxes, match_events, percentiles

HEALTH_EVENTS = HEALTH_EVENT_TYPES  # coverage events are not security events
EVENT_WINDOWS = {
    "activity": (-3.0, 8.0),
    "zone_entry": (-3.0, 8.0),
    "repeated_presence": (-5.0, 10.0),
    "dwell": (-6.0, 12.0),
    "sequence": (-6.0, 15.0),
    "multi_camera": (-6.0, 15.0),
}
NOTIFICATION_WINDOW_S = 15.0


def load_manifest(path: str | Path) -> dict[str, Any]:
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    manifest["_path"] = str(Path(path))
    manifest["_clips"] = {clip["id"]: clip for clip in manifest["clips"]}
    return manifest


def session_config(manifest: dict[str, Any], clip_ids: list[str], detector_overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    config = copy.deepcopy(load_config(manifest["site_config"], resolve_secrets=False))
    config["detector"] = config.get("detector", {}) | (detector_overrides or {})
    cameras = {manifest["_clips"][clip_id]["camera_id"] for clip_id in clip_ids}
    config["cameras"] = [camera for camera in config["cameras"] if camera["id"] in cameras]
    links = config.get("topology", {}).get("links", [])
    config.setdefault("topology", {})["links"] = [link for link in links if link["from"] in cameras and link["to"] in cameras]
    for channel in config.get("alerts", {}).get("channels", []):
        channel["enabled"] = False  # benchmarks never push to phones
    config.setdefault("drone", {})["enabled"] = False
    return config


class ResourceSampler(threading.Thread):
    """Samples this process's CPU and resident memory (psutil, optional)."""

    def __init__(self, interval_s: float = 0.5) -> None:
        super().__init__(daemon=True, name="bench-resources")
        self.interval_s = interval_s
        self.cpu: list[float] = []
        self.rss_mb: list[float] = []
        self._stop = threading.Event()
        try:
            import psutil

            self._process = psutil.Process(os.getpid())
            self._process.cpu_percent(None)
            self.cpu_count = psutil.cpu_count(logical=True) or 1
        except ImportError:
            self._process = None
            self.cpu_count = os.cpu_count() or 1
        self._cpu_time_start = time.process_time()
        self._wall_start = time.perf_counter()

    def run(self) -> None:
        while not self._stop.wait(self.interval_s):
            if self._process is not None:
                self.cpu.append(self._process.cpu_percent(None))
                self.rss_mb.append(self._process.memory_info().rss / 1e6)

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        self.join(timeout=2)
        wall = time.perf_counter() - self._wall_start
        cpu_time = time.process_time() - self._cpu_time_start
        return {
            "logical_cpus": self.cpu_count,
            "process_cpu_percent_of_one_core": round(100 * cpu_time / wall, 1) if wall else None,
            "process_cpu_percent_of_machine": round(100 * cpu_time / wall / self.cpu_count, 1) if wall else None,
            "cpu_samples": percentiles(self.cpu),
            "rss_mb_peak": round(max(self.rss_mb), 1) if self.rss_mb else None,
            "rss_mb_mean": round(sum(self.rss_mb) / len(self.rss_mb), 1) if self.rss_mb else None,
            "psutil": self._process is not None,
        }


class GroundTruthFeeder:
    """Turns annotated boxes into confirmed tracks with annotated identities:
    the 'perfect perception' input for the reference pass."""

    def __init__(self, camera_id: str, max_missed_ms: int) -> None:
        self.camera_id = camera_id
        self.max_missed_ms = max_missed_ms
        self.tracks: dict[str, Track] = {}

    def update(self, boxes, timestamp_ms: int) -> tuple[list[Track], list[Track]]:
        seen = set()
        for box in boxes:
            if box.label != "person" or not box.real_person:
                continue
            seen.add(box.track_id)
            track = self.tracks.get(box.track_id)
            x, y, w, h = box.bbox
            anchor = (x + w / 2, y + h)
            if track is None:
                track = Track(f"{self.camera_id}:{box.track_id}", self.camera_id, "person", box.bbox, 1.0, timestamp_ms, timestamp_ms, confirmed=True, first_anchor=anchor)
                self.tracks[box.track_id] = track
            track.bbox = box.bbox
            track.last_seen_ms = timestamp_ms
            track.hits += 1
            track.max_displacement = max(track.max_displacement, math.hypot(anchor[0] - track.first_anchor[0], anchor[1] - track.first_anchor[1]))
        ended = []
        for gt_id in list(self.tracks):
            if gt_id not in seen and timestamp_ms - self.tracks[gt_id].last_seen_ms > self.max_missed_ms:
                ended.append(self.tracks.pop(gt_id))
        active = [self.tracks[gt_id] for gt_id in seen if gt_id in self.tracks]
        return active, ended

    def flush(self) -> list[Track]:
        ended = list(self.tracks.values())
        self.tracks.clear()
        return ended


@dataclass
class _Clip:
    spec: dict[str, Any]
    camera: dict[str, Any]
    capture: Any
    src_fps: float
    width: int
    height: int
    frame_count: int
    gt: GroundTruth | None
    tracker: IoUTracker
    detector: Any
    gate: MotionGate
    log: Any
    position: int = -1
    done: bool = False
    detection: DetectionStats = field(default_factory=DetectionStats)
    tracking: TrackingStats = field(default_factory=TrackingStats)
    timings: dict[str, list[float]] = field(default_factory=lambda: {"decode": [], "detect": [], "track": [], "engine": [], "total": []})
    frames_processed: int = 0
    detector_runs: int = 0
    wall_s: float = 0.0
    video_s: float = 0.0
    feeder: GroundTruthFeeder | None = None
    report_threshold: float = 0.0  # boxes below this only feed the tracker; they are not reported detections
    detector_settings: dict[str, Any] = field(default_factory=dict)


def run_offline(
    manifest: dict[str, Any],
    clip_ids: list[str],
    out_dir: str | Path,
    *,
    degradation: str | None = None,
    max_seconds: float | None = None,
    reference: bool = True,
    motion_gate: bool = True,
    session_id: str = "session",
    detector_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    config = session_config(manifest, clip_ids, detector_overrides)
    (out / "site_config.json").write_text(json.dumps(safe_config(config), indent=2), encoding="utf-8")
    start = datetime.fromisoformat(manifest.get("start_time", "2026-10-13T14:00:00"))
    start_ms = int(start.timestamp() * 1000)
    arm_mode = manifest.get("arm_mode", "away")

    predicted: list[dict[str, Any]] = []
    notifications: list[Notification] = []
    runtime = SentinelRuntime(config, storage_dir=out / "store", notifiers=[CallbackNotifier("bench", notifications.append)])
    runtime.update_settings({"arm_mode": arm_mode}, "bench")
    runtime.started_ms = start_ms
    runtime.bus.subscribe(lambda topic, payload: predicted.append(payload) if topic == "event" else None)
    reference_events: list[dict[str, Any]] = []
    reference_notifications: list[Notification] = []
    reference_runtime = None
    if reference:
        reference_runtime = SentinelRuntime(config, storage_dir=out / "reference_store", notifiers=[CallbackNotifier("ref", reference_notifications.append)])
        reference_runtime.update_settings({"arm_mode": arm_mode}, "bench")
        reference_runtime.started_ms = start_ms
        reference_runtime.bus.subscribe(lambda topic, payload: reference_events.append(payload) if topic == "event" else None)

    clips = [_open_clip(manifest["_clips"][clip_id], config, out, runtime) for clip_id in clip_ids]
    zones = ZoneMap(config)
    detector_defaults = config.get("detector", {})
    min_area = float(detector_defaults.get("min_box_area", 0.0004))
    idle_ms = int(detector_defaults.get("idle_detect_interval_s", 1.0) * 1000)
    max_missed = int(config.get("tracker", {}).get("max_missed_ms", 2500))
    for clip in clips:
        if clip.gt is not None and reference_runtime is not None:
            clip.feeder = GroundTruthFeeder(clip.camera["id"], max_missed)

    fps = min(float(clip.camera.get("fps", 5)) for clip in clips)
    step_ms = int(round(1000 / fps))
    sampler = ResourceSampler()
    sampler.start()
    wall_start = time.perf_counter()
    offset_ms = 0
    last_detect: dict[str, int] = {}
    active_tracks: dict[str, list[Track]] = {}
    try:
        while not all(clip.done for clip in clips):
            if max_seconds is not None and offset_ms / 1000 > max_seconds:
                break
            now_ms = start_ms + offset_ms
            t_s = offset_ms / 1000
            for clip in clips:
                if clip.done:
                    continue
                frame_started = time.perf_counter()
                target = int(round(t_s * clip.src_fps))
                image = _seek(clip, target)
                decode_ms = (time.perf_counter() - frame_started) * 1000
                camera_id = clip.camera["id"]
                if image is None:
                    clip.done = True
                    ended = clip.tracker.flush()
                    runtime.process_camera_update(camera_id, now_ms, [], ended)
                    runtime.camera_status.pop(camera_id, None)  # end of recording is not a camera outage
                    if clip.feeder is not None:
                        reference_runtime.process_camera_update(camera_id, now_ms, [], clip.feeder.flush())
                        reference_runtime.camera_status.pop(camera_id, None)
                    continue
                runtime.update_camera_status(camera_id, connected=True, last_frame_ms=now_ms)
                if reference_runtime is not None:
                    reference_runtime.update_camera_status(camera_id, connected=True, last_frame_ms=now_ms)
                degrade_started = time.perf_counter()
                image = degrade_module.apply(image, degradation, target)
                degrade_ms = (time.perf_counter() - degrade_started) * 1000
                image = apply_privacy_masks(image, clip.camera.get("privacy_masks", []))
                active = active_tracks.get(camera_id, [])
                run_detector = (not motion_gate) or clip.gate.changed(image) or bool(active) or now_ms - last_detect.get(camera_id, -10**9) >= idle_ms
                detections = []
                detect_ms = track_ms = engine_ms = 0.0
                if run_detector:
                    last_detect[camera_id] = now_ms
                    started = time.perf_counter()
                    raw = clip.detector.detect(Frame(image, now_ms))
                    detect_ms = (time.perf_counter() - started) * 1000
                    detections = filter_detections(raw, camera_id, zones, min_area, enabled_categories(config, clip.camera))
                    started = time.perf_counter()
                    active, ended = clip.tracker.update(detections, now_ms)
                    for track in active:
                        if track.appearance is None or track.hits % 10 == 0:
                            track.appearance = appearance_vector(image, track.bbox)
                    track_ms = (time.perf_counter() - started) * 1000
                    started = time.perf_counter()
                    runtime.process_camera_update(camera_id, now_ms, active, ended)
                    engine_ms = (time.perf_counter() - started) * 1000
                    active_tracks[camera_id] = active
                    clip.detector_runs += 1

                gt_index = int(round(t_s * clip.gt.fps)) if clip.gt is not None else -1  # labels may be at a different rate than the video
                if clip.feeder is not None and clip.gt is not None:
                    gt_active, gt_ended = clip.feeder.update(clip.gt.at_frame(gt_index), now_ms)
                    reference_runtime.process_camera_update(camera_id, now_ms, gt_active, gt_ended)

                if clip.gt is not None and run_detector:
                    persons = [det.bbox for det in detections if det.label == "person" and det.confidence >= clip.report_threshold]
                    gt_boxes = [box for box in clip.gt.at_frame(gt_index) if box.label == "person"]
                    matches, unmatched, missed = match_boxes(persons, gt_boxes)
                    clip.detection.add(matches, unmatched, missed, count_group_boxes(persons, unmatched, gt_boxes))
                    track_boxes = [track for track in active if track.label == "person"]
                    track_matches, _, _ = match_boxes([track.bbox for track in track_boxes], gt_boxes)
                    clip.tracking.add([(track_boxes[i].track_id, gt) for i, gt in track_matches], [b for b in gt_boxes if not b.ignore])

                total_ms = (time.perf_counter() - frame_started) * 1000 - degrade_ms  # synthetic stress is not pipeline cost
                for key, value in (("decode", decode_ms), ("total", total_ms)):
                    clip.timings[key].append(value)
                if run_detector:
                    clip.timings["detect"].append(detect_ms)
                    clip.timings["track"].append(track_ms)
                    clip.timings["engine"].append(engine_ms)
                clip.frames_processed += 1
                clip.wall_s += total_ms / 1000
                clip.video_s = target / clip.src_fps
                site_ids = runtime.engine.correlator.track_to_site
                clip.log.write(
                    json.dumps(
                        {
                            "i": target,
                            "t": round(t_s, 3),
                            "ts": now_ms,
                            "detected": run_detector,
                            "dets": [[det.label, round(det.confidence, 3), *[round(v, 4) for v in det.bbox]] for det in detections],
                            "tracks": [[track.track_id, site_ids.get(track.track_id, ""), track.label, *[round(v, 4) for v in track.bbox]] for track in active],
                            "ms": {"decode": round(decode_ms, 1), "detect": round(detect_ms, 1), "track": round(track_ms, 2), "engine": round(engine_ms, 2)},
                        }
                    )
                    + "\n"
                )
            if offset_ms % 1000 < step_ms:
                runtime.housekeeping(now_ms)
                if reference_runtime is not None:
                    reference_runtime.housekeeping(now_ms)
            offset_ms += step_ms
    finally:
        wall_s = time.perf_counter() - wall_start
        resources = sampler.stop()
        for clip in clips:
            clip.capture.release()
            clip.detector.close()
            clip.log.close()
        runtime.dispatcher.stop()
        if reference_runtime is not None:
            reference_runtime.dispatcher.stop()

    result = _summarise(manifest, clips, config, predicted, notifications, reference_events, reference_notifications, start_ms, degradation, reference_runtime is not None)
    result.update(
        {
            "session_id": session_id,
            "mode": "offline",
            "session_clips": clip_ids,
            "bench_fps": fps,
            "degradation": degradation,
            "arm_mode": arm_mode,
            "wall_s": round(wall_s, 2),
            "resources": resources,
            "host": _host(),
            "motion_gate": motion_gate,
            "detector": {key: (clips[0].detector_settings if clips else config["detector"]).get(key) for key in ("type", "model", "score_threshold", "low_score_threshold", "input_size", "keep_aspect", "providers", "delegate", "tiles")}
            | {"runtime": getattr(clips[0].detector, "describe", lambda: {})() if clips else {}},
        }
    )
    runtime.close()
    if reference_runtime is not None:
        reference_runtime.close()
    (out / "result.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return result


def enabled_categories(config: dict[str, Any], camera: dict[str, Any]) -> set[str]:
    return {name for name, values in effective_categories(config, camera).items() if values.get("detect")}


def safe_config(config: dict[str, Any]) -> dict[str, Any]:
    """Config copy for run artifacts: no private keys, no credentials in source URLs."""
    safe = {key: copy.deepcopy(value) for key, value in config.items() if not key.startswith("_")}
    for camera in safe.get("cameras", []):
        source = camera.get("source", {})
        if "url" in source:
            source["url"] = redact_url(str(source["url"]))
    return safe


def _open_clip(spec: dict[str, Any], config: dict[str, Any], out: Path, runtime: SentinelRuntime) -> _Clip:
    import cv2

    camera = next(camera for camera in config["cameras"] if camera["id"] == spec["camera_id"])
    capture = cv2.VideoCapture(spec["video"])
    if not capture.isOpened():
        raise FileNotFoundError(f"cannot open {spec['video']}; run scripts/fetch_bench_footage.py")
    src_fps = float(spec.get("fps") or capture.get(cv2.CAP_PROP_FPS) or 25)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    gt = load_labels(spec.get("labels"), width, height, src_fps)
    settings = with_category_classes(
        resolve_detector_settings(config.get("detector", {}) | camera.get("detector", {})), effective_categories(config, camera)
    )
    _, offset = sensitivity_adjustments(runtime.settings["sensitivity"])
    detector_threshold, report_threshold = detection_thresholds(settings, offset)
    detector = create_detector(settings, detector_threshold)
    clip_dir = out / "clips" / spec["id"]
    clip_dir.mkdir(parents=True, exist_ok=True)
    return _Clip(
        spec=spec,
        camera=camera,
        capture=capture,
        src_fps=src_fps,
        width=width,
        height=height,
        frame_count=frame_count,
        gt=gt,
        tracker=IoUTracker(camera["id"], TrackerConfig.from_dict((config.get("tracker") or {}) | {"high_score": report_threshold})),
        report_threshold=report_threshold,
        detector_settings=settings,
        detector=detector,
        gate=MotionGate(float(settings.get("motion_min_fraction", 0.002))),
        log=(clip_dir / "frames.jsonl").open("w", encoding="utf-8"),
    )


def _seek(clip: _Clip, target: int):
    """Sequential decode up to `target` (grab() skips colour conversion)."""
    if clip.frame_count and target >= clip.frame_count:
        return None
    while clip.position < target - 1:
        if not clip.capture.grab():
            return None
        clip.position += 1
    ok, image = clip.capture.read()
    if not ok:
        return None
    clip.position += 1
    return image


def _expected_from(clip_spec: dict[str, Any], camera_id: str, reference_events: list[dict[str, Any]], start_ms: int) -> list[ExpectedEvent]:
    expected = [
        ExpectedEvent(
            event_type=item["event_type"],
            time_s=float(item["time_s"]),
            camera_id=camera_id,
            zone_id=item.get("zone_id", ""),
            window_s=tuple(item.get("window_s", EVENT_WINDOWS.get(item["event_type"], (-3.0, 8.0)))),
            source="manual",
            note=item.get("note", ""),
        )
        for item in clip_spec.get("expected_events", [])
    ]
    if clip_spec.get("derive_expected_events", True):
        for event in reference_events:
            if event["camera_id"] != camera_id or event["event_type"] in HEALTH_EVENTS:
                continue
            expected.append(
                ExpectedEvent(
                    event_type=event["event_type"],
                    time_s=(event["timestamp_ms"] - start_ms) / 1000,
                    camera_id=camera_id,
                    zone_id=event.get("zone_id", ""),
                    window_s=EVENT_WINDOWS.get(event["event_type"], (-3.0, 8.0)),
                    source="annotation_reference",
                    note=event["summary"],
                )
            )
    return expected


def _summarise(
    manifest,
    clips: list[_Clip],
    config,
    predicted,
    notifications: list[Notification],
    reference_events,
    reference_notifications: list[Notification],
    start_ms: int,
    degradation: str | None,
    has_reference: bool,
) -> dict[str, Any]:
    clip_results = []
    for clip in clips:
        camera_id = clip.camera["id"]
        spec = clip.spec
        clip_predicted = [event for event in predicted if event["camera_id"] == camera_id and event["event_type"] not in HEALTH_EVENTS]
        labeled_events = bool(spec.get("expected_events")) or (clip.gt is not None and has_reference and spec.get("derive_expected_events", True))
        expected = _expected_from(spec, camera_id, reference_events, start_ms) if labeled_events else []
        event_match = match_events(clip_predicted, expected, start_ms) if labeled_events else None
        clip_notifications = [n.as_dict() for n in notifications if camera_id in n.camera_ids]
        ref_notifications = [n.as_dict() for n in reference_notifications if camera_id in n.camera_ids]
        notification_match = _match_notifications(clip_notifications, ref_notifications) if labeled_events else None
        processed_video_s = clip.video_s
        conditions = list(spec.get("conditions", [])) + ([f"synthetic_{degradation}"] if degradation else [])
        clip_dir_events = {
            "predicted": clip_predicted,
            "expected": [exp.__dict__ for exp in expected],
            "matches": event_match,
            "notifications": clip_notifications,
            "reference_notifications": ref_notifications,
        }
        clip_path = Path(clip.log.name).parent
        (clip_path / "events.json").write_text(json.dumps(clip_dir_events, indent=2, default=str), encoding="utf-8")
        clip_results.append(
            {
                "clip_id": spec["id"],
                "camera_id": camera_id,
                "video": spec["video"],
                "labels": spec.get("labels"),
                "degradation": degradation,
                "source_fps": clip.src_fps,
                "resolution": [clip.width, clip.height],
                "conditions": conditions,
                "label_source": (clip.gt.notes if clip.gt else None) or spec.get("label_source"),
                "labeled_boxes": clip.gt is not None,
                "labeled_events": labeled_events,
                "expect_no_notifications": bool(spec.get("expect_no_notifications", False)),
                "frames_processed": clip.frames_processed,
                "detector_runs": clip.detector_runs,
                "video_seconds": round(processed_video_s, 2),
                "processing_wall_s": round(clip.wall_s, 2),
                "throughput_fps": round(clip.frames_processed / clip.wall_s, 2) if clip.wall_s else None,
                "timings_ms": {key: percentiles(values) for key, values in clip.timings.items()},
                "detection": clip.detection.summary() if clip.gt is not None else None,
                "tracking": clip.tracking.summary() if clip.gt is not None else None,
                "events": event_match["per_type"] if event_match else None,
                "event_counts": _count_types(clip_predicted),
                "notifications": len(clip_notifications),
                "notification_match": notification_match,
                "false_alerts_per_hour": _false_alert_rate(spec, clip_notifications, notification_match, processed_video_s),
            }
        )
    return {"clips": clip_results, "generated_at": datetime.now().isoformat(timespec="seconds")}


def _match_notifications(predicted: list[dict], reference: list[dict]) -> dict[str, Any]:
    used = set()
    matched = 0
    for pred in predicted:
        for index, ref in enumerate(reference):
            if index not in used and abs(pred["timestamp_ms"] - ref["timestamp_ms"]) / 1000 <= NOTIFICATION_WINDOW_S:
                used.add(index)
                matched += 1
                break
    return {
        "expected": len(reference),
        "sent": len(predicted),
        "matched": matched,
        "false": len(predicted) - matched,
        "missed": len(reference) - matched,
    }


def _false_alert_rate(spec, notifications, match, video_s: float) -> float | None:
    if video_s <= 0:
        return None
    if spec.get("expect_no_notifications"):
        return round(len(notifications) * 3600 / video_s, 1)
    if match is not None:
        return round(match["false"] * 3600 / video_s, 1)
    return None


def _count_types(events: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        counts[event["event_type"]] = counts.get(event["event_type"], 0) + 1
    return counts


def _host() -> dict[str, Any]:
    import cv2

    return {
        "platform": platform.platform(),
        "processor": platform.processor(),
        "python": platform.python_version(),
        "opencv": cv2.__version__,
        "logical_cpus": os.cpu_count(),
    }
