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

from .alert_lifecycle import AlertLifecycle, LIFECYCLE_EVENT_NAMES
from .alerts import build_alert_payload
from .baselines import apply_baselines
from .clock import TICK_EVENT_NAME, make_tick, tick_interval_s
from .config_validation import (
    ConfigValidationError,
    load_sensors_config,
    read_json_object,
    validate_deployment_bundle,
    validate_rules,
    validate_zones,
)
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

ALERT_LIFECYCLE_FIELDNAMES = [
    "event_type",
    "alert_id",
    "alert_timestamp_ms",
    "event_timestamp_ms",
    "severity",
    "state",
    "reason_codes",
    "escalation_stage",
    "target",
    "acknowledged_at_ms",
    "acknowledged_by",
    "resolved_at_ms",
    "resolved_by",
    "resolution",
    "last_zone",
    "sensor_health",
    "incident_age_s",
]


def alert_topic(topic_prefix: str, notifications_enabled: bool) -> str:
    suffix = "alerts" if notifications_enabled else "alerts/observed"
    return f"{topic_prefix}/{suffix}"


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
        alert_id=str(payload.get("alert_id") or ""),
        actor_id=str(payload.get("actor_id") or payload.get("responder") or ""),
        outcome=str(payload.get("outcome") or payload.get("resolution") or ""),
    )


