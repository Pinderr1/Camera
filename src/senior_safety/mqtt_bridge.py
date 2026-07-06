from __future__ import annotations

import argparse
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .alerts import build_alert_payload
from .baselines import apply_baselines
from .clock import TICK_EVENT_NAME, make_tick, tick_interval_s
from .event_io import append_csv_row, append_decision_csv, parse_boolish
from .schemas import DetectorDecision, NormalizedEvent
from .state_machine import NightSafetyStateMachine, load_rules

TRANSITION_FIELDNAMES = [
    "transition_id",
    "start_time_local",
    "end_time_local",
    "from_state",
    "to_state",
    "trigger_event_ids",
    "duration_s",
    "confidence",
    "reason_codes",
    "review_status",
    "notes",
]


def normalize_payload(payload: dict[str, Any], now_ms: int | None = None) -> NormalizedEvent:
    timestamp_ms = int(payload.get("timestamp_ms") or now_ms or time.time() * 1000)
    return NormalizedEvent(
        event_id=str(payload.get("event_id") or f"evt_{timestamp_ms}"),
        sensor_id=str(payload.get("sensor_id") or "unknown"),
        sensor_type=str(payload.get("sensor_type") or "unknown"),
        room=str(payload.get("room") or ""),
        zone_id=str(payload.get("zone_id") or ""),
        timestamp_ms=timestamp_ms,
        event_time_local=str(payload.get("event_time_local") or datetime.now().isoformat(timespec="seconds")),
        event_name=str(payload["event_name"]).strip(),
        value=parse_boolish(payload.get("value")),
        confidence=float(payload.get("confidence") or 1.0),
        battery_ok=bool(parse_boolish(payload.get("battery_ok", True))),
        network_ok=bool(parse_boolish(payload.get("network_ok", True))),
        notes=str(payload.get("notes") or ""),
    )


@dataclass
class LiveSession:
    """Feeds normalized events into the state machine and fans decisions out
    to logs, a retained state topic, and the alert pipeline."""

    machine: NightSafetyStateMachine
    log_dir: Path
    on_state: Callable[[dict[str, Any]], None] = lambda payload: None
    on_alert: Callable[[dict[str, Any]], None] = lambda payload: None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _state_since_local: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    _state_since_ms: int | None = None

    def handle_payload(self, payload: dict[str, Any]) -> DetectorDecision:
        return self.handle_event(normalize_payload(payload))

    def tick(self) -> DetectorDecision:
        now = datetime.now()
        return self.handle_event(make_tick(int(time.time() * 1000), now.isoformat(timespec="seconds")))

    def handle_event(self, event: NormalizedEvent) -> DetectorDecision:
        with self._lock:
            previous_state = self.machine.state
            decision = self.machine.process(event)
            state_changed = decision.state != previous_state

            if event.event_name != TICK_EVENT_NAME or state_changed or decision.severity != "none":
                append_decision_csv(self._daily_path("decisions"), decision)

            if state_changed:
                self._log_transition(previous_state, decision, event)

            self.on_state(
                {
                    "state": decision.state,
                    "severity": decision.severity,
                    "reason_codes": decision.reason_codes,
                    "recommended_action": decision.recommended_action,
                    "timestamp_ms": decision.timestamp_ms,
                    "night_window_active": self.machine.night_window_active,
                    "debug": decision.debug,
                }
            )

            alert = build_alert_payload(decision)
            if alert:
                self.on_alert(alert.as_dict())
            return decision

    def _log_transition(self, previous_state: str, decision: DetectorDecision, event: NormalizedEvent) -> None:
        end_local = event.event_time_local or datetime.now().isoformat(timespec="seconds")
        duration_s = 0.0
        if self._state_since_ms is not None:
            duration_s = max(0.0, (decision.timestamp_ms - self._state_since_ms) / 1000.0)
        append_csv_row(
            self._daily_path("transitions"),
            TRANSITION_FIELDNAMES,
            {
                "transition_id": f"tr_{decision.timestamp_ms}_{decision.state}",
                "start_time_local": self._state_since_local,
                "end_time_local": end_local,
                "from_state": previous_state,
                "to_state": decision.state,
                "trigger_event_ids": event.event_id,
                "duration_s": round(duration_s, 1),
                "confidence": decision.confidence,
                "reason_codes": "|".join(decision.reason_codes),
                "review_status": "unreviewed",
                "notes": "",
            },
        )
        self._state_since_local = end_local
        self._state_since_ms = decision.timestamp_ms

    def _daily_path(self, kind: str) -> Path:
        return self.log_dir / f"{kind}_{datetime.now():%Y%m%d}.csv"


def run_bridge(args: argparse.Namespace) -> None:
    import paho.mqtt.client as mqtt

    rules = apply_baselines(load_rules(args.rules), args.baselines)
    machine = NightSafetyStateMachine(rules, critical_sensor_ids=set(args.critical_sensor or []))
    prefix = args.topic_prefix

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    username = args.username or os.environ.get("MQTT_USERNAME")
    password = args.password or os.environ.get("MQTT_PASSWORD")
    if username:
        client.username_pw_set(username, password)
    client.will_set(f"{prefix}/bridge/status", "offline", retain=True)

    session = LiveSession(
        machine=machine,
        log_dir=Path(args.log_dir),
        on_state=lambda payload: client.publish(f"{prefix}/state", json.dumps(payload), retain=True),
        on_alert=lambda payload: client.publish(f"{prefix}/alerts", json.dumps(payload), qos=1),
    )

    def on_connect(client, userdata, flags, reason_code, properties):
        client.subscribe(f"{prefix}/events")
        client.publish(f"{prefix}/bridge/status", "online", retain=True)
        print(f"Connected to {args.host}:{args.port}, subscribed to {prefix}/events")

    def on_message(client, userdata, message):
        try:
            payload = json.loads(message.payload.decode("utf-8"))
            decision = session.handle_payload(payload)
            print(f"{payload.get('event_name')} -> {decision.state} ({decision.severity})")
        except (ValueError, KeyError) as error:
            print(f"Ignored malformed event payload: {error}")

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.host, args.port)
    client.loop_start()

    interval = tick_interval_s(rules)
    try:
        while True:
            time.sleep(interval)
            session.tick()
    except KeyboardInterrupt:
        pass
    finally:
        client.publish(f"{prefix}/bridge/status", "offline", retain=True)
        client.loop_stop()
        client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Live MQTT bridge for the senior night safety state machine.")
    parser.add_argument("--rules", default="config/monitoring-rules.example.json")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--username", help="MQTT username; falls back to MQTT_USERNAME env var.")
    parser.add_argument("--password", help="MQTT password; falls back to MQTT_PASSWORD env var.")
    parser.add_argument("--topic-prefix", default="senior-night")
    parser.add_argument("--log-dir", default="detector_runs/live")
    parser.add_argument("--baselines", default="data/baselines", help="Directory with personal baseline JSON files.")
    parser.add_argument("--critical-sensor", action="append", help="Sensor id that must stay fresh; repeatable.")
    args = parser.parse_args()
    run_bridge(args)


if __name__ == "__main__":
    main()
