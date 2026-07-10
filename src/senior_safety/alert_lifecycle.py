from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .alerts import AlertPayload
from .schemas import DetectorDecision, NormalizedEvent


LIFECYCLE_EVENT_NAMES = {"caregiver_acknowledged", "alert_resolved"}


@dataclass
class AlertRecord:
    alert_id: str
    created_at_ms: int
    updated_at_ms: int
    severity: str
    state: str
    reason_codes: list[str]
    recommended_action: str
    acknowledge_required: bool
    last_zone: str
    sensor_health: str
    incident_age_s: float
    escalation_stage: str = "primary"
    acknowledged_at_ms: int | None = None
    acknowledged_by: str = ""
    resolved_at_ms: int | None = None
    resolved_by: str = ""
    resolution: str = ""

    @property
    def open(self) -> bool:
        return self.resolved_at_ms is None


class AlertLifecycle:
    """Tracks alert delivery, acknowledgement, and escalation across restarts."""

    def __init__(self, state_path: str | Path, rules: dict[str, Any]) -> None:
        self.state_path = Path(state_path)
        self.rules = rules
        self.records: dict[str, AlertRecord] = {}
        self.seen_fingerprints: list[str] = []
        self._load()

    def open_alert(
        self,
        alert: AlertPayload,
        decision: DetectorDecision,
        event: NormalizedEvent,
        *,
        last_zone: str,
    ) -> dict[str, Any] | None:
        fingerprint = self._fingerprint(decision, event)
        if fingerprint in self.seen_fingerprints:
            return None
        self._remember_fingerprint(fingerprint)

        alert_id = f"alert_{decision.timestamp_ms}_{fingerprint[:10]}"
        record = AlertRecord(
            alert_id=alert_id,
            created_at_ms=decision.timestamp_ms,
            updated_at_ms=decision.timestamp_ms,
            severity=alert.severity,
            state=alert.state,
            reason_codes=list(alert.reason_codes),
            recommended_action=alert.recommended_action,
            acknowledge_required=alert.acknowledge_required,
            last_zone=last_zone,
            sensor_health=str(decision.debug.get("sensor_health", "unknown")),
            incident_age_s=self._incident_age_s(decision),
        )
        self.records[alert_id] = record
        self._save()
        return self._delivery_payload(record, alert, "alert_opened", "primary")

    def acknowledge(self, event: NormalizedEvent) -> dict[str, Any]:
        record = self._require_open_record(event.alert_id)
        if record.acknowledged_at_ms is None:
            record.acknowledged_at_ms = event.timestamp_ms
            record.acknowledged_by = event.actor_id or event.sensor_id
            record.updated_at_ms = event.timestamp_ms
            record.escalation_stage = "acknowledged"
            self._save()
        return self._status_payload(record, "alert_acknowledged")

    def resolve(self, event: NormalizedEvent) -> dict[str, Any]:
        record = self._require_open_record(event.alert_id)
        record.resolved_at_ms = event.timestamp_ms
        record.resolved_by = event.actor_id or event.sensor_id
        record.resolution = event.outcome or str(event.value)
        record.updated_at_ms = event.timestamp_ms
        record.escalation_stage = "resolved"
        self._save()
        return self._status_payload(record, "alert_resolved")

    def due_escalations(self, now_ms: int) -> list[dict[str, Any]]:
        ladder = self.rules.get("alert_ladder", {})
        primary_timeout_ms = int(ladder.get("primary_ack_timeout_s", 180) * 1000)
        backup_timeout_ms = int(ladder.get("backup_ack_timeout_s", 300) * 1000)
        due: list[dict[str, Any]] = []

        for record in self.records.values():
            if not record.open or not record.acknowledge_required or record.acknowledged_at_ms is not None:
                continue
            if record.escalation_stage == "primary" and now_ms - record.created_at_ms >= primary_timeout_ms:
                record.escalation_stage = "backup"
                record.updated_at_ms = now_ms
                due.append(self._escalation_payload(record, "alert_escalated", "backup"))
            elif record.escalation_stage == "backup" and now_ms - record.updated_at_ms >= backup_timeout_ms:
                record.escalation_stage = "unacknowledged"
                record.updated_at_ms = now_ms
                due.append(self._escalation_payload(record, "alert_unacknowledged", "all_caregivers"))

        if due:
            self._save()
        return due

    def get(self, alert_id: str) -> AlertRecord | None:
        return self.records.get(alert_id)

    def _require_open_record(self, alert_id: str) -> AlertRecord:
        if not alert_id:
            raise ValueError("alert lifecycle event requires alert_id")
        record = self.records.get(alert_id)
        if record is None:
            raise ValueError(f"unknown alert_id: {alert_id}")
        if not record.open:
            raise ValueError(f"alert is already resolved: {alert_id}")
        return record

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        with self.state_path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        self.seen_fingerprints = [str(value) for value in payload.get("seen_fingerprints", [])]
        self.records = {
            item["alert_id"]: AlertRecord(**item)
            for item in payload.get("records", [])
        }

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(f"{self.state_path.suffix}.tmp")
        payload = {
            "version": 1,
            "seen_fingerprints": self.seen_fingerprints,
            "records": [asdict(record) for record in self.records.values()],
        }
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        temporary.replace(self.state_path)

    def _remember_fingerprint(self, fingerprint: str) -> None:
        self.seen_fingerprints.append(fingerprint)
        self.seen_fingerprints = self.seen_fingerprints[-2000:]

    @staticmethod
    def _fingerprint(decision: DetectorDecision, event: NormalizedEvent) -> str:
        material = json.dumps(
            {
                "event_id": event.event_id,
                "timestamp_ms": decision.timestamp_ms,
                "state": decision.state,
                "severity": decision.severity,
                "reason_codes": decision.reason_codes,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _incident_age_s(decision: DetectorDecision) -> float:
        durations = [
            float(decision.debug.get(key, 0) or 0)
            for key in (
                "bed_unoccupied_s",
                "bathroom_occupied_s",
                "fall_suspected_s",
                "floor_level_s",
                "no_motion_s",
            )
        ]
        return round(max(durations, default=0.0), 1)

    def _delivery_payload(
        self,
        record: AlertRecord,
        alert: AlertPayload,
        event_type: str,
        target: str,
    ) -> dict[str, Any]:
        return alert.as_dict() | self._common_payload(record, event_type, target)

    def _escalation_payload(self, record: AlertRecord, event_type: str, target: str) -> dict[str, Any]:
        title = "Unacknowledged urgent senior night alert"
        if event_type == "alert_escalated":
            title = "Backup response needed: senior night alert"
        return {
            "title": title,
            "message": (
                f"Alert {record.alert_id} remains unacknowledged. "
                "Follow the household response plan; automatic emergency calling is disabled."
            ),
        } | self._common_payload(record, event_type, target)

    def _status_payload(self, record: AlertRecord, event_type: str) -> dict[str, Any]:
        return self._common_payload(record, event_type, "status") | {
            "acknowledged_at_ms": record.acknowledged_at_ms,
            "acknowledged_by": record.acknowledged_by,
            "resolved_at_ms": record.resolved_at_ms,
            "resolved_by": record.resolved_by,
            "resolution": record.resolution,
        }

    def _common_payload(self, record: AlertRecord, event_type: str, target: str) -> dict[str, Any]:
        return {
            "event_type": event_type,
            "alert_id": record.alert_id,
            "alert_timestamp_ms": record.created_at_ms,
            "event_timestamp_ms": record.updated_at_ms,
            "severity": record.severity,
            "state": record.state,
            "reason_codes": record.reason_codes,
            "recommended_action": record.recommended_action,
            "acknowledge_required": record.acknowledge_required,
            "target": target,
            "escalation_stage": record.escalation_stage,
            "last_zone": record.last_zone,
            "sensor_health": record.sensor_health,
            "incident_age_s": record.incident_age_s,
            "automatic_emergency_calls_enabled": bool(
                self.rules.get("alert_ladder", {}).get("automatic_emergency_calls_enabled", False)
            ),
        }
