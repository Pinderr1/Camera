from __future__ import annotations

import argparse
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .baselines import apply_baselines
from .clock import tick_interval_s
from .config_validation import ConfigValidationError, validate_rules, validate_zones
from .mqtt_bridge import LiveSession
from .pose_extractor import load_zones_config, make_jsonl_writer, run_camera_loop
from .state_machine import NightSafetyStateMachine, load_rules

FAST_TEST_OVERRIDES = {
    "possible_fall": {"urgent_after_no_motion_s": 20, "floor_level_posture_min_s": 2},
    "bed_exit_no_return": {"low_notice_after_s": 60, "urgent_after_s": 120},
    "bathroom_overstay": {"low_notice_default_s": 60, "urgent_default_s": 120},
}


def manual_payload(event_name: str) -> dict[str, Any]:
    return {
        "sensor_id": "pilot_keyboard",
        "sensor_type": "button",
        "room": "test",
        "zone_id": "",
        "event_name": event_name,
        "value": True,
        "timestamp_ms": int(time.time() * 1000),
        "event_time_local": datetime.now().isoformat(timespec="seconds"),
        "confidence": 1.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Single-process pilot: camera pose events feed the state machine directly, no MQTT broker."
    )
    parser.add_argument("--zones", default="config/zones.example.json")
    parser.add_argument("--rules", default="config/monitoring-rules.example.json")
    parser.add_argument("--baselines", default="data/baselines")
    parser.add_argument("--log-dir", default="detector_runs/pilot")
    parser.add_argument("--show", action="store_true", help="Preview window; q quits, h=help button, c=cancel button.")
    parser.add_argument("--no-jsonl", action="store_true")
    parser.add_argument(
        "--fast-test",
        action="store_true",
        help="Shorten alert thresholds (urgent after ~20s stillness) for staged scenario testing.",
    )
    args = parser.parse_args()

    try:
        rules = apply_baselines(load_rules(args.rules), args.baselines)
        validate_rules(rules)
        config = load_zones_config(args.zones)
        validate_zones(config, require_camera_zones=True)
    except ConfigValidationError as error:
        raise SystemExit(str(error)) from error
    if args.fast_test:
        for section, overrides in FAST_TEST_OVERRIDES.items():
            rules["thresholds"][section].update(overrides)
        print("Fast-test thresholds active: urgent after ~20s of stillness on the floor.")

    print("Calibrate polygons for this camera view; placeholders are not reliable.")

    engine_floor_s = config.get("events", {}).get("floor_level_min_s", 2.0)
    rules_floor_s = rules["thresholds"]["possible_fall"]["floor_level_posture_min_s"]
    print(
        f"Fall timing: {engine_floor_s}s floor latch (zones config) + {rules_floor_s}s persistence (rules) "
        "before escalation; urgent requires a confirmed rapid drop or "
        f"{rules['thresholds']['possible_fall']['urgent_after_no_motion_s']}s stillness."
    )
    last_printed_state = {"value": None}

    def on_state(payload: dict[str, Any]) -> None:
        if payload["state"] != last_printed_state["value"]:
            last_printed_state["value"] = payload["state"]
            reasons = ",".join(payload["reason_codes"]) or "-"
            print(f"[state] {payload['state']} (severity={payload['severity']}, reasons={reasons})")

    def on_alert(payload: dict[str, Any]) -> None:
        print(f"\n*** ALERT ({payload['severity'].upper()}) *** {payload['title']}\n    {payload['message']}\n")

    session = LiveSession(
        machine=NightSafetyStateMachine(rules),
        log_dir=Path(args.log_dir),
        on_state=on_state,
        on_alert=on_alert,
    )

    def emit(payload: dict[str, Any]) -> None:
        payload.setdefault("event_time_local", datetime.now().isoformat(timespec="seconds"))
        print(f"[event] {payload['event_name']}={payload['value']} zone={payload.get('zone_id', '')}")
        session.handle_payload(payload)

    def on_key(key: str) -> None:
        if key == "h":
            print("[key] manual help pressed")
            session.handle_payload(manual_payload("manual_help_pressed"))
        elif key == "c":
            print("[key] manual cancel pressed")
            session.handle_payload(manual_payload("manual_cancel_pressed"))

    interval = tick_interval_s(rules)

    def tick_loop() -> None:
        while True:
            time.sleep(interval)
            session.tick()

    threading.Thread(target=tick_loop, daemon=True).start()

    writer = None if args.no_jsonl else make_jsonl_writer(config)
    print(f"Pilot logs: {args.log_dir}. Review afterwards with: python -m senior_safety.morning_review --log-dir {args.log_dir}")
    run_camera_loop(config, emit, show=args.show, jsonl_writer=writer, on_key=on_key)


if __name__ == "__main__":
    main()
