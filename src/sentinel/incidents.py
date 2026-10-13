from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .models import (
    SEVERITIES,
    SEVERITY_RANK,
    Incident,
    Notification,
    SecurityEvent,
    max_severity,
    severity_at_least,
)

PRIORITY = {"info": 2, "low": 3, "medium": 4, "high": 5}
DISCLAIMER = "Sentinel reports observed activity, not intent. Review the clip before acting."


@dataclass
class IncidentConfig:
    merge_window_s: float = 120.0
    close_after_s: float = 120.0
    # Distinct concerning behaviours from one incident raise its severity one level.
    corroboration_behaviors: int = 3

    @classmethod
    def from_dict(cls, data: dict | None) -> "IncidentConfig":
        data = data or {}
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


class IncidentManager:
    """Groups related events into incidents so one person walking the property
    produces one evolving incident, not a stream of disconnected alerts."""

    def __init__(self, config: IncidentConfig | None = None) -> None:
        self.config = config or IncidentConfig()
        self.open: dict[str, Incident] = {}
        self._seq = 0

    def add_event(self, event: SecurityEvent) -> tuple[Incident | None, bool]:
        """Returns (incident or None, whether the incident is new). Info-only
        activity never opens an incident but joins one that already covers it."""
        incident = self._match(event)
        is_new = False
        if incident is None:
            if not severity_at_least(event.severity, "low"):
                return None, False
            self._seq += 1
            incident = Incident(
                incident_id=f"inc_{event.timestamp_ms}_{self._seq}",
                opened_ms=event.started_ms,
                updated_ms=event.timestamp_ms,
                severity=event.severity,
                title=event.summary,
            )
            self.open[incident.incident_id] = incident
            is_new = True

        incident.updated_ms = max(incident.updated_ms, event.timestamp_ms)
        incident.event_ids.append(event.event_id)
        if event.event_type not in incident.event_types:
            incident.event_types.append(event.event_type)
        for collection, values in (
            (incident.cameras, [event.camera_id, *event.details.get("cameras", [])]),
            (incident.zones, [event.zone_id]),
            (incident.site_track_ids, [event.site_track_id]),
        ):
            for value in values:
                if value and value not in collection:
                    collection.append(value)
        if event.event_type != "activity" and SEVERITY_RANK[event.severity] >= SEVERITY_RANK[incident.peak_event_severity]:
            incident.peak_event_severity = event.severity
            incident.title = event.summary
        incident.severity = self._severity(incident, event)
        event.incident_id = incident.incident_id
        return incident, is_new

    def touch(self, site_track_ids: set[str], now_ms: int) -> None:
        """Keep incidents open while their subjects are still in view."""
        for incident in self.open.values():
            if site_track_ids & set(incident.site_track_ids):
                incident.updated_ms = max(incident.updated_ms, now_ms)

    def close_stale(self, now_ms: int) -> list[Incident]:
        closed = []
        for incident_id, incident in list(self.open.items()):
            if now_ms - incident.updated_ms >= self.config.close_after_s * 1000:
                incident.status = "closed"
                incident.closed_ms = now_ms
                closed.append(self.open.pop(incident_id))
        return closed

    def _match(self, event: SecurityEvent) -> Incident | None:
        window_ms = self.config.merge_window_s * 1000
        candidates = [
            incident
            for incident in self.open.values()
            if event.timestamp_ms - incident.updated_ms <= window_ms
        ]
        for incident in candidates:
            if event.site_track_id and event.site_track_id in incident.site_track_ids:
                return incident
        for incident in candidates:
            if event.site_track_id and any(
                site_id in incident.site_track_ids for site_id in event.details.get("site_track_ids", [])
            ):
                return incident
        if not severity_at_least(event.severity, "low"):
            return None
        for incident in candidates:
            if event.camera_id in incident.cameras:
                return incident
        return None

    def _severity(self, incident: Incident, event: SecurityEvent) -> str:
        severity = max_severity(incident.severity, event.severity)
        behaviours = [kind for kind in incident.event_types if kind != "activity"]
        if len(behaviours) >= self.config.corroboration_behaviors and severity != SEVERITIES[-1]:
            severity = SEVERITIES[SEVERITY_RANK[severity] + 1]
        return severity


