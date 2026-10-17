"""Catalog private cam01 originals and exercise existing offline replay engines.

Run from the repository root with PYTHONPATH=src. All outputs stay local in
detector_runs/. Existing runs and source recordings are never overwritten.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from senior_safety.pose_extractor import extract_features, load_zones_config
from senior_safety.pose_replay import read_pose_jsonl, run_replay
from senior_safety.state_machine import load_rules
from sentinel.bench.pose import PoseRecordingScenario
from sentinel.replay import run_scenario


ROW_KEYS = {"clip_id", "frame_index", "timestamp_ms", "person_id", "pose_model",
            "pose_confidence", "full_body_pose_confidence", "landmarks", "bbox", "quality_flags"}
LANDMARK_KEYS = {"name", "x", "y", "z", "visibility"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect_recording(path: Path) -> dict:
    content = path.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in content.splitlines() if line.strip()]
    recording = read_pose_jsonl(path)
    timestamps = [row["timestamp_ms"] for row in rows]
    extra = sorted({key for row in rows for key in row if key not in ROW_KEYS})
    extra_landmark = sorted({key for row in rows for lm in row.get("landmarks", [])
                             for key in lm if key not in LANDMARK_KEYS})
    stat = path.stat()
    birthtime = getattr(stat, "st_birthtime", None)
    return {
        "clip_id": recording.clip_id, "file_path": path.as_posix(), "sha256": digest(path),
        "bytes": stat.st_size,
        "filesystem_created_utc": datetime.fromtimestamp(birthtime, timezone.utc).isoformat() if birthtime is not None else None,
        "filesystem_modified_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "filename_session_local_unzoned": path.stem.removeprefix("cam01_").removesuffix("_live"),
        "first_frame_utc": datetime.fromtimestamp(min(timestamps) / 1000, timezone.utc).isoformat() if timestamps else None,
        "last_frame_utc": datetime.fromtimestamp(max(timestamps) / 1000, timezone.utc).isoformat() if timestamps else None,
        "frames": len(rows), "duration_s": recording.duration_ms / 1000,
        "frames_with_landmarks": sum(bool(row.get("landmarks")) for row in rows),
        "frames_with_person": sum(extract_features(lm, t).person_present for t, lm in recording.frames),
        "quality_flags": dict(Counter(flag for row in rows for flag in row.get("quality_flags", []))),
        "models": sorted({row.get("pose_model", "") for row in rows}),
        "nonincreasing_timestamps": sum(b <= a for a, b in zip(timestamps, timestamps[1:])),
        "max_gap_ms": max((b - a for a, b in zip(timestamps, timestamps[1:])), default=0),
        "unexpected_row_keys": extra, "unexpected_landmark_keys": extra_landmark,
        "privacy_pattern_hits": {name: len(re.findall(pattern, content)) for name, pattern in {
            "url": r"(?i)(?:https?|rtsp)://",
            "email": r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}",
            "secret_key": r'(?i)"(?:password|token|secret|api_key|stream_url)"\s*:',
        }.items()},
        "provenance": "camera_derived_pilot_per_repository_history",
        "primary_label": "uncertain", "review_status": "needs_review",
        "consent_status": "unverified", "privacy_level": "private_movement_and_timestamps",
        "split": "local_regression_only", "training_eligible": False,
        "status": "empty_preserved" if not rows else "usable_for_unscored_replay",
    }


def offline_config() -> dict:
    return {
        "site": {"id": "pose_replay"},
        "cameras": [{"id": "pose_replay", "zones": [{"id": "illustrative_area",
            "name": "Illustrative replay area", "type": "general",
            "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]}]}],
        "rules": [], "alerts": {"channels": []}, "guardian": {"enabled": False},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("detector_runs") / ("cam01_" + datetime.now().strftime("%Y%m%dT%H%M%S%f")))
    args = parser.parse_args()
    source = Path("data/processed/pose/cam01")
    paths = sorted(source.glob("*.jsonl"))
    if not paths:
        raise SystemExit("No local cam01 originals found; nothing changed.")
    # Keep derived movement records out of Git even if someone supplies --out.
    if not args.out.resolve().is_relative_to(Path("detector_runs").resolve()):
        raise SystemExit("Output must be inside the ignored detector_runs directory.")
    args.out.mkdir(parents=True, exist_ok=False)
    records = [inspect_recording(path) for path in paths]
    with Path("data/labels/clips.csv").open(encoding="utf-8", newline="") as handle:
        historical = {row["clip_id"]: row for row in csv.DictReader(handle)}
    for record in records:
        record["historical_annotation_unverified"] = historical.get(record["clip_id"])
    (args.out / "manifest.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    # Match the existing dataset registry schema, but keep private entries local.
    with Path("data/dataset_manifest.csv").open(encoding="utf-8", newline="") as handle:
        fields = next(csv.reader(handle))
    with (args.out / "dataset_manifest.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow({"clip_id": record["clip_id"], "source": "local_camera_pose",
                "source_dataset": "cam01_private_pilot", "camera_id": "cam01",
                "duration_s": record["duration_s"], "has_pose": bool(record["frames"]),
                "privacy_level": record["privacy_level"], "consent_status": "unverified",
                "file_path": record["file_path"], "sha256": record["sha256"],
                "notes": "Local regression only; uncertain labels; source resolution/audio/consent unknown. Timing in manifest.jsonl."})
    labels = args.out / "labels"
    labels.mkdir()
    with (labels / "clips.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["clip_id", "primary_label", "scenario", "review_status", "notes"])
        writer.writeheader()
        for record in records:
            writer.writerow({"clip_id": record["clip_id"], "primary_label": "uncertain", "review_status": "needs_review",
                             "notes": "Private pilot; no reviewed ground truth. See manifest for historical notes."})
    usable = [path for path, record in zip(paths, records) if record["frames"]]
    metrics = run_replay(usable, load_zones_config("config/zones.cam01-night.json"),
                         load_rules("config/monitoring-rules.example.json"), labels,
                         args.out / "legacy_replay", write_clip_decisions=True)
    sentinel_results = []
    for path in usable:
        scenario = PoseRecordingScenario(path)
        result = run_scenario(offline_config(), scenario, start=datetime(2026, 1, 1, 12),
                              arm_mode="disarmed", tail_s=5)
        result["input_kind"] = "private_camera_pose_derived_boxes_not_ground_truth"
        result["geometry"] = "illustrative_full_frame_zone_not_site_calibration"
        (args.out / (path.stem + "_sentinel.json")).write_text(json.dumps(result, indent=2), encoding="utf-8")
        sentinel_results.append({"clip_id": path.stem, "activity_events": len(result["events"]),
                                 "incidents": len(result["incidents"]), "notifications": len(result["notifications"])})
    unchanged = all(digest(path) == record["sha256"] for path, record in zip(paths, records))
    summary = {"originals": len(paths), "original_bytes": sum(r["bytes"] for r in records),
               "frames": sum(r["frames"] for r in records), "duration_s": sum(r["duration_s"] for r in records),
               "empty_originals": sum(not r["frames"] for r in records),
               "original_hashes_unchanged": unchanged, "legacy_unscored_metrics": metrics,
               "sentinel_replays": sentinel_results,
               "limitations": "No accuracy claims; labels/consent unverified; pose absence is not proof of an empty room; no wildlife or image training data."}
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if not unchanged:
        raise RuntimeError("Original hashes changed during the audit; investigate concurrent writes.")
    print(f"Preserved {len(paths)} originals; replayed {len(usable)}. Private report: {args.out / 'summary.json'}")


if __name__ == "__main__":
    main()
