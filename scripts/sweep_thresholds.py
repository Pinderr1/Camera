"""Sweep detection thresholds over the cached replay corpus and pick an operating point.

Selection is lexicographic: URFD fall recall first, then fewest URFD
possible_fall false positives, then fewest live-clip false positives, then
fewest URFD urgent false positives, then lowest detection latency.

Usage (from repo root):
    python scripts/sweep_thresholds.py --out detector_runs/tuning
    python scripts/sweep_thresholds.py --apply   # also write winner into configs
"""

from __future__ import annotations

import argparse
import copy
import csv
import itertools
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from senior_safety.pose_extractor import load_zones_config
from senior_safety.pose_replay import collect_pose_paths, run_replay
from senior_safety.state_machine import load_rules

GRID = {
    "rapid_drop_min_vy_bl": [1.2, 1.5, 1.8],
    "rapid_drop_min_total_bl": [0.7, 0.9, 1.1],
    "fall_corroborate_torso_deg": [45, 55],
    "floor_level_min_s": [2.0, 3.0],
}

SWEEP_FIELDNAMES = [
    *GRID.keys(),
    "urfd_fall_recall",
    "urfd_fall_detected",
    "urfd_false_positive_clips",
    "urfd_urgent_false_clips",
    "urfd_detect_latency_p95_s",
    "live_false_positive_clips",
    "live_urgent_false_clips",
]


def apply_overrides(zones_config: dict, overrides: dict) -> dict:
    updated = copy.deepcopy(zones_config)
    updated.setdefault("events", {}).update(overrides)
    return updated


def evaluate(combo: dict, corpora: list[dict], rules: dict, out_dir: Path) -> dict:
    row = dict(combo)
    for corpus in corpora:
        metrics = run_replay(
            corpus["paths"],
            apply_overrides(corpus["zones"], combo),
            rules,
            corpus["labels"],
            out_dir / corpus["name"],
            pad_tail_s=corpus["pad_tail_s"],
        )
        prefix = corpus["name"]
        row[f"{prefix}_fall_recall"] = metrics["fall_recall"]
        row[f"{prefix}_fall_detected"] = metrics["fall_detected"]
        row[f"{prefix}_false_positive_clips"] = metrics["false_positive_clips"]
        row[f"{prefix}_urgent_false_clips"] = metrics["urgent_false_clips"]
        row[f"{prefix}_detect_latency_p95_s"] = metrics["detect_latency_p95_s"]
    return row


def sort_key(row: dict):
    return (
        -(row.get("urfd_fall_recall") or 0.0),
        row.get("urfd_false_positive_clips", 999),
        row.get("live_false_positive_clips", 999),
        row.get("urfd_urgent_false_clips", 999),
        row.get("urfd_detect_latency_p95_s") or 99.0,
    )


def write_winner_into_configs(winner: dict) -> None:
    for config_path in (REPO / "config" / "zones.example.json", REPO / "config" / "zones.urfd.json"):
        config = json.loads(config_path.read_text(encoding="utf-8"))
        for key in GRID:
            config["events"][key] = winner[key]
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        print(f"Updated {config_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Threshold sweep over the cached pose replay corpus.")
    parser.add_argument("--rules", default=str(REPO / "config" / "monitoring-rules.example.json"))
    parser.add_argument("--out", default=str(REPO / "detector_runs" / "tuning"))
    parser.add_argument("--apply", action="store_true", help="Write the winning thresholds into the zones configs.")
    args = parser.parse_args()

    rules = load_rules(args.rules)
    corpora = [
        {
            "name": "urfd",
            "paths": collect_pose_paths([str(REPO / "data" / "processed" / "pose" / "urfd")]),
            "zones": load_zones_config(REPO / "config" / "zones.urfd.json"),
            "labels": REPO / "data" / "labels" / "urfd",
            "pad_tail_s": 60.0,
        },
        {
            "name": "live",
            "paths": collect_pose_paths([str(REPO / "data" / "processed" / "pose" / "cam01")]),
            "zones": load_zones_config(REPO / "config" / "zones.example.json"),
            "labels": REPO / "data" / "labels",
            "pad_tail_s": 0.0,
        },
    ]

    out_dir = Path(args.out)
    combos = [dict(zip(GRID.keys(), values)) for values in itertools.product(*GRID.values())]
    rows = []
    for index, combo in enumerate(combos, start=1):
        row = evaluate(combo, corpora, rules, out_dir / "runs" / f"combo_{index:03d}")
        rows.append(row)
        print(
            f"[{index}/{len(combos)}] {combo} -> recall {row['urfd_fall_recall']} "
            f"fp {row['urfd_false_positive_clips']} live_fp {row['live_false_positive_clips']}"
        )

    rows.sort(key=sort_key)
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "sweep.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SWEEP_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in SWEEP_FIELDNAMES})

    winner = rows[0]
    print("\nBest operating point:")
    print(json.dumps({key: winner[key] for key in SWEEP_FIELDNAMES}, indent=2))
    if args.apply:
        write_winner_into_configs({key: winner[key] for key in GRID})
    else:
        print("Re-run with --apply to write these thresholds into the zones configs.")


if __name__ == "__main__":
    main()
