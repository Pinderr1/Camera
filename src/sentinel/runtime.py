from __future__ import annotations

import dataclasses
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import ARM_MODES, SENSITIVITIES, cameras, zone_index
from .engine import TemporalEventEngine
from .explain import notification_reason
from .health import HealthMonitor, HealthSettings
from .incidents import DISCLAIMER, AlertPolicy, AlertPolicyConfig, IncidentConfig, IncidentManager
from .models import Incident, Notification, SecurityEvent, Track, effective_categories, severity_at_least
from .notify import NotificationDispatcher, Notifier, build_notifiers
from .credentials import SecretStore
from .store import EventStore

log = logging.getLogger("sentinel.runtime")
DAY_MS = 86_400_000


class EventBus:
    """In-process pub/sub. The API's live stream and the MQTT publisher are just subscribers."""

    def __init__(self) -> None:
        self._subscribers: list[Callable[[str, dict[str, Any]], None]] = []
        self._lock = threading.Lock()

    def subscribe(self, callback: Callable[[str, dict[str, Any]], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(topic, payload)
            except Exception:  # a broken subscriber must not take down detection
                log.exception("bus subscriber failed on %s", topic)


class SentinelRuntime:
    def __init__(
        self,
        config: dict[str, Any],
        *,
        storage_dir: str | Path | None = None,
        notifiers: list[Notifier] | None = None,
        mqtt_publish: Callable[[str, dict], None] | None = None,
        store: EventStore | None = None,
    ) -> None:
        self.config = config
        self.storage_dir = Path(storage_dir or config.get("storage", {}).get("dir", "data/sentinel"))
        self.media_root = self.storage_dir / "media"
        self.media_root.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.storage_dir / "settings.json"
        self.settings = self._load_settings()
        self.bus = EventBus()
        self.store = store if store is not None else EventStore(self.storage_dir / "sentinel.db")
        self.engine = TemporalEventEngine(config, arm_mode=self.settings["arm_mode"], sensitivity=self.settings["sensitivity"])
        self.incidents = IncidentManager(IncidentConfig.from_dict(config.get("incidents")))
        self.policy = AlertPolicy(AlertPolicyConfig.from_dict(config.get("alerts")))
        self.secrets = SecretStore(self.storage_dir / "credentials.json")
        self._mqtt_publish = mqtt_publish
        self._fixed_notifiers = notifiers is not None
        self.dispatcher = NotificationDispatcher(
            notifiers if notifiers is not None else build_notifiers(config, self.media_root, mqtt_publish, self.secrets.get)
        )
        self.zones = zone_index(config)
        self.camera_configs = {camera["id"]: camera for camera in cameras(config)}
        self.camera_status: dict[str, dict[str, Any]] = {camera_id: _initial_status(camera_id) for camera_id in self.camera_configs}
        self.recorders: dict[str, Any] = {}
        self.latest_jpeg: dict[str, bytes] = {}  # annotated live view
        self.latest_frame: dict[str, bytes] = {}  # clean (privacy-masked, unannotated) frame for the setup editor
        self.drone = None
        self.started_ms = int(time.time() * 1000)
        self._lock = threading.RLock()
        self._last_purge_ms = 0
        self._scene_logged: dict[tuple[str, str], int] = {}
        self.health = HealthMonitor(
            HealthSettings.from_dict(config.get("health")),
            save_outage=lambda outage: self.store.upsert_outage(outage.as_dict()),
            record_event=self._health_event,
            notify=self._notify,
            camera_name=self._camera_name,
        )
        self.storage_status: dict[str, Any] = {}
        self._recording_error_ms = 0
        self._health_seq = 0

    def reconfigure(self, config: dict[str, Any]) -> None:
        """Apply an edited site config. Zones, rules, topology, and alert policy
        take effect immediately; open incidents, settings, and history are kept.
        In-flight tracks restart, since zone geometry may have changed."""
        with self._lock:
            self.config = config
            self.engine = TemporalEventEngine(config, arm_mode=self.settings["arm_mode"], sensitivity=self.settings["sensitivity"])
            self.incidents.config = IncidentConfig.from_dict(config.get("incidents"))
            self.policy.config = AlertPolicyConfig.from_dict(config.get("alerts"))
            if not self._fixed_notifiers:  # channels edited in the dashboard apply without a restart
                self.dispatcher.notifiers = build_notifiers(config, self.media_root, self._mqtt_publish, self.secrets.get)
            self.zones = zone_index(config)
            self.health.settings = HealthSettings.from_dict(config.get("health"))
            self.camera_configs = {camera["id"]: camera for camera in cameras(config)}
            for camera_id in list(self.camera_status):
                if camera_id not in self.camera_configs:
                    del self.camera_status[camera_id]
                    self.latest_jpeg.pop(camera_id, None)
                    self.latest_frame.pop(camera_id, None)
                    self.recorders.pop(camera_id, None)
            for camera_id in self.camera_configs:
                self.camera_status.setdefault(camera_id, _initial_status(camera_id))

    # ---- settings --------------------------------------------------------

    def _load_settings(self) -> dict[str, Any]:
        defaults = {"arm_mode": "home", "sensitivity": "normal", "notifications_enabled": True} | self.config.get("settings_defaults", {})
        if self.settings_path.exists():
            try:
                defaults |= json.loads(self.settings_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log.error("ignoring corrupt %s", self.settings_path)
        return defaults

    def update_settings(self, changes: dict[str, Any], actor: str) -> dict[str, Any]:
        allowed = {"arm_mode": ARM_MODES, "sensitivity": tuple(SENSITIVITIES), "notifications_enabled": (True, False)}
        with self._lock:
            for key, value in changes.items():
                if key not in allowed or value not in allowed[key]:
                    raise ValueError(f"invalid setting {key}={value!r}")
            self.settings |= changes
            self.engine.arm_mode = self.settings["arm_mode"]
            self.engine.set_sensitivity(self.settings["sensitivity"])
            temporary = self.settings_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")
            temporary.replace(self.settings_path)
        self.store.audit(actor, "settings_changed", changes)
        self.bus.publish("settings", dict(self.settings))
        return dict(self.settings)

    # ---- detection path --------------------------------------------------

    def process_camera_update(
        self,
        camera_id: str,
        timestamp_ms: int,
        active: list[Track],
        ended: list[Track],
        image: Any = None,
    ) -> list[SecurityEvent]:
        with self._lock:
            events = self.engine.process(camera_id, timestamp_ms, active, ended)
            site_ids = {self.engine.correlator.track_to_site.get(track.track_id, "") for track in active} - {""}
            self.incidents.touch(site_ids, timestamp_ms)
            self._extend_recordings(camera_id, site_ids, timestamp_ms)
            self._handle_events(events, timestamp_ms, image=image, active=active)
            return events

    def _handle_events(self, events: list[SecurityEvent], now_ms: int, *, image: Any = None, active: list[Track] | None = None) -> None:
        changed: dict[str, tuple[Incident, list[SecurityEvent]]] = {}
        for event in events:
            self._apply_category_policy(event)
            incident, is_new = self.incidents.add_event(event)
            if image is not None and incident is not None:
                self._capture_media(event, incident, image, active or [], is_new)
            self.store.add_event(event)
            self.bus.publish("event", event.as_dict())
            if incident is not None:
                changed.setdefault(incident.incident_id, (incident, []))[1].append(event)
        for incident, incident_events in changed.values():
            self.store.upsert_incident(incident)
            self.bus.publish("incident", incident.as_dict())
            notification = self.policy.evaluate(incident, incident_events, self.settings["arm_mode"], now_ms)
            if notification:
                reason = notification_reason(incident_events, {rule["id"]: rule for rule in self.config.get("rules", [])})
                if reason and notification.message.endswith(DISCLAIMER):
                    notification = dataclasses.replace(notification, message=notification.message[: -len(DISCLAIMER)] + reason + "\n" + DISCLAIMER)
                self._notify(notification)
            self._maybe_request_drone(incident)

    SCENE_LABELS = {
        "lighting_change": "Lighting change",
        "ir_mode": "Camera switched to night (IR) mode",
        "precipitation_or_noise": "Rain, snow, or image noise",
        "moving_shadows": "Moving shadows",
        "recurring_motion": "Recurring background motion (vegetation, flags)",
        "near_camera_object": "Something close to the lens (insect, web, debris)",
    }

    def scene_update(self, camera_id: str, conditions: dict[str, bool], now_ms: int) -> None:
        """Record environmental conditions on the camera and log their onset as
        info events (at most once per condition per camera per 10 minutes).
        Environmental motion never opens an incident."""
        with self._lock:
            status = self.camera_status.get(camera_id)
            if status is None:
                return
            active = sorted(name for name, on in conditions.items() if on)
            previous = set(status.get("scene", []))
            status["scene"] = active
            for name in set(active) - previous:
                key = (camera_id, name)
                if now_ms - self._scene_logged.get(key, -10**12) < 600_000:
                    continue
                self._scene_logged[key] = now_ms
                event = SecurityEvent(
                    event_id=f"evt_{now_ms}_scene_{camera_id}_{name}", event_type="scene_condition", severity="info",
                    camera_id=camera_id, timestamp_ms=now_ms, started_ms=now_ms, confidence=0.5,
                    summary=f"{self.SCENE_LABELS.get(name, name)} on {self._camera_name(camera_id)}", reason_codes=[name],
                )
                self.store.add_event(event)
                self.bus.publish("event", event.as_dict())

    def _apply_category_policy(self, event: SecurityEvent) -> None:
        """Categories with alerts off (animals by default) are recorded as info:
        info events never open incidents or send notifications."""
        if not event.label or event.severity == "info":
            return
        categories = effective_categories(self.config, self.camera_configs.get(event.camera_id))
        if not categories.get(event.label, {}).get("alerts", False):
            event.details["original_severity"] = event.severity
            event.severity = "info"
            event.reason_codes.append("category_alerts_off")

    def _capture_media(self, event: SecurityEvent, incident: Incident, image: Any, active: list[Track], is_new: bool) -> None:
        recorder = self.recorders.get(event.camera_id)
        privacy = self.config.get("privacy", {})
        if recorder is None:
            return
        try:
            if privacy.get("store_snapshots", True) and (is_new or severity_at_least(event.severity, "medium")):
                from .vision import annotate

                camera_zones = self.camera_configs.get(event.camera_id, {}).get("zones", [])
                event.snapshot_path = recorder.snapshot(annotate(image, active, camera_zones), event.timestamp_ms, event.event_id)
                if event.snapshot_path:
                    self.record_evidence(event.snapshot_path)
                if event.snapshot_path and not incident.snapshot_path:
                    incident.snapshot_path = event.snapshot_path
            if privacy.get("store_clips", True):
                clip = recorder.trigger(incident.incident_id, event.timestamp_ms)
                if clip:
                    event.clip_path = clip
                    if clip not in incident.clip_paths:
                        incident.clip_paths.append(clip)
                    if clip not in incident.clip_starts and recorder.clip_started_ms():
                        incident.clip_starts[clip] = {"camera_id": event.camera_id, "started_ms": recorder.clip_started_ms()}
        except Exception as error:  # a failed recording must never lose the event or its alert
            self.recording_failed(event.camera_id, error)

    def _extend_recordings(self, camera_id: str, site_ids: set[str], now_ms: int) -> None:
        recorder = self.recorders.get(camera_id)
        if recorder is None or not site_ids or not self.config.get("privacy", {}).get("store_clips", True):
            return
        for incident in self.incidents.open.values():
            if camera_id in incident.cameras and site_ids & set(incident.site_track_ids):
                try:
                    clip = recorder.trigger(incident.incident_id, now_ms)
                except Exception as error:
                    self.recording_failed(camera_id, error)
                    return
                if clip and clip not in incident.clip_paths:
                    incident.clip_paths.append(clip)
                if clip and clip not in incident.clip_starts and recorder.clip_started_ms():
                    incident.clip_starts[clip] = {"camera_id": camera_id, "started_ms": recorder.clip_started_ms()}

    def _notify(self, notification: Notification) -> None:
        self.bus.publish("notification", notification.as_dict())
        self.store.audit("system", "notification", notification.as_dict())
        if self.settings.get("notifications_enabled", True):
            self.dispatcher.submit(notification)

    def _maybe_request_drone(self, incident: Incident) -> None:
        """Queue (never approve) an inspection request for serious incidents."""
        drone_config = self.config.get("drone", {})
        threshold = drone_config.get("auto_request_min_severity")
        if self.drone is None or not threshold or not severity_at_least(incident.severity, threshold):
            return
        zone = next(
            (self.zones[zone_id] for zone_id in reversed(incident.zones) if "map_polygon" in self.zones.get(zone_id, {})),
            None,
        )
        if zone is None:
            return
        polygon = zone["map_polygon"]
        centroid = (sum(p[0] for p in polygon) / len(polygon), sum(p[1] for p in polygon) / len(polygon))
        try:
            self.drone.request_inspection(
                requested_by="sentinel", map_point=centroid, label=zone.get("name", zone["id"]), source="incident", incident_id=incident.incident_id
            )
        except Exception as error:
            log.warning("drone inspection request not created: %s", error)

    # ---- operator actions ------------------------------------------------

    def acknowledge(self, incident_id: str, actor: str) -> dict[str, Any]:
        def apply(incident: Incident) -> None:
            if incident.acknowledged_ms is None:
                incident.acknowledged_ms = int(time.time() * 1000)
                incident.acknowledged_by = actor

        return self._update_incident(incident_id, actor, "incident_acknowledged", apply)

    def resolve(self, incident_id: str, actor: str, outcome: str) -> dict[str, Any]:
        allowed = {"expected_activity", "false_alarm", "real_concern", "unclear"}
        if outcome not in allowed:
            raise ValueError(f"outcome must be one of {sorted(allowed)}")

        def apply(incident: Incident) -> None:
            incident.resolution = outcome
            incident.resolved_by = actor
            if incident.acknowledged_ms is None:
                incident.acknowledged_ms = int(time.time() * 1000)
                incident.acknowledged_by = actor

        return self._update_incident(incident_id, actor, "incident_resolved", apply, outcome=outcome)

    def add_note(self, incident_id: str, actor: str, text: str) -> dict[str, Any]:
        """Operator annotation. Notes are appended, never edited, and audited."""
        text = text.strip()[:2000]
        if not text:
            raise ValueError("note is empty")

        def apply(incident: Incident) -> None:
            incident.notes.append({"by": actor, "ms": int(time.time() * 1000), "text": text})

        return self._update_incident(incident_id, actor, "incident_note_added", apply)

    def report_missed(self, actor: str, *, occurred_ms: int, camera_id: str, zone_id: str = "", category: str = "", description: str = "") -> dict[str, Any]:
        """The owner saw something Sentinel did not report. Stored for evaluation,
        together with what Sentinel did record on that camera around that time."""
        if camera_id not in self.camera_configs:
            raise KeyError(camera_id)
        window = 300_000
        nearby = self.store.list_events(since_ms=occurred_ms - window, until_ms=occurred_ms + window, camera_id=camera_id, limit=50)
        status = self.camera_status.get(camera_id, {}).get("health", {})
        now = int(time.time() * 1000)
        report = {
            "report_id": f"miss_{now}", "created_ms": now, "reported_by": actor, "occurred_ms": int(occurred_ms), "camera_id": camera_id,
            "zone_id": zone_id, "category": category, "description": description.strip()[:1000],
            "sentinel_recorded": [{"event_id": e["event_id"], "event_type": e["event_type"], "summary": e["summary"], "timestamp_ms": e["timestamp_ms"]} for e in nearby],
            "camera_state_now": status.get("state"),
        }
        self.store.add_missed_report(report)
        self.store.audit(actor, "missed_event_reported", {"report_id": report["report_id"], "camera_id": camera_id})
        return report

    def record_evidence(self, relative_path: str) -> None:
        """Fingerprint a finished clip or snapshot (SHA-256) so exports can show it is unchanged."""
        import hashlib

        path = self.media_root / relative_path
        try:
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
            self.store.record_evidence(relative_path, digest.hexdigest(), path.stat().st_size)
        except OSError as error:
            log.warning("could not fingerprint %s: %s", relative_path, error)

    def protect(self, incident_id: str, actor: str, protected: bool = True) -> dict[str, Any]:
        """Keep an incident's recordings past normal retention and out of disk-space purges."""

        def apply(incident: Incident) -> None:
            incident.protected = protected

        return self._update_incident(incident_id, actor, "incident_protected" if protected else "incident_unprotected", apply)

    def _update_incident(self, incident_id: str, actor: str, action: str, mutate: Callable[[Incident], Any], **extra: Any) -> dict[str, Any]:
        with self._lock:
            incident = self.incidents.open.get(incident_id)
            if incident is None:
                stored = self.store.get_incident(incident_id)
                if stored is None:
                    raise KeyError(incident_id)
                incident = Incident(**stored)
            mutate(incident)
            self.store.upsert_incident(incident)
        self.store.audit(actor, action, {"incident_id": incident_id} | extra)
        self.bus.publish("incident", incident.as_dict())
        return incident.as_dict()

    # ---- health and housekeeping -----------------------------------------

    def update_camera_status(self, camera_id: str, **fields: Any) -> None:
        with self._lock:
            status = self.camera_status.get(camera_id)
            if status is None:  # camera removed while its worker was stopping
                return
            status.update(fields)

    def recording_failed(self, camera_id: str, error: BaseException) -> None:
        """Clip or snapshot write failed (disk full, permissions): detection and
        alerts continue; the storage component is reported degraded."""
        now_ms = int(time.time() * 1000)
        if now_ms - self._recording_error_ms > 60_000:
            log.error("recording failed on %s: %s", camera_id, error)
        self._recording_error_ms = now_ms
        with self._lock:
            self.health.set_component("storage", "degraded", f"recording failed on {self._camera_name(camera_id)}: {str(error)[:160]}", now_ms)

    def apply_storage_status(self, status: dict[str, Any], now_ms: int | None = None) -> None:
        """From the service's StorageGuard: pause or resume recording and report it."""
        now_ms = now_ms or int(time.time() * 1000)
        self.storage_status = status
        for recorder in list(self.recorders.values()):
            recorder.set_paused(status.get("recording_paused", False))
        with self._lock:
            if status.get("recording_paused"):
                self.health.set_component(
                    "storage", "failed", f"disk nearly full ({status['free_mb']} MB free): recording paused, detection and alerts continue", now_ms
                )
            elif status.get("low"):
                self.health.set_component(
                    "storage", "degraded", f"disk space low ({status['free_mb']} MB free); oldest recordings are being deleted", now_ms, notify=False
                )
            elif now_ms - self._recording_error_ms > 300_000:
                self.health.set_component("storage", "ok", "", now_ms)

    def housekeeping(self, now_ms: int | None = None) -> None:
        now_ms = now_ms or int(time.time() * 1000)
        with self._lock:
            self.engine.tick(now_ms)
            visits, trips = self.engine.drain_activity()
            if (visits or trips) and self.config.get("activity", {}).get("enabled", True):
                self.store.add_activity(visits, trips)
            self.health.evaluate(self.camera_status, now_ms)
            if self.store.last_write_ok:
                self.health.set_component("database", "ok", "", now_ms)
            else:
                self.health.set_component("database", "degraded", f"events cannot be saved ({self.store.last_error[:160]}); alerts still go out", now_ms)
            for incident in self.incidents.close_stale(now_ms):
                self.store.upsert_incident(incident)
                self.bus.publish("incident", incident.as_dict())
            for notification in self.policy.due_escalations(list(self.incidents.open.values()), now_ms):
                self._notify(notification)
        if now_ms - self._last_purge_ms > 3_600_000:
            self._last_purge_ms = now_ms
            retention = self.config.get("privacy", {}).get("retention_days", {})
            purged = self.store.purge(
                events_before_ms=now_ms - int(retention.get("events", 30) * DAY_MS),
                media_before_ms=now_ms - int(retention.get("media", 7) * DAY_MS),
                media_root=self.media_root,
                protected_before_ms=now_ms - int(retention.get("protected", 90) * DAY_MS),
            )
            purged["activity"] = self.store.purge_activity(now_ms - int(self.config.get("activity", {}).get("retention_days", 90) * DAY_MS))
            if any(purged.values()):
                log.info("retention purge: %s", purged)

    def _health_event(self, camera_id: str, event_type: str, severity: str, summary: str, now_ms: int) -> None:
        """Health events go to the timeline and live stream but never open
        incidents: coverage problems are notified by the health monitor."""
        self._health_seq += 1
        event = SecurityEvent(
            event_id=f"evt_{now_ms}_{event_type}_{camera_id or 'system'}_{self._health_seq}", event_type=event_type, severity=severity,
            camera_id=camera_id, timestamp_ms=now_ms, started_ms=now_ms, confidence=1.0, summary=summary, reason_codes=[event_type],
        )
        self.store.add_event(event)
        self.bus.publish("event", event.as_dict())
        self.bus.publish("health", self.health.coverage(self.camera_status))

    def _camera_name(self, camera_id: str) -> str:
        return self.camera_configs.get(camera_id, {}).get("name", camera_id)

    def snapshot_state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "settings": dict(self.settings),
                "cameras": [dict(status) for status in self.camera_status.values()],
                "open_incidents": [incident.as_dict() for incident in self.incidents.open.values()],
                "site_tracks": [site.summary() for site in self.engine.site_tracks() if site.active_members],
                "ambiguous_links": self.engine.correlator.ambiguous_count,
                "coverage": self.health.coverage(self.camera_status),
            }

    def close(self) -> None:
        for recorder in self.recorders.values():
            recorder.finish()
        self.dispatcher.stop()
        self.store.close()


def _initial_status(camera_id: str) -> dict[str, Any]:
    return {"camera_id": camera_id, "connected": False, "last_frame_ms": 0, "fps": 0.0, "error": "", "offline_reported": False}
