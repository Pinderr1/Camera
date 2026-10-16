"""Repeatable Guardian video test: wildlife clips through Sentinel's detector,
tracker, runtime, Guardian monitor and (optionally) SpeciesNet.

    python scripts/guardian/fetch_wildlife_videos.py            # once
    python scripts/guardian/video_eval.py --out bench_runs/guardian_video/NAME \
        --detectors yolox_s,yolox_s+mdv6 [--no-species] [--only black_bear_day,...] [--fps 5]

Your own footage: add an entry to a copy of config/guardian/videos.json with
`"path"` instead of `"title"` (any file OpenCV reads), label `present_s` /
`absent_s` by eye, and pass `--registry`.

Each clip is decoded at `--fps` (Sentinel's default camera rate is 5) on a
virtual clock, so results do not depend on machine speed. Per frame, the same
stages as pipeline.CameraWorker run: detect -> filter -> IoUTracker ->
SentinelRuntime (temporal engine, Guardian) -> species requests. The species
worker runs on its real background thread; the evaluation waits for it after
each frame, so a slow classifier never skips a crop here (live, it may).

Scores per clip and detector:
  * presence recall: frames inside `present_s` with at least one animal box;
  * false-positive frames: frames inside `absent_s` with an animal box;
  * Guardian tracks: how many were opened (fragmentation) and the longest;
  * species: what Guardian finally says about each track, and whether a track
    confirmed the expected species (or confirmed a wrong one).
Presence ranges were labelled by eye from contact sheets, not per frame: treat
small differences (a frame or two at the edges) as noise."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

MD = "mdv6_apa_rtdetr_c"
DETECTORS: dict[str, dict[str, Any]] = {
    "yolox_s": {"type": "onnx", "model": "auto"},
    "mdv6": {"type": "onnx", "model": MD, "score_threshold": 0.6},
    "yolox_s+mdv6": {"type": "onnx", "model": "auto", "animal_detector": {"model": MD, "score_threshold": 0.6}},
}
CAMERA = "trailcam"


def site_config(clip: dict[str, Any], detector: dict[str, Any], fps: float, species: bool, storage: Path) -> dict[str, Any]:
    return {
        "site": {"id": "guardian_video_eval", "name": "Guardian video evaluation"},
        "cameras": [{"id": CAMERA, "name": clip["id"], "fps": fps, "source": {"type": "file", "path": "unused"},
                     "zones": [{"id": "near_camera", "name": "Near the camera", "type": "yard",
                                "polygon": [[0, 0.7], [1, 0.7], [1, 1], [0, 1]]}]}],
        "topology": {"links": []},
        "rules": [],
        "detector": detector,
        "categories": {"animal": {"detect": True, "alerts": False}},
        "guardian": {"enabled": True, "protected_zones": ["near_camera"],
                     "species_classifier": {"enabled": species, "model_dir": str(ROOT / "models/speciesnet/v4.0.3a")}},
        "privacy": {"store_clips": False},
        "storage": {"dir": str(storage)},
    }


def in_ranges(t: float, ranges: list[list[float]]) -> bool:
    return any(start <= t <= end for start, end in ranges)


def pct(values: list[float], q: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q / 100 * len(ordered)))], 1)


def species_matches(classifier: dict[str, Any] | None, expected: dict[str, Any] | None) -> bool | None:
    if not classifier or classifier.get("status") != "confirmed":
        return None
    if expected is None:
        return False  # confirmed a species on a clip where none is expected
    taxonomy = ((classifier.get("taxon") or {}).get("taxonomy") or ";;;;").split(";")
    return taxonomy[3] == expected["genus"] and (taxonomy[4].split(" ")[0] == expected["species"])


def wait_for_species(runtime: Any) -> None:
    service = runtime.species
    if service is None:
        return
    worker = service.worker
    idle_checks = 0
    deadline = time.monotonic() + 60
    while idle_checks < 2 and time.monotonic() < deadline:
        if worker.queue.empty() and not worker.busy and worker.state != "starting":
            idle_checks += 1
        else:
            idle_checks = 0
        time.sleep(0.005)


def run_clip(clip: dict[str, Any], video: Path, detector_name: str, fps: float, species: bool, workdir: Path) -> dict[str, Any]:
    import cv2

    from sentinel.config import validate_config
    from sentinel.detectors import create_detector, detection_thresholds, resolve_detector_settings
    from sentinel.engine import ZoneMap
    from sentinel.models import canonical_class, effective_categories
    from sentinel.pipeline import filter_detections, with_category_classes
    from sentinel.resources import _process_rss_mb
    from sentinel.runtime import SentinelRuntime
    from sentinel.tracker import IoUTracker, TrackerConfig
    from sentinel.vision import Frame

    config = site_config(clip, DETECTORS[detector_name], fps, species, workdir)
    validate_config(config)
    runtime = SentinelRuntime(config, notifiers=[])
    runtime.update_settings({"arm_mode": "home"}, "video_eval")
    events: list[dict[str, Any]] = []
    runtime.bus.subscribe(lambda topic, payload: events.append(payload) if topic == "event" else None)
    camera = config["cameras"][0]
    categories = effective_categories(config, camera)
    settings = with_category_classes(resolve_detector_settings(config["detector"]), categories)
    report_threshold, high_score = detection_thresholds(settings)
    rss_before = _process_rss_mb()
    detector = create_detector(settings, report_threshold)
    tracker = IoUTracker(CAMERA, TrackerConfig.from_dict({"high_score": high_score}))
    zones = ZoneMap(config)
    enabled = {name for name, values in categories.items() if values.get("detect")}
    min_area = float(settings.get("min_box_area", 0.0004))

    capture = cv2.VideoCapture(str(video))
    native_fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    step = max(1, round(native_fps / fps))
    start_ms = 1_792_000_000_000  # fixed virtual clock (2026-10-15): deterministic engine timing
    frames = Counter()
    detect_ms, guardian_views, track_ids = [], {}, set()
    rss_peak = rss_before or 0
    index = 0
    while True:
        ok = capture.grab()
        if not ok:
            break
        if index % step:
            index += 1
            continue
        ok, image = capture.retrieve()
        if not ok:
            break
        t = index / native_fps
        now_ms = start_ms + int(t * 1000)
        started = time.perf_counter()
        detections = filter_detections(detector.detect(Frame(image, now_ms)), CAMERA, zones, min_area, enabled)
        detect_ms.append((time.perf_counter() - started) * 1000)
        animals = [d for d in detections if canonical_class(d.label) == "animal"]
        active, ended = tracker.update(detections, now_ms)
        runtime.update_camera_status(CAMERA, connected=True, last_frame_ms=now_ms)
        runtime.process_camera_update(CAMERA, now_ms, active, ended)
        runtime.guardian_species_frame(CAMERA, now_ms, image)
        wait_for_species(runtime)
        track_ids.update(track.track_id for track in active if track.label == "animal")
        shown = False
        for view in runtime.guardian.snapshot(CAMERA):
            guardian_views[view["guardian_track_id"]] = view
            shown = shown or (view["last_seen_ms"] == now_ms and not view["lost"] and not view.get("set_aside"))
        state = "present" if in_ranges(t, clip["present_s"]) else "absent" if in_ranges(t, clip["absent_s"]) else "unscored"
        frames[state] += 1
        frames[f"{state}_with_animal"] += bool(animals)
        frames["animal_boxes"] += len(animals)
        frames[f"{state}_with_track"] += shown  # what a person would see: a Guardian track on screen
        if index % (step * 25) == 0:
            rss_peak = max(rss_peak, _process_rss_mb() or 0)
        index += 1
    capture.release()
    now_ms = start_ms + int(index / native_fps * 1000)
    runtime.process_camera_update(CAMERA, now_ms, [], tracker.flush())
    for view in runtime.guardian.snapshot(CAMERA):
        guardian_views[view["guardian_track_id"]] = view
    species_status = runtime.species.status() if runtime.species else {"enabled": False}
    describe = detector.describe() if hasattr(detector, "describe") else {}
    detector.close()
    runtime.dispatcher.stop()
    runtime.close()

    expected = clip.get("expected")
    tracks = []
    for view in guardian_views.values():
        classifier = view["species"].get("classifier")
        tracks.append({
            "id": view["guardian_track_id"], "duration_s": round((view["last_seen_ms"] - view["first_seen_ms"]) / 1000, 1),
            "display": view["species"]["display"], "detector_labels": view["species"]["detector_labels"],
            "species_status": (classifier or {}).get("status"), "species_display": (classifier or {}).get("display"),
            "species_confidence": (classifier or {}).get("confidence"), "matches_expected": species_matches(classifier, expected),
            "classifier_top": ((classifier or {}).get("taxon") or {}).get("label"),
            "set_aside": bool(view.get("set_aside")),
            "level": (view.get("assessment") or {}).get("level"),
        })
    guardian_events = [e for e in events if (e.get("details") or {}).get("guardian")]
    return {
        "clip": clip["id"], "detector": detector_name, "light": clip["light"], "animal": clip.get("animal"),
        "frames": dict(frames), "fps": fps, "video_frames": total,
        "presence_recall": round(frames["present_with_animal"] / frames["present"], 3) if frames["present"] else None,
        "false_positive_frames": f"{frames['absent_with_animal']}/{frames['absent']}" if frames["absent"] else None,
        "track_frames_in_empty_ranges": f"{frames['absent_with_track']}/{frames['absent']}" if frames["absent"] else None,
        "camera_tracks": len(track_ids), "guardian_tracks": sum(not t["set_aside"] for t in tracks),
        "set_aside_tracks": sum(t["set_aside"] for t in tracks),
        "longest_track_s": max((t["duration_s"] for t in tracks if not t["set_aside"]), default=0.0),
        "species_confirmed_right": any(t["matches_expected"] is True for t in tracks),
        "species_confirmed_wrong": [t["species_display"] for t in tracks if t["matches_expected"] is False],
        "tracks": sorted(tracks, key=lambda t: (t["set_aside"], -t["duration_s"])),
        "guardian_events": dict(Counter(f"{e.get('type')}:{(e.get('details') or {}).get('level', '')}" for e in guardian_events)),
        "detect_ms": {"p50": pct(detect_ms, 50), "p95": pct(detect_ms, 95)},
        "rss_mb": {"before_detector": rss_before, "peak_sampled": rss_peak},
        "detector_info": describe, "species_worker": species_status,
    }


def render(results: list[dict[str, Any]], species: bool) -> str:
    detectors = list(dict.fromkeys(r["detector"] for r in results))
    lines = ["# Guardian video test", "",
             "Fixed trail-camera clips (Wikimedia Commons; licences in config/guardian/videos.json), decoded at "
             f"{results[0]['fps'] if results else '-'} fps on a virtual clock. Presence ranges were labelled by eye; "
             "a frame or two at range edges is noise. Not security-camera footage.", "",
             "## Per clip", "",
             "| Clip | Light | Detector | Presence recall | Empty ranges: frames with a box / with a shown track | Guardian tracks (set aside) | Longest (s) | Species (best track) | Expected species confirmed | Detect p50 ms |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in results:
        best = r["tracks"][0] if r["tracks"] and not r["tracks"][0]["set_aside"] else None
        best_text = (f"{best['species_display'] or best['display']} ({best['species_status'] or 'detector only'})" if best else "-")
        verdict = "yes" if r["species_confirmed_right"] else ("WRONG: " + ", ".join(map(str, r["species_confirmed_wrong"])) if r["species_confirmed_wrong"] else "no")
        if not species:
            verdict = "-"
        recall = f"{r['presence_recall']:.0%}" if r["presence_recall"] is not None else "-"
        lines.append(f"| {r['clip']} | {r['light']} | {r['detector']} | {recall} | {r['false_positive_frames'] or '-'} / {r['track_frames_in_empty_ranges'] or '-'} | "
                     f"{r['guardian_tracks']} ({r['set_aside_tracks']}) | {r['longest_track_s']} | {best_text} | {verdict} | {r['detect_ms']['p50']} |")
    lines += ["", "## Totals", "", "| Detector | Light | Presence frames found | Empty-range frames with a box | Empty-range frames with a shown track | Clips with expected species confirmed | Wrong confirmations |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for name in detectors:
        for light in ("day", "night", "all"):
            rows = [r for r in results if r["detector"] == name and (light == "all" or r["light"] == light)]
            if not rows:
                continue
            present = sum(r["frames"].get("present", 0) for r in rows)
            found = sum(r["frames"].get("present_with_animal", 0) for r in rows)
            absent = sum(r["frames"].get("absent", 0) for r in rows)
            fp = sum(r["frames"].get("absent_with_animal", 0) for r in rows)
            shown = sum(r["frames"].get("absent_with_track", 0) for r in rows)
            expected = [r for r in rows if r.get("expects_species")]
            right = sum(1 for r in expected if r["species_confirmed_right"])
            wrong = sum(len(r["species_confirmed_wrong"]) for r in rows)
            lines.append(f"| {name} | {light} | {found}/{present} ({found / max(1, present):.0%}) | {fp}/{absent} | {shown}/{absent} | "
                         f"{right}/{len(expected) if species else '-'} | {wrong if species else '-'} |")
    lines += ["", "Track detail (per clip and detector) is in results.json.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, default=ROOT / "config/guardian/videos.json")
    parser.add_argument("--videos", type=Path, default=ROOT / "data/guardian/videos")
    parser.add_argument("--out", type=Path, default=ROOT / "bench_runs/guardian_video/latest")
    parser.add_argument("--detectors", default="yolox_s,yolox_s+mdv6")
    parser.add_argument("--only", default="")
    parser.add_argument("--fps", type=float, default=5.0)
    parser.add_argument("--no-species", action="store_true")
    args = parser.parse_args()
    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    wanted = set(filter(None, args.only.split(",")))
    args.out.mkdir(parents=True, exist_ok=True)
    results = []
    for clip in registry["clips"]:
        if wanted and clip["id"] not in wanted:
            continue
        video = Path(clip["path"]) if clip.get("path") else args.videos / f"{clip['id']}.webm"
        if not video.exists():
            print(f"skip {clip['id']}: {video} missing (run scripts/guardian/fetch_wildlife_videos.py)")
            continue
        for name in args.detectors.split(","):
            with tempfile.TemporaryDirectory(prefix="guardian_video_") as storage:
                result = run_clip(clip, video, name, args.fps, not args.no_species, Path(storage))
            result["expects_species"] = clip.get("expected") is not None
            results.append(result)
            print(f"{clip['id']:18} {name:14} recall={result['presence_recall']} fp={result['false_positive_frames']} "
                  f"tracks={result['guardian_tracks']} species_ok={result['species_confirmed_right']} wrong={result['species_confirmed_wrong']}", flush=True)
    (args.out / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    text = render(results, not args.no_species)
    (args.out / "report.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
