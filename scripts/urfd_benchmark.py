"""Download URFD cam0 RGB sequences, extract pose JSONL, and score the detector.

URFD: 30 falls + 40 ADLs with frame-level lying labels — a ready-made labeled
benchmark for the fall pipeline. Raw frames stay in data/raw (gitignored);
the reusable artifacts are the pose JSONL files and the generated labels.

Usage (from repo root):
    python scripts/urfd_benchmark.py --out detector_runs/urfd_baseline
    python scripts/urfd_benchmark.py --limit 4 --out detector_runs/urfd_smoke
    python scripts/urfd_benchmark.py --skip-pose --out detector_runs/urfd_rescored
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from senior_safety.pose_extractor import create_landmarker, load_zones_config
from senior_safety.pose_replay import collect_pose_paths, run_replay
from senior_safety.state_machine import load_rules

BASE_URL = "http://fenix.ur.edu.pl/~mkepski/ds/data"
RAW_DIR = REPO / "data" / "raw" / "public" / "urfd"
POSE_DIR = REPO / "data" / "processed" / "pose" / "urfd"
LABELS_DIR = REPO / "data" / "labels" / "urfd"
SOURCE_FPS = 30
FRAME_STEP = 3  # 30 fps source sampled every 3rd frame ~= the 10 fps live camera

FALL_CLIPS = [f"fall-{i:02d}" for i in range(1, 31)]
ADL_CLIPS = [f"adl-{i:02d}" for i in range(1, 41)]


def frame_ms(frame_number: int) -> int:
    return round((frame_number - 1) * 1000 / SOURCE_FPS)


def download(clips: list[str]) -> None:
    zips_dir = RAW_DIR / "zips"
    zips_dir.mkdir(parents=True, exist_ok=True)
    targets = [f"{clip}-cam0-rgb.zip" for clip in clips] + ["urfall-cam0-falls.csv", "urfall-cam0-adls.csv"]
    for name in targets:
        destination = zips_dir / name if name.endswith(".zip") else RAW_DIR / name
        if destination.exists() and destination.stat().st_size > 0:
            continue
        url = f"{BASE_URL}/{name}"
        print(f"Downloading {url}")
        urllib.request.urlretrieve(url, destination)


def extract(clips: list[str]) -> None:
    frames_dir = RAW_DIR / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for clip in clips:
        target = frames_dir / clip
        if target.exists() and any(target.rglob("*.png")):
            continue
        archive = RAW_DIR / "zips" / f"{clip}-cam0-rgb.zip"
        print(f"Extracting {archive.name}")
        with zipfile.ZipFile(archive) as handle:
            handle.extractall(target)


def clip_frame_paths(clip: str) -> list[tuple[int, Path]]:
    numbered = []
    for path in (RAW_DIR / "frames" / clip).rglob("*.png"):
        digits = "".join(ch for ch in path.stem.rsplit("-", 1)[-1] if ch.isdigit())
        if digits:
            numbered.append((int(digits), path))
    numbered.sort()
    return numbered


def extract_pose(clips: list[str], pose_config: dict) -> None:
    import cv2
    import mediapipe as mp

    POSE_DIR.mkdir(parents=True, exist_ok=True)
    for clip in clips:
        output = POSE_DIR / f"{clip}.jsonl"
        if output.exists() and output.stat().st_size > 0:
            continue
        frames = [(number, path) for number, path in clip_frame_paths(clip) if (number - 1) % FRAME_STEP == 0]
        if not frames:
            print(f"No frames found for {clip}, skipping")
            continue
        print(f"Pose extraction: {clip} ({len(frames)} frames)")
        # VIDEO mode needs monotonic timestamps per landmarker, so each clip
        # gets a fresh one.
        landmarker, landmark_names = create_landmarker(pose_config)
        rows = []
        try:
            for index, (number, path) in enumerate(frames):
                bgr = cv2.imread(str(path))
                if bgr is None:
                    continue
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                timestamp_ms = frame_ms(number)
                result = landmarker.detect_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), timestamp_ms
                )
                landmarks = []
                if result.pose_landmarks:
                    landmarks = [
                        {
                            "name": name,
                            "x": round(lm.x, 4),
                            "y": round(lm.y, 4),
                            "z": round(lm.z, 4),
                            "visibility": round(lm.visibility or 0.0, 3),
                        }
                        for name, lm in zip(landmark_names, result.pose_landmarks[0])
                    ]
                rows.append(
                    {
                        "clip_id": clip,
                        "frame_index": index,
                        "timestamp_ms": timestamp_ms,
                        "source_frame": number,
                        "pose_model": "mediapipe_pose_landmarker_full",
                        "landmarks": landmarks,
                    }
                )
        finally:
            landmarker.close()
        with output.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")


def write_labels(clips: list[str]) -> None:
    impacts: dict[str, int] = {}
    fall_starts: dict[str, int] = {}
    with (RAW_DIR / "urfall-cam0-falls.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.reader(handle):
            if len(row) < 3:
                continue
            clip, frame_number, label = row[0].strip(), int(row[1]), row[2].strip()
            if label == "0" and clip not in fall_starts:
                fall_starts[clip] = frame_number
            if label == "1" and clip not in impacts:
                impacts[clip] = frame_number

    # ADLs whose frame labels contain lying/transition frames are deliberate
    # floor-level activities; at home these are bed/couch-zone suppressed, so
    # the scorer reports them separately from clean-activity false positives.
    lying_adls: set[str] = set()
    adls_csv = RAW_DIR / "urfall-cam0-adls.csv"
    if adls_csv.exists():
        with adls_csv.open(newline="", encoding="utf-8") as handle:
            for row in csv.reader(handle):
                if len(row) >= 3 and row[2].strip() in {"0", "1"}:
                    lying_adls.add(row[0].strip())

    LABELS_DIR.mkdir(parents=True, exist_ok=True)
    with (LABELS_DIR / "clips.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["clip_id", "primary_label", "scenario", "notes"])
        for clip in clips:
            if clip.startswith("fall-"):
                writer.writerow([clip, "fall", "fall_urfd_staged", "URFD cam0 staged fall"])
            elif clip in lying_adls:
                writer.writerow([clip, "no_fall", "lie_down", "URFD ADL with lying-labeled frames"])
            else:
                writer.writerow([clip, "no_fall", "adl_upright", "URFD cam0 activity of daily living"])
    with (LABELS_DIR / "events.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["event_id", "clip_id", "event_label", "start_ms", "impact_ms", "end_ms", "confidence", "notes"])
        for clip in clips:
            if clip not in impacts:
                continue
            start = frame_ms(fall_starts[clip]) if clip in fall_starts else ""
            writer.writerow(
                [
                    f"urfd_{clip}_impact",
                    clip,
                    "impact_or_floor_contact",
                    start,
                    frame_ms(impacts[clip]),
                    "",
                    1.0,
                    "first lying-labeled frame in urfall-cam0-falls.csv",
                ]
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="URFD cam0 benchmark for the fall detection pipeline.")
    parser.add_argument("--zones", default=str(REPO / "config" / "zones.urfd.json"))
    parser.add_argument("--rules", default=str(REPO / "config" / "monitoring-rules.example.json"))
    parser.add_argument("--out", default=str(REPO / "detector_runs" / "urfd_baseline"))
    parser.add_argument("--limit", type=int, help="Only the first N falls and N ADLs (smoke test).")
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--skip-pose", action="store_true", help="Reuse cached pose JSONL; just re-score.")
    parser.add_argument("--detect-window-s", type=float, default=10.0)
    parser.add_argument(
        "--pad-tail-s",
        type=float,
        default=60.0,
        help="URFD clips end seconds after impact; padding lets escalation rules finish.",
    )
    args = parser.parse_args()

    falls = FALL_CLIPS[: args.limit] if args.limit else FALL_CLIPS
    adls = ADL_CLIPS[: args.limit] if args.limit else ADL_CLIPS
    clips = falls + adls

    zones_config = load_zones_config(args.zones)
    if not args.skip_pose:
        if not args.skip_download:
            download(clips)
        extract(clips)
        extract_pose(clips, zones_config.get("pose", {}))
    write_labels(clips)

    pose_paths = [POSE_DIR / f"{clip}.jsonl" for clip in clips]
    pose_paths = [path for path in pose_paths if path.exists() and path.stat().st_size > 0]
    metrics = run_replay(
        collect_pose_paths([str(path) for path in pose_paths]),
        zones_config,
        load_rules(args.rules),
        LABELS_DIR,
        args.out,
        detect_window_s=args.detect_window_s,
        pad_tail_s=args.pad_tail_s,
    )
    print(json.dumps(metrics, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
