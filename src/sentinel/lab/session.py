"""Test Lab runs: one recorded clip through Sentinel's real stack (edge: OpenCV).

Per analysed frame the same stages run as in `pipeline.CameraWorker` and
scripts/guardian/video_eval.py: privacy masks -> detector (the real ONNX
models) -> filter -> IoUTracker -> SentinelRuntime (temporal engine, rules,
correlator, Guardian, incidents, alert policy) -> species classifier. Nothing
is drawn from labels or scripted: labels are only compared afterwards.

Differences from a live camera, all on purpose and shown in the lab:
  * the clip is decoded on a virtual clock at the chosen fps, so a slow
    computer gives the same result, only later;
  * the detector runs on every analysed frame (no motion gate), so what you
    see is what the model sees;
  * the species worker is waited for after each frame (bounded), so replays
    are repeatable;
  * alerts are simulated: the alert policy decides as it would live, and the
    resulting notifications are recorded here, never sent.

A run directory holds frames/NNNNN.jpg, frames.jsonl (one record per frame),
run.json (settings, progress, summary, events, alerts) and corrections.jsonl."""

from __future__ import annotations

import copy
import json
import logging
import queue
import re
import shutil
import threading
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from . import scenes
from .catalog import BENCH_SITE, ROOT, LabClip, all_clips, find_clip
from .profiles import detector_config, list_profiles

log = logging.getLogger(__name__)

CAMERA = "lab"
MAX_FPS = 10.0
MAX_SECONDS = 600.0
FRAME_WIDTH = 960
SPECIES_WAIT_S = 8.0
CLOCK = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
CORRECTION_KINDS = {"false_positive", "missed", "wrong_label", "correct", "event_correct", "event_false_alarm", "note"}
TARGET_CLASSES = {"person": {"person"}, "vehicle": {"vehicle"}, "animal": {"animal"}, "none": set()}

def _round_box(box) -> list[float]:
    return [round(float(v), 4) for v in box]


def _in_ranges(t: float, ranges: list[list[float]]) -> bool:
    return any(start <= t <= end for start, end in ranges)


def _pct(values: list[float], q: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q / 100 * len(ordered)))], 1)


def resolve_scene(storage_dir: Path, clip: LabClip) -> dict[str, Any] | None:
    """The clip's saved scene, else its preset's default; None keeps the bench
    zones and rules for MOT16/CAVIAR clips nobody has drawn zones for."""
    saved = scenes.load(storage_dir, clip.id)
    if saved is not None:
        return saved | {"source": "saved"}
    if clip.bench_camera:
        return None
    return scenes.preset_scene(scenes.default_preset(clip)) | {"source": "preset"}


def lab_site_config(clip: LabClip, detector: dict[str, Any], fps: float, species: bool, storage: Path,
                    scene: dict[str, Any] | None = None) -> dict[str, Any]:
    """A one-camera site for the clip, built only from the Lab scene (or the bench
    zones and rules for bench footage without one); the site config and live
    cameras are never read or changed. Guardian is on; recognition stays off."""
    camera: dict[str, Any] = {"id": CAMERA, "name": clip.title, "fps": fps, "source": {"type": "file", "path": "unused"}}
    rules: list[dict[str, Any]] = []
    protected: list[str] = []
    if scene is not None:
        camera["zones"], rules, protected = scenes.site_parts(scene)
    elif clip.bench_camera:
        from ..config import load_config

        bench = load_config(str(ROOT / BENCH_SITE), resolve_secrets=False)
        source = next((c for c in bench["cameras"] if c["id"] == clip.bench_camera), None)
        if source is not None:
            camera["zones"] = copy.deepcopy(source.get("zones", []))
            camera["privacy_masks"] = copy.deepcopy(source.get("privacy_masks", []))
            rules = copy.deepcopy(bench.get("rules", []))
            protected = [zone["id"] for zone in camera["zones"] if zone.get("type") in {"entrance", "restricted"}]
    if not camera.get("zones"):
        camera["zones"], rules, protected = scenes.site_parts(scenes.preset_scene(scenes.default_preset(clip)))
    species_dir = ROOT / "models/speciesnet/v4.0.3a"
    return {
        "site": {"id": "test_lab", "name": "Sentinel Test Lab"},
        "cameras": [camera],
        "topology": {"links": []},
        "rules": rules,
        "detector": detector,
        "categories": {"animal": {"detect": True, "alerts": True}},
        "guardian": {"enabled": True, "protected_zones": protected,
                     "species_classifier": {"enabled": bool(species and species_dir.exists()), "model_dir": str(species_dir)}},
        "alerts": {"channels": []},
        "privacy": {"store_clips": False},
        "storage": {"dir": str(storage)},
        "drone": {"enabled": False},
    }


