from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path

from .event_io import append_csv_row

REVIEW_LOG_FIELDNAMES = [
    "run_id",
    "clip_id",
    "predicted_label",
    "truth_label",
    "alert_time_ms",
    "decision_latency_s",
    "review_label",
    "severity",
    "reason_code",
    "ack_time_ms",
    "responder",
    "reviewer",
    "notes",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    parser = argparse.ArgumentParser(description="Morning review of last night's transitions and alerts.")
    parser.add_argument("--log-dir", default="detector_runs/live")
    parser.add_argument("--date", default=datetime.now().strftime("%Y%m%d"), help="Log date as YYYYMMDD.")
    parser.add_argument(
        "--append-review-log",
        action="store_true",
        help="Append one placeholder row per delivered alert to data/labels/review_log.csv for manual labeling.",
    )
    args = parser.parse_args()

    log_dir = Path(args.log_dir)
    transitions = read_csv(log_dir / f"transitions_{args.date}.csv")
    decisions = read_csv(log_dir / f"decisions_{args.date}.csv")

    print(f"=== Transitions for {args.date} ({len(transitions)}) ===")
    for row in transitions:
        print(
            f"{row['start_time_local']} -> {row['end_time_local']}  "
            f"{row['from_state']:>20} -> {row['to_state']:<20} "
            f"{float(row['duration_s'] or 0):7.0f}s  {row['reason_codes']}"
        )

    alerts = [
        row
        for row in decisions
        if row.get("severity") in {"low", "urgent"} and "alert_cooldown" not in (row.get("suppressions") or "")
    ]
    print(f"\n=== Delivered alerts ({len(alerts)}) ===")
    for row in alerts:
        print(f"{row['timestamp_ms']:>15}  {row['severity']:>6}  {row['state']:<20} {row['reason_codes']}")

    urgent = sum(1 for row in alerts if row["severity"] == "urgent")
    print(f"\nSummary: {len(transitions)} transitions, {len(alerts)} delivered alerts ({urgent} urgent).")

    states = {row["to_state"] for row in transitions}
    if "offline_or_blind" in states:
        print("Warning: monitoring went offline_or_blind at least once. Check sensor/camera health.")

    if args.append_review_log and alerts:
        review_path = Path("data/labels/review_log.csv")
        for row in alerts:
            append_csv_row(
                review_path,
                REVIEW_LOG_FIELDNAMES,
                {
                    "run_id": f"live_{args.date}",
                    "clip_id": "",
                    "predicted_label": row["state"],
                    "truth_label": "",
                    "alert_time_ms": row["timestamp_ms"],
                    "decision_latency_s": "",
                    "review_label": "uncertain",
                    "severity": row["severity"],
                    "reason_code": row["reason_codes"],
                    "ack_time_ms": "",
                    "responder": "",
                    "reviewer": "",
                    "notes": json.dumps({"debug": row.get("debug", "")})[:200],
                },
            )
        print(f"Appended {len(alerts)} placeholder rows to {review_path}. Fill in review_label after checking.")


if __name__ == "__main__":
    main()