@dataclass
class LiveSession:
    """Feeds normalized events into the state machine and fans decisions out
    to logs, a retained state topic, and the alert pipeline."""

    machine: NightSafetyStateMachine
    log_dir: Path
    on_state: Callable[[dict[str, Any]], None] = lambda payload: None
    on_alert: Callable[[dict[str, Any]], None] = lambda payload: None
    on_alert_status: Callable[[dict[str, Any]], None] = lambda payload: None
    allowed_sensor_ids: set[str] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _state_since_local: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    _state_since_ms: int | None = None
    _last_zone: str = ""
    _alert_lifecycle: AlertLifecycle = field(init=False)

    def __post_init__(self) -> None:
        self._alert_lifecycle = AlertLifecycle(self.log_dir / "alert_state.json", self.machine.rules)

    def handle_payload(self, payload: dict[str, Any]) -> DetectorDecision:
        return self.handle_event(normalize_payload(payload))

    def tick(self) -> DetectorDecision:
        now = datetime.now()
        return self.handle_event(make_tick(int(time.time() * 1000), now.isoformat(timespec="seconds")))

    def handle_event(self, event: NormalizedEvent) -> DetectorDecision:
        with self._lock:
            if (
                self.allowed_sensor_ids is not None
                and event.event_name not in LIFECYCLE_EVENT_NAMES | {TICK_EVENT_NAME}
                and event.sensor_id not in self.allowed_sensor_ids
            ):
                raise ValueError(f"unknown sensor_id: {event.sensor_id}")
            if event.zone_id:
                self._last_zone = event.zone_id
            if event.event_name in LIFECYCLE_EVENT_NAMES:
                return self._handle_lifecycle_event(event)

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
                delivery = self._alert_lifecycle.open_alert(
                    alert,
                    decision,
                    event,
                    last_zone=self._last_zone,
                )
                if delivery:
                    self._emit_alert(delivery)
            for escalation in self._alert_lifecycle.due_escalations(event.timestamp_ms):
                self._emit_alert(escalation)
            return decision

    def _handle_lifecycle_event(self, event: NormalizedEvent) -> DetectorDecision:
        if event.event_name == "caregiver_acknowledged":
            status = self._alert_lifecycle.acknowledge(event)
        else:
            status = self._alert_lifecycle.resolve(event)
        self._log_alert_lifecycle(status, event.timestamp_ms)
        self.on_alert_status(status)
        decision = DetectorDecision(
            timestamp_ms=event.timestamp_ms,
            state=self.machine.state,
            severity="info",
            score=0.0,
            confidence=event.confidence,
            reason_codes=[event.event_name],
            recommended_action="observe",
            debug={"alert_id": event.alert_id, "actor_id": event.actor_id, "outcome": event.outcome},
        )
        append_decision_csv(self._daily_path("decisions"), decision)
        return decision

    def _emit_alert(self, payload: dict[str, Any]) -> None:
        self._log_alert_lifecycle(payload, int(payload.get("event_timestamp_ms") or 0))
        self.on_alert(payload)

    def _log_alert_lifecycle(self, payload: dict[str, Any], event_timestamp_ms: int) -> None:
        row = {name: payload.get(name, "") for name in ALERT_LIFECYCLE_FIELDNAMES}
        row["event_timestamp_ms"] = event_timestamp_ms
        reason_codes = payload.get("reason_codes", [])
        row["reason_codes"] = "|".join(reason_codes) if isinstance(reason_codes, list) else reason_codes
        append_csv_row(self._daily_path("alert_lifecycle"), ALERT_LIFECYCLE_FIELDNAMES, row)

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
    rules = apply_baselines(load_rules(args.rules), args.baselines)
    validate_rules(rules)
    configured_sensor_ids: set[str] = set()
    critical_sensor_ids = set(args.critical_sensor or [])
    sensors = None
    zones = None
    if args.sensors:
        sensors = load_sensors_config(args.sensors)
        configured_sensor_ids = {sensor["sensor_id"] for sensor in sensors["sensors"]}
        critical_sensor_ids.update(sensor["sensor_id"] for sensor in sensors["sensors"] if sensor["critical"])
    if args.zones:
        zones = read_json_object(args.zones, "zones")
        validate_zones(zones)
    if args.notifications_enabled:
        missing = [name for name, value in (("--sensors", sensors), ("--zones", zones)) if value is None]
        if missing:
            raise ConfigValidationError(
                [f"notification mode requires {' and '.join(missing)} household configuration"]
            )
        critical_sensor_ids = validate_deployment_bundle(
            rules,
            sensors,
            zones,
            paths=[args.rules, args.sensors, args.zones],
        ) | set(args.critical_sensor or [])
    if configured_sensor_ids:
        unknown = sorted(set(args.critical_sensor or []) - configured_sensor_ids)
        if unknown:
            raise ConfigValidationError([f"--critical-sensor is not in the sensors config: {', '.join(unknown)}"])

    import paho.mqtt.client as mqtt

    machine = NightSafetyStateMachine(rules, critical_sensor_ids=critical_sensor_ids)
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
        on_alert=lambda payload: client.publish(
            alert_topic(prefix, args.notifications_enabled),
            json.dumps(payload),
            qos=1,
        ),
        on_alert_status=lambda payload: client.publish(f"{prefix}/alerts/status", json.dumps(payload), qos=1),
        allowed_sensor_ids=configured_sensor_ids or None,
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
    parser.add_argument("--sensors", help="Sensor inventory JSON; required when notifications are enabled.")
    parser.add_argument("--zones", help="Camera zones JSON; required when notifications are enabled.")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--username", help="MQTT username; falls back to MQTT_USERNAME env var.")
    parser.add_argument("--password", help="MQTT password; falls back to MQTT_PASSWORD env var.")
    parser.add_argument("--topic-prefix", default="senior-night")
    parser.add_argument("--log-dir", default="detector_runs/live")
    parser.add_argument("--baselines", default="data/baselines", help="Directory with personal baseline JSON files.")
    parser.add_argument("--critical-sensor", action="append", help="Sensor id that must stay fresh; repeatable.")
    parser.add_argument(
        "--notifications-enabled",
        action="store_true",
        help="Publish caregiver notifications to <prefix>/alerts. Without this flag they go to alerts/observed only.",
    )
    args = parser.parse_args()
    try:
        run_bridge(args)
    except ConfigValidationError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