class LabRun:
    def __init__(self, run_dir: Path, meta: dict[str, Any]) -> None:
        self.dir = run_dir
        self.meta = meta
        self.frames: list[dict[str, Any]] | None = None  # loaded lazily for finished runs
        self.cancel = threading.Event()
        self._lock = threading.Lock()

    @property
    def id(self) -> str:
        return self.meta["id"]

    def save(self) -> None:
        tmp = self.dir / "run.json.tmp"
        tmp.write_text(json.dumps(self.meta, indent=1, default=str), encoding="utf-8")
        tmp.replace(self.dir / "run.json")

    def frame_records(self, start: int = 0, limit: int = 100000) -> list[dict[str, Any]]:
        with self._lock:
            if self.frames is None:
                path = self.dir / "frames.jsonl"
                self.frames = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []
            return self.frames[start:start + limit]

    def append_frame(self, record: dict[str, Any]) -> None:
        with self._lock:
            if self.frames is None:
                self.frames = []
            self.frames.append(record)

    def corrections(self) -> list[dict[str, Any]]:
        path = self.dir / "corrections.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def view(self) -> dict[str, Any]:
        return self.meta | {"corrections": len(self.corrections())}


class LabManager:
    """Owns the lab runs under <storage>/lab/runs and one worker thread, so a lab
    run never competes with another lab run for the GPU (live cameras still share it)."""

    def __init__(self, storage_dir: str | Path, extra_profiles: Any = None) -> None:
        self.storage_dir = Path(storage_dir)
        self.root = self.storage_dir / "lab" / "runs"
        self.root.mkdir(parents=True, exist_ok=True)
        (self.storage_dir / "lab" / "videos").mkdir(parents=True, exist_ok=True)
        self.extra_profiles = extra_profiles or (lambda: {})
        self.runs: dict[str, LabRun] = {}
        self._queue: queue.Queue[str] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        for path in sorted(self.root.glob("*/run.json")):
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if meta.get("state") in {"queued", "running"}:
                meta["state"] = "interrupted"  # the process stopped during the run; frames so far are kept
            self.runs[meta["id"]] = LabRun(path.parent, meta)

    # ---- catalog -----------------------------------------------------------

    def catalog(self) -> dict[str, Any]:
        clips = [clip.public() for clip in all_clips(self.storage_dir)]
        return {"clips": clips, "profiles": list_profiles(self.extra_profiles()),
                "drop_folder": str((self.storage_dir / "lab" / "videos").resolve()),
                "limits": {"max_fps": MAX_FPS, "max_seconds": MAX_SECONDS}, "scenes": scenes.catalog()}

    # ---- runs ----------------------------------------------------------------

    def list_runs(self) -> list[dict[str, Any]]:
        for path in self.root.glob("*/run.json"):  # runs made by `python -m sentinel.training compare` in another process
            if path.parent.name not in self.runs:
                try:
                    self.runs[path.parent.name] = LabRun(path.parent, json.loads(path.read_text(encoding="utf-8")))
                except ValueError:
                    pass
        return sorted((run.view() for run in self.runs.values()), key=lambda m: m.get("created", ""), reverse=True)

    def get(self, run_id: str) -> LabRun:
        run = self.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    def start(self, clip_id: str, profile: str = "current", fps: float = 5.0, arm_mode: str = "away",
              species: bool = True, max_seconds: float = 120.0, actor: str = "lab", start_time: str | None = None) -> dict[str, Any]:
        clip = find_clip(self.storage_dir, clip_id)
        if clip is None:
            raise KeyError(clip_id)
        if not Path(clip.path).exists():
            raise FileNotFoundError(f"{clip.id}: video not downloaded (see docs/datasets.md)")
        detector = detector_config(profile, self.extra_profiles())
        if arm_mode not in {"home", "away", "night", "vacation", "disarmed"}:
            raise ValueError("arm_mode must be home, away, night, vacation or disarmed")
        if start_time and not CLOCK.match(start_time):
            raise ValueError("start_time must be HH:MM")
        start_time = start_time or ("02:00" if clip.light == "night" else "14:00")
        fps = max(1.0, min(MAX_FPS, float(fps)))
        max_seconds = max(2.0, min(MAX_SECONDS, float(max_seconds)))
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        run_dir = self.root / run_id
        (run_dir / "frames").mkdir(parents=True)
        meta = {
            "id": run_id, "created": datetime.now().isoformat(timespec="seconds"), "actor": actor, "state": "queued",
            "clip": clip.public(), "profile": profile, "detector": detector,
            "options": {"fps": fps, "arm_mode": arm_mode, "species": species, "max_seconds": max_seconds, "motion_gate": False,
                        "start_time": start_time},
            "scene": resolve_scene(self.storage_dir, clip),  # copied into the run, so later edits never change it
            "progress": {"frames": 0, "expected": None, "video_s": 0.0, "duration_s": None},
            "events": [], "alerts": [], "incidents": [], "summary": {}, "error": "",
        }
        run = LabRun(run_dir, meta)
        run.frames = []
        run.save()
        with self._lock:
            self.runs[run_id] = run
            self._queue.put(run_id)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._work, daemon=True, name="test-lab")
                self._thread.start()
        return run.view()

    # ---- scenes (zones and rules per video; never the site config) ----------

    def scene(self, clip_id: str) -> dict[str, Any]:
        clip = find_clip(self.storage_dir, clip_id)
        if clip is None:
            raise KeyError(clip_id)
        scene = resolve_scene(self.storage_dir, clip)
        if scene is None:  # bench footage keeps config/bench/site.json until zones are drawn for it
            scene = scenes.preset_scene(scenes.default_preset(clip)) | {"source": "bench"}
        return scene | {"clip_id": clip.id, "rule_list": _rule_views(scenes.rules(scene), scene["zones"])}

    def save_scene(self, clip_id: str, body: dict[str, Any], actor: str) -> dict[str, Any]:
        if find_clip(self.storage_dir, clip_id) is None:
            raise KeyError(clip_id)
        scenes.save(self.storage_dir, clip_id, body, actor)
        return self.scene(clip_id)

    def reset_scene(self, clip_id: str) -> dict[str, Any]:
        if find_clip(self.storage_dir, clip_id) is None:
            raise KeyError(clip_id)
        scenes.reset(self.storage_dir, clip_id)
        return self.scene(clip_id)

    def still(self, clip_id: str, t: float = 1.0) -> bytes:
        """One JPEG frame of the clip for drawing zones before any run."""
        import cv2

        clip = find_clip(self.storage_dir, clip_id)
        if clip is None:
            raise KeyError(clip_id)
        capture = cv2.VideoCapture(clip.path)
        try:
            if not capture.isOpened():
                raise FileNotFoundError(f"{clip.id}: video not available")
            capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, float(t)) * 1000)
            ok, image = capture.read()
            if not ok:
                capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, image = capture.read()
            if not ok:
                raise FileNotFoundError(f"{clip.id}: no frame could be decoded")
        finally:
            capture.release()
        if clip.bench_camera:  # the same privacy masks as the runs of this clip
            from ..vision import apply_privacy_masks

            masks = lab_site_config(clip, {}, 5.0, False, self.storage_dir)["cameras"][0].get("privacy_masks", [])
            image = apply_privacy_masks(image, masks)
        scale = min(1.0, FRAME_WIDTH / max(1, image.shape[1]))
        if scale < 1:
            image = cv2.resize(image, (int(image.shape[1] * scale), int(image.shape[0] * scale)), interpolation=cv2.INTER_AREA)
        return cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()

    def cancel(self, run_id: str) -> dict[str, Any]:
        run = self.get(run_id)
        run.cancel.set()
        if run.meta["state"] == "queued":
            run.meta["state"] = "cancelled"
            run.save()
        return run.view()

    def delete(self, run_id: str) -> None:
        run = self.get(run_id)
        if run.meta["state"] in {"queued", "running"}:
            raise ValueError("cancel the run before deleting it")
        self.runs.pop(run_id, None)
        shutil.rmtree(run.dir, ignore_errors=True)

    def add_correction(self, run_id: str, body: dict[str, Any], actor: str) -> dict[str, Any]:
        run = self.get(run_id)
        kind = body.get("kind")
        if kind not in CORRECTION_KINDS:
            raise ValueError(f"kind must be one of {sorted(CORRECTION_KINDS)}")
        record: dict[str, Any] = {"id": uuid.uuid4().hex[:10], "kind": kind, "actor": actor, "created": datetime.now().isoformat(timespec="seconds")}
        if "frame" in body:
            frame = int(body["frame"])
            if not 0 <= frame < len(run.frame_records()):
                raise ValueError("frame out of range")
            record["frame"] = frame
        if body.get("bbox") is not None:
            box = [float(v) for v in body["bbox"]]
            if len(box) != 4 or not all(0 <= v <= 1 for v in box) or box[2] <= 0 or box[3] <= 0:
                raise ValueError("bbox must be [x, y, w, h] in 0..1")
            record["bbox"] = _round_box(box)
        for key in ("label", "detection", "event_id", "note", "track_id"):
            if body.get(key) is not None:
                record[key] = str(body[key])[:200] if key != "detection" else int(body[key])
        with (run.dir / "corrections.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        return record

    def remove_correction(self, run_id: str, correction_id: str) -> None:
        run = self.get(run_id)
        kept = [c for c in run.corrections() if c["id"] != correction_id]
        (run.dir / "corrections.jsonl").write_text("".join(json.dumps(c) + "\n" for c in kept), encoding="utf-8")

    def stop(self) -> None:
        for run in self.runs.values():
            run.cancel.set()

    def run_blocking(self, clip_id: str, **options: Any) -> dict[str, Any]:
        """Headless run in the calling thread (training comparisons, tests)."""
        view = self.start(clip_id, **options)
        run = self.runs[view["id"]]
        while run.meta["state"] in {"queued", "running"}:
            time.sleep(0.05)
        return run.view()

    def _work(self) -> None:
        while True:
            try:
                run_id = self._queue.get(timeout=2)
            except queue.Empty:
                return
            run = self.runs.get(run_id)
            if run is None or run.meta["state"] == "cancelled":
                continue
            try:
                execute(run, self.storage_dir)
            except Exception as error:  # a broken clip or model must not take the service down
                log.exception("test lab run %s failed", run_id)
                run.meta["state"] = "failed"
                run.meta["error"] = f"{type(error).__name__}: {str(error)[:300]}"
                run.save()


def execute(run: LabRun, storage_dir: Path) -> None:
    import cv2

    from ..config import validate_config
    from ..detectors import create_detector, detection_thresholds, resolve_detector_settings
    from ..engine import ZoneMap
    from ..models import canonical_class, effective_categories
    from ..pipeline import filter_detections, with_category_classes
    from ..runtime import SentinelRuntime
    from ..tracker import IoUTracker, TrackerConfig
    from ..vision import Frame, apply_privacy_masks, appearance_vector

    meta = run.meta
    clip = find_clip(storage_dir, meta["clip"]["id"])
    if clip is None:
        raise FileNotFoundError(meta["clip"]["id"])
    options = meta["options"]
    fps = float(options["fps"])
    scene = meta.get("scene")
    config = lab_site_config(clip, meta["detector"], fps, options["species"], run.dir / "store", scene)
    validate_config(config)
    camera = config["cameras"][0]
    if scene is not None:  # Lab zone types (public areas are `general` zones in the engine)
        meta["zones"] = copy.deepcopy(scene["zones"])
        lab_rules = scenes.rules(scene)
    else:
        meta["zones"] = [{key: zone.get(key) for key in ("id", "name", "type", "polygon")} for zone in camera.get("zones", [])]
        lab_rules = config["rules"]
    meta["rules"] = _rule_views(lab_rules, meta["zones"])
    context = {"zones": {zone["id"]: zone for zone in meta["zones"]}, "rules": {rule["id"]: rule for rule in meta["rules"]}}
    meta["state"] = "running"
    meta["started"] = datetime.now().isoformat(timespec="seconds")
    run.save()

    runtime = SentinelRuntime(config, storage_dir=run.dir / "store", notifiers=[])
    runtime.update_settings({"arm_mode": options["arm_mode"]}, "test_lab")
    context["threshold"] = runtime.policy.config.notify_min_severity.get(options["arm_mode"], "none")
    pending_events: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []
    incidents: dict[str, dict[str, Any]] = {}

    def on_bus(topic: str, payload: dict[str, Any]) -> None:
        if topic == "event":
            pending_events.append(payload)
        elif topic == "notification":
            alerts.append(payload)
        elif topic == "incident":
            incidents[payload.get("incident_id") or payload.get("id")] = payload

    runtime.bus.subscribe(on_bus)
    categories = effective_categories(config, camera)
    settings = with_category_classes(resolve_detector_settings(config["detector"]), categories)
    detector_threshold, report_threshold = detection_thresholds(settings)
    detector = create_detector(settings, detector_threshold)
    tracker = IoUTracker(CAMERA, TrackerConfig.from_dict({"high_score": report_threshold}))
    zones = ZoneMap(config)
    enabled = {name for name, values in categories.items() if values.get("detect")}
    min_area = float(settings.get("min_box_area", 0.0004))
    meta["detector_resolved"] = {key: settings.get(key) for key in ("type", "model", "input_size", "score_threshold", "precision", "animal_detector")}

    gt = None
    capture = cv2.VideoCapture(clip.path)
    if not capture.isOpened():
        raise FileNotFoundError(f"OpenCV cannot open {Path(clip.path).name}")
    native_fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
    if not 1 <= native_fps <= 240:
        native_fps = 25.0
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if clip.label_spec:
        from ..bench.labels import load_labels

        gt = load_labels(clip.label_spec, width, height, native_fps)
    step = max(1, round(native_fps / fps))
    duration = total / native_fps if total else None
    limit_s = min(float(options["max_seconds"]), duration or MAX_SECONDS)
    meta["video"] = {"fps": round(native_fps, 2), "frames": total, "width": width, "height": height, "duration_s": round(duration, 2) if duration else None}
    meta["progress"] = {"frames": 0, "expected": int(limit_s * native_fps / step) + 1, "video_s": 0.0, "duration_s": round(limit_s, 2)}
    targets = set().union(*(TARGET_CLASSES.get(s, {s}) for s in clip.subjects)) if clip.subjects else set()
    clock = options.get("start_time") or ("02:00" if clip.light == "night" else "14:00")
    start = datetime.fromisoformat(f"2026-10-15T{clock}:00")  # rule hours are judged on this virtual clock
    start_ms = int(start.timestamp() * 1000)
    scale = min(1.0, FRAME_WIDTH / max(1, width))

    detect_ms: list[float] = []
    frame_ms: list[float] = []
    counts: Counter = Counter()
    track_ids: set[str] = set()
    guardian_seen: dict[str, dict[str, Any]] = {}
    log_file = (run.dir / "frames.jsonl").open("w", encoding="utf-8")
    last_housekeeping = start_ms
    index = n = 0
    try:
        while not run.cancel.is_set():
            if not capture.grab():
                break
            if index % step:
                index += 1
                continue
            ok, image = capture.retrieve()
            if not ok:
                break
            t = index / native_fps
            if t > limit_s:
                break
            now_ms = start_ms + int(t * 1000)
            began = time.perf_counter()
            image = apply_privacy_masks(image, camera.get("privacy_masks", []))
            frame = Frame(image, now_ms, meta={"set_aside_boxes": runtime.guardian_set_aside_boxes(CAMERA)})
            raw = detector.detect(frame)
            detect_ms.append((time.perf_counter() - began) * 1000)
            detections = filter_detections(raw, CAMERA, zones, min_area, enabled)
            active, ended = tracker.update(detections, now_ms)
            for track in active:
                if track.appearance is None or track.hits % 10 == 0:
                    track.appearance = appearance_vector(image, track.bbox)
            runtime.update_camera_status(CAMERA, connected=True, last_frame_ms=now_ms)
            runtime.process_camera_update(CAMERA, now_ms, active, ended)
            runtime.guardian_species_frame(CAMERA, now_ms, image)
            frame_ms.append((time.perf_counter() - began) * 1000)
            _wait_for_species(runtime)
            if now_ms - last_housekeeping >= 1000:
                runtime.housekeeping(now_ms)
                last_housekeeping = now_ms

            record = _frame_record(n, index, t, detections, active, runtime, report_threshold, canonical_class)
            record["ms"] = {"detect": round(detect_ms[-1], 1), "frame": round(frame_ms[-1], 1)}
            _score(record, gt, clip, t, index, targets)
            for event in pending_events:
                event["frame"] = n
                event["t"] = round((event["timestamp_ms"] - start_ms) / 1000, 2)
                record.setdefault("events", []).append(event.get("event_id") or event.get("id"))
                meta["events"].append(_event_view(event, context))
            pending_events.clear()
            counts.update(d["cls"] for d in record["dets"] if not d.get("weak"))
            track_ids.update(tr["id"] for tr in record["tracks"])
            for g in record["guardian"]:
                guardian_seen[g["gid"]] = g
            out = cv2.resize(image, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA) if scale < 1 else image
            cv2.imwrite(str(run.dir / "frames" / f"{n:05d}.jpg"), out, [cv2.IMWRITE_JPEG_QUALITY, 80])
            log_file.write(json.dumps(record) + "\n")
            log_file.flush()
            run.append_frame(record)
            n += 1
            index += 1
            meta["progress"].update({"frames": n, "video_s": round(t, 2)})
            meta["alerts"] = [_alert_view(a, start_ms) for a in alerts]
            if n % 10 == 0:
                _alert_decisions(meta, context)
                meta["summary"] = _summary(run, counts, track_ids, guardian_seen, detect_ms, frame_ms, incidents)
                run.save()
        end_ms = start_ms + int(index / native_fps * 1000)
        runtime.process_camera_update(CAMERA, end_ms, [], tracker.flush())
        for event in pending_events:
            event["frame"] = max(0, n - 1)
            event["t"] = round((event["timestamp_ms"] - start_ms) / 1000, 2)
            meta["events"].append(_event_view(event, context))
        meta["species_worker"] = runtime.species.status() if runtime.species else {"enabled": False}
        meta["detector_runtime"] = detector.describe() if hasattr(detector, "describe") else {}
    finally:
        log_file.close()
        capture.release()
        detector.close()
        runtime.dispatcher.stop()
        runtime.close()
    meta["alerts"] = [_alert_view(a, start_ms) for a in alerts]
    meta["incidents"] = [_incident_view(i) for i in incidents.values()]
    _alert_decisions(meta, context)
    meta["summary"] = _summary(run, counts, track_ids, guardian_seen, detect_ms, frame_ms, incidents)
    meta["state"] = "cancelled" if run.cancel.is_set() else "done"
    meta["finished"] = datetime.now().isoformat(timespec="seconds")
    run.save()


def _wait_for_species(runtime: Any) -> None:
    service = runtime.species
    if service is None:
        return
    worker = service.worker
    idle = 0
    deadline = time.monotonic() + SPECIES_WAIT_S
    while idle < 2 and time.monotonic() < deadline:
        if worker.queue.empty() and not worker.busy and worker.state != "starting":
            idle += 1
        else:
            idle = 0
        time.sleep(0.005)


def _frame_record(n, index, t, detections, active, runtime, report_threshold, canonical_class) -> dict[str, Any]:
    site_ids = runtime.engine.correlator.track_to_site
    dets = []
    for det in detections:
        item = {"label": det.label, "cls": canonical_class(det.label), "conf": round(det.confidence, 3), "bbox": _round_box(det.bbox)}
        if det.confidence < report_threshold:
            item["weak"] = True  # only extends existing tracks; not a reported detection
        dets.append(item)
    tracks = [{"id": track.track_id.rsplit(":", 1)[-1], "site": site_ids.get(track.track_id, ""), "label": track.label,
               "cls": canonical_class(track.label), "bbox": _round_box(track.bbox), "hits": track.hits} for track in active]
    guardian = []
    overlays = {o["track_id"]: o for o in runtime.guardian_overlays(CAMERA)}
    for view in runtime.guardian.snapshot(CAMERA) if runtime.guardian.settings.enabled else []:
        if view["lost"] or view["track_id"] not in overlays:
            continue
        overlay = overlays[view["track_id"]]
        species = view["species"]
        classifier = species.get("classifier") or {}
        guardian.append({
            "gid": view["guardian_track_id"].rsplit(":", 1)[-1], "track": view["track_id"].rsplit(":", 1)[-1],
            "bbox": _round_box(view["bbox"]), "trail": [[round(x, 4), round(y, 4)] for x, y in overlay["trail"]],
            "movement": overlay["movement"], "level": overlay["level"], "label": overlay["label"],
            "species": species["display"], "species_status": classifier.get("status") or species.get("status"),
            "species_confidence": classifier.get("confidence"), "targets": view["targets"],
        })
    return {"n": n, "i": index, "t": round(t, 3), "dets": dets, "tracks": tracks, "guardian": guardian}


def _score(record: dict[str, Any], gt, clip: LabClip, t: float, index: int, targets: set[str]) -> None:
    """Compare with labels where they exist. Box labels (MOT16/CAVIAR: people only):
    each reported person box is tp/fp, each labelled person matched or missed.
    Presence ranges: whether the frame holds a target box when it should."""
    if gt is not None:
        from ..bench.metrics import match_boxes

        boxes = [box for box in gt.at_frame(int(round(t * gt.fps))) if box.label == "person"]
        people = [i for i, d in enumerate(record["dets"]) if d["cls"] == "person" and not d.get("weak")]
        matches, unmatched, missed = match_boxes([record["dets"][i]["bbox"] for i in people], boxes)
        for p, _ in matches:
            record["dets"][people[p]]["verdict"] = "tp"
        for p in unmatched:
            record["dets"][people[p]]["verdict"] = "fp"
        missed_ids = {id(box) for box in missed}
        record["gt"] = [{"bbox": _round_box(box.bbox), "id": box.track_id, "ignore": box.ignore, "missed": id(box) in missed_ids} for box in boxes]
        record["score"] = {"tp": len(matches), "fp": len(unmatched), "missed": len(missed)}
    elif clip.present_s or clip.absent_s:
        found = any(d["cls"] in targets for d in record["dets"] if not d.get("weak")) if targets else False
        other = any(d["cls"] in {"person", "vehicle", "animal"} for d in record["dets"] if not d.get("weak"))
        if _in_ranges(t, clip.present_s):
            record["presence"] = {"expected": True, "verdict": "found" if found else "missed"}
        elif _in_ranges(t, clip.absent_s):
            record["presence"] = {"expected": False, "verdict": "false_positive" if (found or other) else "clear"}


def _event_view(event: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    details = event.get("details") or {}
    view = {
        "id": event.get("event_id") or event.get("id"), "type": event.get("event_type"), "severity": event.get("severity"),
        "summary": event.get("summary"), "zone": event.get("zone_id", ""), "rule": event.get("rule_id") or details.get("rule_id", ""),
        "frame": event.get("frame"), "t": event.get("t"), "reasons": event.get("reason_codes", [])[:8],
        "confidence": event.get("confidence"), "guardian": bool(details.get("guardian")), "level": details.get("level_name") or details.get("level"),
        "incident": event.get("incident_id", ""), "health": event.get("event_type", "").startswith("camera_"),
    }
    if context is not None and not view["health"]:
        view["explain"] = _explain(event, view, context)
    return view


def _explain(event: dict[str, Any], view: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Why the event exists, from what was observed: zone, rule, how long, how sure."""
    details = event.get("details") or {}
    zone = context["zones"].get(view["zone"]) or {}
    rule = context["rules"].get(view["rule"]) or {}
    duration = details.get("dwell_s")
    if duration is None and event.get("started_ms") is not None:
        duration = round(max(0, event["timestamp_ms"] - event["started_ms"]) / 1000, 1)
    out: dict[str, Any] = {
        "zone": zone.get("name", view["zone"]), "zone_type": zone.get("type", ""),
        "ordinary_area": zone.get("type") in scenes.ORDINARY_TYPES,
        "rule": rule.get("name", ""), "rule_what": rule.get("what", ""), "rule_when": rule.get("when", ""),
        "duration_s": duration, "confidence": view["confidence"],
        "observations": (details.get("observation") or {}).get("track_hits"),
    }
    first_seen = (details.get("subject") or {}).get("first_seen_ms")
    if first_seen is not None:
        out["tracked_s"] = round(max(0, event["timestamp_ms"] - first_seen) / 1000, 1)
    if details.get("threshold_s") is not None:
        out["threshold_s"] = details["threshold_s"]
    if view["type"] == "activity":
        out["basis"] = "First sighting of this subject in a zone: ordinary activity, logged as info."
    elif view["guardian"]:
        out["basis"] = f"Wilderness Guardian level {view['level'] or ''} from the animal's movement relative to protected zones."
    return out


def _alert_decisions(meta: dict[str, Any], context: dict[str, Any]) -> None:
    """For each event: did the alert policy push it, and if not, why. The policy
    itself decided; this only reads its output (alerts per incident) back."""
    from ..models import SEVERITY_RANK

    threshold = context.get("threshold", "none")
    mode = meta["options"]["arm_mode"]
    alerts_by_incident: dict[str, list[float]] = {}
    for alert in meta["alerts"]:
        alerts_by_incident.setdefault(alert.get("incident") or "", []).append(alert["t"])
    for event in meta["events"]:
        if event.get("health") or "explain" not in event:
            continue
        reasons = event.get("reasons") or []
        t = event.get("t") or 0
        times = alerts_by_incident.get(event["incident"], []) if event.get("incident") else []
        if event["severity"] == "info":
            if "rule_cooldown" in reasons:
                text = "Logged as info: the rule fired again within its cooldown."
            elif "category_alerts_off" in reasons:
                text = "Logged as info: alerts are off for this category."
            else:
                text = "Logged as info: ordinary activity is recorded, never pushed."
            decision = "logged"
        elif any(abs(at - t) <= 0.5 for at in times):
            decision, text = "alerted", "A simulated alert was raised for this incident."
        elif any(at < t for at in times):
            decision, text = "logged", "Logged: this incident had already alerted (one alert per incident, again only if its severity rises)."
        elif threshold == "none":
            decision, text = "logged", f"Logged: nothing is pushed in {mode} mode."
        elif SEVERITY_RANK.get(event["severity"], 0) < SEVERITY_RANK.get(threshold, 0):
            decision, text = "logged", f"Logged: {event['severity']} is below the {mode} mode alert level ({threshold})."
        else:
            decision, text = "logged", "Logged: held back by the alert rate limit."
        event["explain"]["alert"] = decision
        event["explain"]["alert_reason"] = text


def _rule_views(rules: list[dict[str, Any]], zones: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from ..rules import describe_rule

    names = {zone["id"]: zone.get("name", zone["id"]) for zone in zones}
    views = []
    for rule in rules:
        described = describe_rule(rule, names) if rule.get("severity") else {"what": "", "when": ""}
        views.append({key: rule[key] for key in ("id", "type", "severity", "zone_types", "min_dwell_s", "active_hours", "classes") if key in rule}
                     | {"name": rule.get("name", rule["id"]), "enabled": rule.get("enabled", True),
                        "what": described["what"], "when": described["when"]})
    return views


def _alert_view(alert: dict[str, Any], start_ms: int) -> dict[str, Any]:
    return {"title": alert.get("title"), "body": alert.get("body") or alert.get("message"), "severity": alert.get("severity"),
            "t": round((alert.get("timestamp_ms", start_ms) - start_ms) / 1000, 2), "incident": alert.get("incident_id", ""),
            "simulated": True}


def _incident_view(incident: dict[str, Any]) -> dict[str, Any]:
    return {key: incident.get(key) for key in ("incident_id", "title", "summary", "severity", "status", "event_ids")}


def _summary(run: LabRun, counts, track_ids, guardian_seen, detect_ms, frame_ms, incidents) -> dict[str, Any]:
    frames = run.frame_records()
    summary: dict[str, Any] = {
        "frames": len(frames), "detections": dict(counts), "tracks": len(track_ids), "guardian_tracks": len(guardian_seen),
        "species": sorted({f"{g['species']} ({g['species_status']})" for g in guardian_seen.values()}),
        "guardian_levels": dict(Counter(g["level"] for g in guardian_seen.values())),
        "events": dict(Counter(e["type"] for e in run.meta["events"])), "alerts": len(run.meta["alerts"]), "incidents": len(incidents),
        "detect_ms": {"p50": _pct(detect_ms, 50), "p95": _pct(detect_ms, 95)}, "frame_ms": {"p50": _pct(frame_ms, 50), "p95": _pct(frame_ms, 95)},
    }
    scored = [f["score"] for f in frames if "score" in f]
    if scored:
        tp, fp, missed = (sum(s[key] for s in scored) for key in ("tp", "fp", "missed"))
        summary["boxes"] = {"tp": tp, "fp": fp, "missed": missed,
                            "precision": round(tp / (tp + fp), 3) if tp + fp else None, "recall": round(tp / (tp + missed), 3) if tp + missed else None,
                            "labels": "person boxes"}
    presence = [f["presence"] for f in frames if "presence" in f]
    if presence:
        present = [p for p in presence if p["expected"]]
        absent = [p for p in presence if not p["expected"]]
        summary["presence"] = {"present_frames": len(present), "found": sum(p["verdict"] == "found" for p in present),
                               "absent_frames": len(absent), "false_positive_frames": sum(p["verdict"] == "false_positive" for p in absent),
                               "recall": round(sum(p["verdict"] == "found" for p in present) / len(present), 3) if present else None,
                               "labels": "presence ranges labelled by eye"}
    summary["errors"] = [f["n"] for f in frames if (f.get("score") and (f["score"]["fp"] or f["score"]["missed"]))
                         or (f.get("presence") and f["presence"]["verdict"] in {"missed", "false_positive"})]
    return summary
