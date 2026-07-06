from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

BATHROOM_BASELINE_FILE = "bathroom_duration_baseline.json"
ROUTINE_BASELINE_FILE = "nightly_routine_baseline.json"


def read_transitions(paths: list[Path]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in paths:
        with path.open(newline="", encoding="utf-8") as handle:
            rows.extend(csv.DictReader(handle))
    rows.sort(key=lambda row: row.get("start_time_local", ""))
    return rows


def _percentiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)

    def pct(p: float) -> float:
        if not ordered:
            return 0.0
        index = min(len(ordered) - 1, max(0, round(p / 100 * (len(ordered) - 1))))
        return ordered[index]

    return {"p50_s": pct(50), "p90_s": pct(90), "p95_s": pct(95), "count": len(ordered)}


def _night_key(local_text: str) -> str:
    parsed = datetime.fromisoformat(local_text)
    return (parsed - timedelta(hours=12)).date().isoformat()


def compute_baselines(rows: list[dict[str, str]]) -> dict[str, Any]:
    bathroom_durations = [
        float(row["duration_s"]) for row in rows if row.get("from_state") == "bathroom_occupied" and row.get("duration_s")
    ]

    trip_durations: list[float] = []
    exits_per_night: dict[str, int] = {}
    trip_start: datetime | None = None
    for row in rows:
        to_state = row.get("to_state")
        start_local = row.get("end_time_local") or row.get("start_time_local") or ""
        if to_state == "bed_exit" and start_local:
            trip_start = datetime.fromisoformat(start_local)
            night = _night_key(start_local)
            exits_per_night[night] = exits_per_night.get(night, 0) + 1
        elif to_state == "returned_to_bed" and trip_start is not None and start_local:
            trip_durations.append((datetime.fromisoformat(start_local) - trip_start).total_seconds())
            trip_start = None

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "bathroom_duration": _percentiles(bathroom_durations),
        "bed_trip_duration": _percentiles(trip_durations),
        "bed_exits_per_night": _percentiles([float(count) for count in exits_per_night.values()]),
        "nights_observed": len(exits_per_night),
    }


def write_baselines(baselines: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / BATHROOM_BASELINE_FILE).write_text(
        json.dumps(baselines["bathroom_duration"] | {"generated_at": baselines["generated_at"]}, indent=2),
        encoding="utf-8",
    )
    (output_dir / ROUTINE_BASELINE_FILE).write_text(json.dumps(baselines, indent=2), encoding="utf-8")


def apply_baselines(rules: dict[str, Any], baselines_dir: str | Path) -> dict[str, Any]:
    """Overlay personalized thresholds onto the rules when baselines exist
    and enough nights have been observed to trust them."""
    path = Path(baselines_dir) / ROUTINE_BASELINE_FILE
    if not path.exists():
        return rules
    baselines = json.loads(path.read_text(encoding="utf-8"))
    overstay = rules.get("thresholds", {}).get("bathroom_overstay", {})
    bathroom = baselines.get("bathroom_duration", {})
    if overstay.get("use_personal_baseline") and bathroom.get("count", 0) >= 10:
        margin = overstay.get("low_notice_after_p95_plus_s", 300)
        personalized = bathroom["p95_s"] + margin
        overstay["low_notice_default_s"] = min(overstay.get("low_notice_default_s", personalized), personalized)
        overstay["personalized_from_baseline"] = True
    return rules


def main() -> None:
    parser = argparse.ArgumentParser(description="Compute personal routine baselines from transition logs.")
    parser.add_argument("--transitions", nargs="+", default=["detector_runs/live"], help="Transition CSV files or directories.")
    parser.add_argument("--output", default="data/baselines")
    args = parser.parse_args()

    paths: list[Path] = []
    for entry in args.transitions:
        path = Path(entry)
        if path.is_dir():
            paths.extend(sorted(path.glob("transitions_*.csv")))
        elif path.exists():
            paths.append(path)
    if not paths:
        raise SystemExit("No transition CSV files found.")

    baselines = compute_baselines(read_transitions(paths))
    write_baselines(baselines, Path(args.output))
    print(json.dumps(baselines, indent=2))


if __name__ == "__main__":
    main()
