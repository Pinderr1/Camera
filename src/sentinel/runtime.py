from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import ARM_MODES, SENSITIVITIES, cameras, zone_index
from .engine import TemporalEventEngine
from .incidents import AlertPolicy, AlertPolicyConfig, IncidentConfig, IncidentManager
from .models import Incident, Notification, SecurityEvent, Track, severity_at_least
from .notify import NotificationDispatcher, Notifier, build_notifiers
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
    ) -> None:
        self.config = config
        self.storage_dir = Path(storage_dir or config.get("storage", {}).get("dir", "data/sentinel"))
        self.media_root = self.storage_dir / "media"
        self.media_root.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.storage_dir / "settings.json"
        self.settings = self._load_settings()
        self.bus = EventBus()
        self.store = EventStore(self.storage_dir / "sentinel.db")
        self.engine = TemporalEventEngine(config, arm_mode=self.settings["arm_mode"], sensitivity=self.settings["sensitivity"])
        self.incidents = IncidentManager(IncidentConfig.from_dict(config.get("incidents")))
        self.policy = AlertPolicy(AlertPolicyConfig.from_dict(config.get("alerts")))
        self.dispatcher = NotificationDispatcher(
            notifiers if notifiers is not None else build_notifiers(config, self.media_root, mqtt_publish)
        )
        self.zones = zone_index(config)
        self.camera_configs = {camera["id"]: camera for camera in cameras(config)}
        self.camera_status: dict[str, dict[str, Any]] = {
            camera_id: {"camera_id": camera_id, "connected": False, "last_frame_ms": 0, "fps": 0.0, "error": "", "offline_reported": False}
            for camera_id in self.camera_configs
        }
        self.recorders: dict[str, Any] = {}
        self.latest_jpeg: dict[str, bytes] = {}
        self.drone = None
        self.started_ms = int(time.time() * 1000)
        self._lock = threading.RLock()
        self._last_purge_ms = 0

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
                self._notify(notification)
            self._maybe_request_drone(incident)

    def _capture_media(self, event: SecurityEvent, incident: Incident, image: Any, active: list[Track], is_new: bool) -> None:
        recorder = self.recorders.get(event.camera_id)
        privacy = self.config.get("privacy", {})
        if recorder is None:
            return
        if privacy.get("store_snapshots", True) and (is_new or severity_at_least(event.severity, "medium")):
            from .vision import annotate

            camera_zones = self.camera_configs.get(event.camera_id, {}).get("zones", [])
            event.snapshot_path = recorder.snapshot(annotate(image, active, camera_zones), event.timestamp_ms, event.event_id)
            if not incident.snapshot_path:
                incident.snapshot_path = event.snapshot_path
        if privacy.get("store_clips", True):
            clip = recorder.trigger(incident.incident_id, event.timestamp_ms)
            if clip:
                event.clip_path = clip
                if clip not in incident.clip_paths:
                    incident.clip_paths.append(clip)

    def _extend_recordings(self, camera_id: str, site_ids: set[str], now_ms: int) -> None:
        recorder = self.recorders.get(camera_id)
        if recorder is None or not site_ids or not self.config.get("privacy", {}).get("store_clips", True):
            return
        for incident in self.incidents.open.values():
            if camera_id in incident.cameras and site_ids & set(incident.site_track_ids):
                clip = recorder.trigger(incident.incident_id, now_ms)
                if clip and clip not in incident.clip_paths:
                    incident.clip_paths.append(clip)

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
            status = self.camera_status[camera_id]
            was_offline_reported = status["offline_reported"]
            status.update(fields)
            if fields.get("connected") and was_offline_reported:
                status["offline_reported"] = False
                now_ms = int(fields.get("last_frame_ms") or time.time() * 1000)
                self._system_event(camera_id, "camera_online", "info", f"Camera {self._camera_name(camera_id)} is back online", now_ms)

    def housekeeping(self, now_ms: int | None = None) -> None:
        now_ms = now_ms or int(time.time() * 1000)
        with self._lock:
            self.engine.tick(now_ms)
            offline_after_ms = int(self.config.get("health", {}).get("camera_offline_after_s", 60) * 1000)
            for camera_id, status in self.camera_status.items():
                last = status["last_frame_ms"] or self.started_ms
                if not status["offline_reported"] and now_ms - last > offline_after_ms:
                    status["offline_reported"] = True
                    status["connected"] = False
                    self._system_event(camera_id, "camera_offline", "medium", f"Camera {self._camera_name(camera_id)} stopped sending video", now_ms)
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
            )
            if any(purged.values()):
                log.info("retention purge: %s", purged)

    def _system_event(self, camera_id: str, event_type: str, severity: str, summary: str, now_ms: int) -> None:
        event = SecurityEvent(
            event_id=f"evt_{now_ms}_{event_type}_{camera_id}",
            event_type=event_type,
            severity=severity,
            camera_id=camera_id,
            timestamp_ms=now_ms,
            started_ms=now_ms,
            confidence=1.0,
            summary=summary,
            reason_codes=[event_type],
        )
        self._handle_events([event], now_ms)

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
            }

    def close(self) -> None:
        for recorder in self.recorders.values():
            recorder.finish()
        self.dispatcher.stop()
        self.store.close()
