from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .clock import TICK_EVENT_NAME, is_night_window, parse_local_datetime
from .schemas import DetectorDecision, NormalizedEvent


STATES = {
    "asleep_in_bed",
    "bed_exit",
    "walking_to_bathroom",
    "bathroom_occupied",
    "returning_to_bed",
    "returned_to_bed",
    "out_of_bed_unknown",
    "possible_fall",
    "fallen_no_motion",
    "bathroom_overstay",
    "unusual_inactivity",
    "needs_check",
    "urgent_alert",
    "offline_or_blind",
}


def load_rules(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


@dataclass
class SensorHealth:
    last_seen_ms: int
    critical: bool
    online: bool = True


@dataclass
class NightSafetyStateMachine:
    rules: dict[str, Any]
    critical_sensor_ids: set[str] = field(default_factory=set)
    state: str = "asleep_in_bed"
    active_trip_start_ms: int | None = None
    bed_unoccupied_since_ms: int | None = None
    bathroom_occupied_since_ms: int | None = None
    fall_suspected_since_ms: int | None = None
    floor_level_since_ms: int | None = None
    no_motion_since_ms: int | None = None
    last_motion_ms: int | None = None
    night_window_active: bool = True
    sensor_health: dict[str, SensorHealth] = field(default_factory=dict)
    last_alert_ms: dict[tuple[str, str], int] = field(default_factory=dict)

    def process(self, event: NormalizedEvent) -> DetectorDecision:
        self._update_night_window(event)
        if event.event_name != TICK_EVENT_NAME:
            self._record_sensor_health(event)

        reason_codes: list[str] = []
        suppressions: list[str] = []
        severity = "none"
        action = "observe"
        score = 0.0

        # Manual signals must work even while sensors look stale or offline.
        if event.event_name == "manual_help_pressed" and bool(event.value):
            self.state = "urgent_alert"
            return self._decision(
                event,
                "urgent",
                1.0,
                ["manual_help_pressed"],
                "urgent_caregiver_check",
                suppressions,
            )

        if event.event_name == "manual_cancel_pressed" and bool(event.value):
            self._reset_active_incident()
            self.last_alert_ms.clear()
            self.state = "returned_to_bed" if self._bed_occupied_known() else "out_of_bed_unknown"
            return self._decision(event, severity, score, ["manual_cancel_pressed"], "observe", suppressions)

        stale_decision = self._stale_sensor_decision(event.timestamp_ms)
        if stale_decision:
            return stale_decision

        if event.event_name == "sensor_offline" and bool(event.value):
            self.state = "offline_or_blind"
            return self._decision(event, "low", 0.7, ["sensor_offline"], "notify_caregiver", suppressions)

        if event.event_name == "sensor_online" and bool(event.value):
            if self.state == "offline_or_blind":
                self.state = "out_of_bed_unknown" if self.active_trip_start_ms is not None else "asleep_in_bed"
            return self._decision(event, severity, score, [], action, suppressions)

        if event.event_name in {"person_present", "route_motion"}:
            if bool(event.value):
                self.last_motion_ms = event.timestamp_ms
                if self.active_trip_start_ms is not None and self.state in {"bed_exit", "out_of_bed_unknown", "returned_to_bed"}:
                    self.state = "walking_to_bathroom"
                    severity = "info"
            else:
                if self.active_trip_start_ms is not None and self.state == "walking_to_bathroom":
                    self.state = "out_of_bed_unknown"
                    reason_codes.append("route_motion_missing")

        if event.event_name == "bed_occupied":
            if bool(event.value):
                self.bed_unoccupied_since_ms = None
                if self.active_trip_start_ms is not None:
                    self.state = "returned_to_bed"
                    self.active_trip_start_ms = None
                    self.bathroom_occupied_since_ms = None
                    self._reset_active_incident()
                    self.last_alert_ms.clear()
                    severity = "info"
                else:
                    self.state = "asleep_in_bed"
            else:
                if self.bed_unoccupied_since_ms is None:
                    self.bed_unoccupied_since_ms = event.timestamp_ms
                if self.active_trip_start_ms is None:
                    self.active_trip_start_ms = event.timestamp_ms
                if self.state in {"asleep_in_bed", "returned_to_bed"}:
                    self.state = "bed_exit"
                    severity = "info"
                    score = max(score, 0.25)
                    reason_codes.append("outside_bed")

        if event.event_name == "bathroom_occupied":
            if bool(event.value):
                if self.bathroom_occupied_since_ms is None:
                    self.bathroom_occupied_since_ms = event.timestamp_ms
                if self.active_trip_start_ms is not None:
                    self.state = "bathroom_occupied"
                    severity = "info"
            else:
                if self.state == "bathroom_occupied":
                    self.state = "returning_to_bed"
                    severity = "info"
                self.bathroom_occupied_since_ms = None

        if event.event_name == "door_open" and bool(event.value) and self.active_trip_start_ms is not None:
            if self.state in {"bed_exit", "walking_to_bathroom", "out_of_bed_unknown"}:
                self.state = "walking_to_bathroom"
                severity = "info"

        if event.event_name == "fall_suspected" and bool(event.value):
            if self.fall_suspected_since_ms is None:
                self.fall_suspected_since_ms = event.timestamp_ms
            self.state = "possible_fall"
            severity = "low"
            action = "soft_check"
            score = max(score, 0.7)
            reason_codes.append("rapid_drop")

        if event.event_name == "floor_level_posture" and bool(event.value):
            if self.floor_level_since_ms is None:
                self.floor_level_since_ms = event.timestamp_ms
            if self.state not in {"fallen_no_motion", "urgent_alert"}:
                self.state = "possible_fall"
            severity = "low"
            action = "soft_check"
            score = max(score, 0.7)
            reason_codes.append("floor_level_posture")

        if event.event_name == "no_motion":
            if bool(event.value):
                if self.no_motion_since_ms is None:
                    self.no_motion_since_ms = event.timestamp_ms
                reason_codes.append(self._no_motion_reason(event.timestamp_ms))
            else:
                self.no_motion_since_ms = None
                if self.state in {"possible_fall", "fallen_no_motion"}:
                    self.state = "out_of_bed_unknown" if self.active_trip_start_ms is not None else "asleep_in_bed"

        fall_alert = self._apply_fall_persistence(event.timestamp_ms)
        if fall_alert:
            self.state, severity, action, score = fall_alert
            reason_codes.extend(["floor_level_posture", self._no_motion_reason(event.timestamp_ms), "outside_bed"])

        routine_alert = self._apply_routine_timing(event.timestamp_ms)
        if routine_alert and self.state not in {"urgent_alert", "fallen_no_motion"}:
            self.state, severity, action, score, routine_reasons = routine_alert
            reason_codes.extend(routine_reasons)

        reason_codes = _dedupe(reason_codes)
        return self._decision(event, severity, score, reason_codes, action, suppressions)

    def _update_night_window(self, event: NormalizedEvent) -> None:
        local_dt = parse_local_datetime(event.event_time_local)
        if local_dt is None:
            return
        active = is_night_window(self.rules, local_dt)
        if active != self.night_window_active:
            # Entering/leaving the night window restarts routine timers so a
            # normal daytime bed exit does not instantly count as no-return.
            self.bed_unoccupied_since_ms = None
            self.active_trip_start_ms = None
            self.bathroom_occupied_since_ms = None
        self.night_window_active = active

    def _record_sensor_health(self, event: NormalizedEvent) -> None:
        critical = event.sensor_id in self.critical_sensor_ids
        online = event.network_ok and event.battery_ok and not (
            event.event_name == "sensor_offline" and bool(event.value)
        )
        self.sensor_health[event.sensor_id] = SensorHealth(event.timestamp_ms, critical, online)

    def _stale_sensor_decision(self, now_ms: int) -> DetectorDecision | None:
        threshold_s = self.rules["thresholds"]["sensor_health"]["critical_sensor_offline_after_s"]
        threshold_ms = threshold_s * 1000
        for sensor_id, health in self.sensor_health.items():
            # HA/ESPHome binary sensors are often event-driven; no state change is
            # not the same as stale. Only explicitly configured heartbeat sensors
            # use elapsed-time staleness here.
            if health.critical and (not health.online or now_ms - health.last_seen_ms > threshold_ms):
                self.state = "offline_or_blind"
                return DetectorDecision(
                    timestamp_ms=now_ms,
                    state=self.state,
                    severity="low",
                    score=0.7,
                    confidence=0.8,
                    reason_codes=["sensor_offline"],
                    recommended_action="notify_caregiver",
                    suppressions=self._cooldown_suppressions("low", now_ms, []),
                    debug=self._debug(now_ms) | {"offline_sensor_id": sensor_id},
                )
        return None

    def _cooldown_suppressions(self, severity: str, now_ms: int, suppressions: list[str]) -> list[str]:
        if severity not in {"low", "urgent"}:
            return suppressions
        cooldown_s = self.rules.get("suppression", {}).get("realert_cooldown_s", 600)
        key = (self.state, severity)
        last = self.last_alert_ms.get(key)
        if last is not None and now_ms - last < cooldown_s * 1000:
            return suppressions + ["alert_cooldown"]
        self.last_alert_ms[key] = now_ms
        return suppressions

    def _apply_fall_persistence(self, now_ms: int) -> tuple[str, str, str, float] | None:
        if self.floor_level_since_ms is None or self.no_motion_since_ms is None:
            return None
        fall_threshold = self.rules["thresholds"]["possible_fall"]
        floor_s = _elapsed_s(self.floor_level_since_ms, now_ms)
        no_motion_s = _elapsed_s(self.no_motion_since_ms, now_ms)
        if floor_s >= fall_threshold["floor_level_posture_min_s"]:
            self.state = "fallen_no_motion"
        if no_motion_s >= fall_threshold["urgent_after_no_motion_s"]:
            return "urgent_alert", "urgent", "urgent_caregiver_check", 1.0
        if self.state == "fallen_no_motion":
            return "fallen_no_motion", "low", "soft_check", 0.85
        return None

    def _apply_routine_timing(self, now_ms: int) -> tuple[str, str, str, float, list[str]] | None:
        if self.bathroom_occupied_since_ms is not None:
            duration_s = _elapsed_s(self.bathroom_occupied_since_ms, now_ms)
            thresholds = self.rules["thresholds"]["bathroom_overstay"]
            if duration_s >= thresholds["urgent_default_s"]:
                return "urgent_alert", "urgent", "urgent_caregiver_check", 0.9, ["bathroom_overstay"]
            if duration_s >= thresholds["low_notice_default_s"]:
                return "bathroom_overstay", "low", "notify_caregiver", 0.65, ["bathroom_overstay"]

        if self.bed_unoccupied_since_ms is not None and self.night_window_active:
            duration_s = _elapsed_s(self.bed_unoccupied_since_ms, now_ms)
            thresholds = self.rules["thresholds"]["bed_exit_no_return"]
            if duration_s >= thresholds["urgent_after_s"]:
                return "urgent_alert", "urgent", "urgent_caregiver_check", 0.9, ["bed_exit_no_return"]
            if duration_s >= thresholds["low_notice_after_s"]:
                return "needs_check", "low", "notify_caregiver", 0.6, ["bed_exit_no_return"]
        return None

    def _no_motion_reason(self, now_ms: int) -> str:
        if self.no_motion_since_ms is None:
            return "no_motion_30s"
        elapsed_s = _elapsed_s(self.no_motion_since_ms, now_ms)
        return "no_motion_45s" if elapsed_s >= 45 else "no_motion_30s"

    def _reset_active_incident(self) -> None:
        self.fall_suspected_since_ms = None
        self.floor_level_since_ms = None
        self.no_motion_since_ms = None

    def _bed_occupied_known(self) -> bool:
        return self.bed_unoccupied_since_ms is None

    def _decision(
        self,
        event: NormalizedEvent,
        severity: str,
        score: float,
        reason_codes: list[str],
        action: str,
        suppressions: list[str],
    ) -> DetectorDecision:
        if event.event_name != "manual_help_pressed":
            suppressions = self._cooldown_suppressions(severity, event.timestamp_ms, suppressions)
        return DetectorDecision(
            timestamp_ms=event.timestamp_ms,
            state=self.state,
            severity=severity,
            score=round(score, 3),
            confidence=event.confidence,
            reason_codes=reason_codes,
            recommended_action=action,
            suppressions=suppressions,
            debug=self._debug(event.timestamp_ms),
        )

    def _debug(self, now_ms: int) -> dict[str, Any]:
        return {
            "bed_unoccupied_s": _elapsed_s(self.bed_unoccupied_since_ms, now_ms),
            "bathroom_occupied_s": _elapsed_s(self.bathroom_occupied_since_ms, now_ms),
            "fall_suspected_s": _elapsed_s(self.fall_suspected_since_ms, now_ms),
            "floor_level_s": _elapsed_s(self.floor_level_since_ms, now_ms),
            "no_motion_s": _elapsed_s(self.no_motion_since_ms, now_ms),
            "sensor_health": "ok" if self.state != "offline_or_blind" else "offline_or_blind",
        }


def _elapsed_s(start_ms: int | None, now_ms: int) -> float:
    if start_ms is None:
        return 0.0
    return max(0.0, (now_ms - start_ms) / 1000.0)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result