@dataclass
class AlertPolicyConfig:
    notify_min_severity: dict[str, str]
    max_notifications: int = 6
    rate_window_s: float = 600.0
    ack_timeout_s: float = 300.0

    @classmethod
    def from_dict(cls, data: dict | None) -> "AlertPolicyConfig":
        data = data or {}
        thresholds = {"disarmed": "none", "home": "high", "away": "medium"} | data.get("notify_min_severity", {})
        return cls(
            notify_min_severity=thresholds,
            max_notifications=int(data.get("max_notifications", 6)),
            rate_window_s=float(data.get("rate_window_s", 600)),
            ack_timeout_s=float(data.get("ack_timeout_s", 300)),
        )


class AlertPolicy:
    """Decides which incident changes deserve a push notification. Each incident
    notifies once, again only if its severity rises, and a global rate limit
    stops a busy evening from flooding the phone."""

    def __init__(self, config: AlertPolicyConfig) -> None:
        self.config = config
        self._sent: deque[int] = deque()
        self.suppressed = 0
        self._escalated: set[str] = set()

    def evaluate(self, incident: Incident, events: list[SecurityEvent], arm_mode: str, now_ms: int) -> Notification | None:
        threshold = self.config.notify_min_severity.get(arm_mode, "none")
        if threshold == "none" or not severity_at_least(incident.severity, threshold):
            return None
        if incident.notified_severity and not (
            SEVERITY_RANK[incident.severity] > SEVERITY_RANK[incident.notified_severity]
        ):
            return None
        if not self._allow(now_ms):
            self.suppressed += 1
            return None
        incident.notified_severity = incident.severity
        lines = [event.summary for event in events if event.event_type != "activity"][-3:]
        suppressed_note = ""
        if self.suppressed:
            suppressed_note = f"\n{self.suppressed} lower-priority notification(s) were held back; see the timeline."
            self.suppressed = 0
        return Notification(
            incident_id=incident.incident_id,
            severity=incident.severity,
            title=f"{incident.severity.upper()}: {incident.title}",
            message="\n".join(lines + [f"Cameras: {', '.join(incident.cameras)}", DISCLAIMER]) + suppressed_note,
            priority=PRIORITY[incident.severity],
            camera_ids=tuple(incident.cameras),
            timestamp_ms=now_ms,
            snapshot_path=incident.snapshot_path,
        )

    def due_escalations(self, incidents: list[Incident], now_ms: int) -> list[Notification]:
        due = []
        for incident in incidents:
            if (
                incident.severity == "high"
                and incident.notified_severity
                and incident.acknowledged_ms is None
                and incident.incident_id not in self._escalated
                and now_ms - incident.opened_ms >= self.config.ack_timeout_s * 1000
            ):
                self._escalated.add(incident.incident_id)
                due.append(
                    Notification(
                        incident_id=incident.incident_id,
                        severity=incident.severity,
                        title=f"Unacknowledged: {incident.title}",
                        message=f"This incident has not been acknowledged. {DISCLAIMER}",
                        priority=5,
                        camera_ids=tuple(incident.cameras),
                        timestamp_ms=now_ms,
                        snapshot_path=incident.snapshot_path,
                        kind="escalation",
                    )
                )
        return due

    def _allow(self, now_ms: int) -> bool:
        window_ms = self.config.rate_window_s * 1000
        while self._sent and now_ms - self._sent[0] > window_ms:
            self._sent.popleft()
        if len(self._sent) >= self.config.max_notifications:
            return False
        self._sent.append(now_ms)
        return True
