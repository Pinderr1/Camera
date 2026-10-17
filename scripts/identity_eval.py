"""Measure face-template separation on labelled public footage.

  python scripts/identity_eval.py [--clip data/bench/mot16/MOT16-09] [--every 3] [--max-frames 525] [--out bench_runs/identity]

MOT16 ground truth gives every pedestrian an identity across the clip. Each
detected face is assigned to the pedestrian whose box holds it (top part of
the box, exactly one candidate), then:
- same-person pairs: faces of one pedestrian at least `--min-gap-ms` apart;
- different-person pairs: faces of two different pedestrians.
The report gives the similarity distributions and what fraction of pairs the
current thresholds would call a match, a non-match, or leave undecided.

What this is not: an enrollment test. Real enrollment uses frontal, close,
well-lit photos; MOT16 faces are small, often turned and motion-blurred, and
every pedestrian is a stranger to the model. Sentinel's per-track decision
also needs several agreeing frames (identity.min_agreeing), which this
per-pair view does not include. Never quote these numbers as recognition
accuracy for a home installation."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def load_gt(clip_dir: Path) -> dict[int, list[tuple[int, float, float, float, float]]]:
    rows: dict[int, list[tuple[int, float, float, float, float]]] = defaultdict(list)
    for line in (clip_dir / "gt.txt").read_text().splitlines():
        parts = line.split(",")
        frame, person_id, x, y, w, h, consider, cls = int(parts[0]), int(parts[1]), *map(float, parts[2:6]), int(parts[6]), int(parts[7])
        if consider == 1 and cls == 1:
            rows[frame].append((person_id, x, y, w, h))
    return rows


def collect_faces(analyzer, clip_dir: Path, *, max_frames: int = 525, every: int = 3, min_face_px: int = 40, min_score: float = 0.85,
                  fps: float = 30.0) -> list[tuple[int, int, int, list[float]]]:
    """[(person_id, frame_ms, face_px, template)] for faces that belong to exactly one labelled pedestrian."""
    import cv2

    gt = load_gt(clip_dir)
    capture = cv2.VideoCapture(str(next(clip_dir.glob("MOT16-*.mp4"))))
    faces = []
    frame = 0
    while frame < max_frames:
        ok, image = capture.read()
        if not ok:
            break
        frame += 1
        if frame % every:
            continue
        for face in analyzer.detect(image):
            size = int(min(face[2], face[3]))
            if float(face[14]) < min_score or size < min_face_px:
                continue
            cx, cy = float(face[0] + face[2] / 2), float(face[1] + face[3] / 2)
            owners = [pid for pid, x, y, w, h in gt.get(frame, []) if x <= cx <= x + w and y <= cy <= y + 0.35 * h]
            if len(owners) != 1:
                continue
            faces.append((owners[0], int(frame * 1000 / fps), size, [float(v) for v in analyzer.template(image, face)]))
    capture.release()
    return faces


def pair_scores(faces, *, min_gap_ms: int = 1000) -> tuple[list[float], list[float]]:
    same, different = [], []
    for i, (pid_a, ms_a, _, a) in enumerate(faces):
        for pid_b, ms_b, _, b in faces[i + 1:]:
            score = sum(x * y for x, y in zip(a, b))
            if pid_a == pid_b:
                if abs(ms_a - ms_b) >= min_gap_ms:
                    same.append(score)
            else:
                different.append(score)
    return same, different


def summarize(values: list[float], match: float, unknown_below: float) -> dict:
    if not values:
        return {"pairs": 0}
    ordered = sorted(values)
    pick = lambda q: round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 3)
    return {"pairs": len(values), "mean": round(sum(values) / len(values), 3), "p05": pick(0.05), "median": pick(0.5), "p95": pick(0.95),
            "match": round(sum(v >= match for v in values) / len(values), 3),
            "no_match": round(sum(v < unknown_below for v in values) / len(values), 3),
            "undecided": round(sum(unknown_below <= v < match for v in values) / len(values), 3)}


def main() -> int:
    from sentinel.identity.faces import FaceAnalyzer
    from sentinel.identity.settings import DEFAULT_DETECTOR, DEFAULT_RECOGNIZER, IdentitySettings

    parser = argparse.ArgumentParser()
    parser.add_argument("--clip", default=str(ROOT / "data/bench/mot16/MOT16-09"))
    parser.add_argument("--every", type=int, default=3)
    parser.add_argument("--max-frames", type=int, default=525)
    parser.add_argument("--min-gap-ms", type=int, default=1000)
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    settings = IdentitySettings()
    analyzer = FaceAnalyzer(ROOT / DEFAULT_DETECTOR, ROOT / DEFAULT_RECOGNIZER)
    faces = collect_faces(analyzer, Path(args.clip), max_frames=args.max_frames, every=args.every, min_face_px=settings.min_face_px)
    same, different = pair_scores(faces, min_gap_ms=args.min_gap_ms)
    report = {
        "clip": Path(args.clip).name, "frames": args.max_frames, "every": args.every, "faces": len(faces),
        "pedestrians_with_faces": len({f[0] for f in faces}), "face_px_median": sorted(f[2] for f in faces)[len(faces) // 2] if faces else None,
        "thresholds": {"match_threshold": settings.match_threshold, "unknown_below": settings.unknown_below},
        "same_person": summarize(same, settings.match_threshold, settings.unknown_below),
        "different_people": summarize(different, settings.match_threshold, settings.unknown_below),
        "note": "pairwise, small and turned faces of strangers; not enrollment-quality and not an accuracy figure",
    }
    print(json.dumps(report, indent=2))
    if args.out:
        Path(args.out).mkdir(parents=True, exist_ok=True)
        (Path(args.out) / "identity_eval.json").write_text(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
