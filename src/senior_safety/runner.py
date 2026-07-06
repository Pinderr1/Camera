from __future__ import annotations

import argparse
import json
from pathlib import Path

from .event_io import read_sensor_events, write_decisions_csv
from .omnifall_replay import read_omnifall_segments_csv
from .state_machine import NightSafetyStateMachine, load_rules


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the senior night safety state machine.")
    parser.add_argument("--rules", default="config/monitoring-rules.example.json")
    parser.add_argument("--events", help="Normalized sensor_events CSV.")
    parser.add_argument("--omnifall-segments", help="OmniFall-like segment CSV export.")
    parser.add_argument("--output", default="detector_runs/decisions.csv")
    parser.add_argument("--metrics", default="detector_runs/metrics.json")
    args = parser.parse_args()

    if not args.events and not args.omnifall_segments:
        raise SystemExit("Provide --events or --omnifall-segments.")

    rules = load_rules(args.rules)
    machine = NightSafetyStateMachine(rules)
    events = read_omnifall_segments_csv(args.omnifall_segments) if args.omnifall_segments else read_sensor_events(args.events)
    decisions = [machine.process(event) for event in events]

    write_decisions_csv(args.output, decisions)
    metrics = {
        "events": len(events),
        "decisions": len(decisions),
        "urgent_alerts": sum(1 for decision in decisions if decision.severity == "urgent"),
        "low_alerts": sum(1 for decision in decisions if decision.severity == "low"),
        "final_state": decisions[-1].state if decisions else machine.state,
    }
    metrics_path = Path(args.metrics)
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(metrics, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
