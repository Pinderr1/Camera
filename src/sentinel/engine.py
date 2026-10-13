from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from senior_safety.pose_events import point_in_polygon

from .config import sensitivity_adjustments, zone_index
from .correlation import CorrelationConfig, CrossCameraCorrelator, SiteTrack, ZonePresence
from .models import SecurityEvent, Track

DEFAULT_RULE_CLASSES = ["person"]
ACTIVITY_CLASSES = {"person", "vehicle"}
QUIET_ZONE_TYPES = {"street", "ignore"}


@dataclass
class EngineConfig:
    zone_enter_ms: int = 600
    zone_exit_ms: int = 1500
    # A zone visit survives a short tracking dropout (occlusion, detector miss).
    presence_grace_ms: int = 5000
    arrival_radius: float = 0.03

    @classmethod
    def from_dict(cls, data: dict | None) -> "EngineConfig":
        data = data or {}
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


@dataclass
class _CameraTrackState:
    site_track_id: str
    inside: set[str] = field(default_factory=set)
    pending_enter: dict[str, int] = field(default_factory=dict)
    pending_exit: dict[str, int] = field(default_factory=dict)


class ZoneMap:
    def __init__(self, config: dict[str, Any]) -> None:
        self.zones = zone_index(config)
        self.by_camera: dict[str, list[dict[str, Any]]] = {}
        for zone in self.zones.values():
            self.by_camera.setdefault(zone["camera_id"], []).append(zone)

    def zones_at(self, camera_id: str, point: tuple[float, float]) -> list[str]:
        return [
            zone["id"]
            for zone in self.by_camera.get(camera_id, [])
            if zone["type"] != "ignore" and point_in_polygon(point[0], point[1], zone["polygon"])
        ]

    def is_ignored(self, camera_id: str, point: tuple[float, float]) -> bool:
        return any(
            zone["type"] == "ignore" and point_in_polygon(point[0], point[1], zone["polygon"])
            for zone in self.by_camera.get(camera_id, [])
        )

    def zone_type(self, zone_id: str) -> str:
        return self.zones.get(zone_id, {}).get("type", "general")

    def zone_name(self, zone_id: str) -> str:
        return self.zones.get(zone_id, {}).get("name", zone_id)


def in_active_hours(timestamp_ms: int, hours: dict[str, str] | None) -> bool:
    if not hours:
        return True
    now = datetime.fromtimestamp(timestamp_ms / 1000).strftime("%H:%M")
    start, end = hours["start"], hours["end"]
    if start <= end:
        return start <= now < end
    return now >= start or now < end


class TemporalEventEngine:
    """Turns per-camera track updates into security events by reasoning over
    time: zone visits with hysteresis, dwell, repeated activity, ordered zone
    sequences, and cross-camera movement. Detector frames are never judged in
    isolation; every event cites the rule, zone, tracks, and durations behind it.

    Event language describes observed behaviour only. It never asserts intent
    or identity."""

    def __init__(self, config: dict[str, Any], *, arm_mode: str = "home", sensitivity: str = "normal") -> None:
        self.config = config
        self.zones = ZoneMap(config)
        self.rules: list[dict[str, Any]] = list(config.get("rules", []))
        self.engine_config = EngineConfig.from_dict(config.get("engine"))
        self.correlator = CrossCameraCorrelator(
            config.get("topology", {}).get("links", []),
            CorrelationConfig.from_dict(config.get("correlation")),
        )
        self.arm_mode = arm_mode
        self.dwell_multiplier = 1.0
        self.set_sensitivity(sensitivity)
        self._camera_tracks: dict[str, _CameraTrackState] = {}
        self._zone_entries: dict[str, deque] = {}
        self._repeat_last_fired: dict[str, int] = {}
        self._event_seq = 0

    def set_sensitivity(self, sensitivity: str) -> None:
        self.dwell_multiplier, _ = sensitivity_adjustments(sensitivity)

    def process(self, camera_id: str, timestamp_ms: int, active: list[Track], ended: list[Track]) -> list[SecurityEvent]:
        events: list[SecurityEvent] = []
        touched: dict[str, tuple[SiteTrack, Track]] = {}

        for track in active:
            zones_now = self.zones.zones_at(camera_id, track.anchor)
            site = self.correlator.assign(track, timestamp_ms, zones_now)
            if track.max_displacement >= self.engine_config.arrival_radius:
                site.arrived = True
            state = self._camera_tracks.get(track.track_id)
            if state is None:
                state = _CameraTrackState(site_track_id=site.site_track_id)
                self._camera_tracks[track.track_id] = state
            entered, exited = self._update_membership(state, zones_now, timestamp_ms)
            for zone_id in exited:
                presence = site.zones.get(zone_id)
                if presence and presence.left_ms is None:
                    presence.left_ms = timestamp_ms
            for zone_id in entered:
                if self._enter_zone(site, zone_id, camera_id, timestamp_ms):
                    events.extend(self._on_new_entry(site, track, zone_id, timestamp_ms))
            touched[site.site_track_id] = (site, track)

        for site, track in touched.values():
            events.extend(self._evaluate_dwell(site, track, timestamp_ms))
            events.extend(self._evaluate_site_rules(site, track, timestamp_ms))

        for track in ended:
            state = self._camera_tracks.pop(track.track_id, None)
            site = self.correlator.end(track, timestamp_ms)
            if state and site:
                for zone_id in state.inside:
                    presence = site.zones.get(zone_id)
                    if presence and presence.left_ms is None:
                        presence.left_ms = track.last_seen_ms

        return events

    def tick(self, now_ms: int) -> None:
        self.correlator.expire(now_ms)

    def site_tracks(self) -> list[SiteTrack]:
        return list(self.correlator.site_tracks.values())

    # ---- zone membership -------------------------------------------------

    def _update_membership(self, state: _CameraTrackState, zones_now: list[str], now: int) -> tuple[list[str], list[str]]:
        entered: list[str] = []
        exited: list[str] = []
        current = set(zones_now)
        for zone_id in current - state.inside:
            started = state.pending_enter.setdefault(zone_id, now)
            if now - started >= self.engine_config.zone_enter_ms:
                state.inside.add(zone_id)
                state.pending_enter.pop(zone_id, None)
                entered.append(zone_id)
        for zone_id in list(state.pending_enter):
            if zone_id not in current:
                state.pending_enter.pop(zone_id)
        for zone_id in list(state.inside):
            if zone_id in current:
                state.pending_exit.pop(zone_id, None)
                continue
            started = state.pending_exit.setdefault(zone_id, now)
            if now - started >= self.engine_config.zone_exit_ms:
                state.inside.discard(zone_id)
                state.pending_exit.pop(zone_id, None)
                exited.append(zone_id)
        return entered, exited

    def _enter_zone(self, site: SiteTrack, zone_id: str, camera_id: str, now: int) -> bool:
        """Returns True for a genuinely new visit, False for a continuation after a short dropout."""
        presence = site.zones.get(zone_id)
        if presence and (presence.left_ms is None or now - presence.left_ms <= self.engine_config.presence_grace_ms):
            presence.left_ms = None
            return False
        site.zones[zone_id] = ZonePresence(since_ms=now)
        site.path.append((now, camera_id, zone_id))
        del site.path[:-50]
        self._zone_entries.setdefault(zone_id, deque(maxlen=200)).append((now, site.site_track_id, site.label))
        return True

    # ---- rules -----------------------------------------------------------

    def _rule_active(self, rule: dict[str, Any], label: str, now: int) -> bool:
        if not rule.get("enabled", True):
            return False
        if label not in rule.get("classes", DEFAULT_RULE_CLASSES):
            return False
        modes = rule.get("arm_modes")
        if modes and self.arm_mode not in modes:
            return False
        return in_active_hours(now, rule.get("active_hours"))

    def _rule_matches_zone(self, rule: dict[str, Any], zone_id: str) -> bool:
        zone_type = self.zones.zone_type(zone_id)
        if rule.get("zones"):
            return zone_id in rule["zones"]
        if rule.get("zone_types"):
            return zone_type in rule["zone_types"]
        return zone_type not in QUIET_ZONE_TYPES

    def _on_new_entry(self, site: SiteTrack, track: Track, zone_id: str, now: int) -> list[SecurityEvent]:
        events: list[SecurityEvent] = []
        zone_type = self.zones.zone_type(zone_id)
        zone_name = self.zones.zone_name(zone_id)
        if not site.activity_logged and site.label in ACTIVITY_CLASSES and zone_type not in QUIET_ZONE_TYPES:
            site.activity_logged = True
            events.append(
                self._event(
                    "activity", "info", track, site, now,
                    summary=f"{site.label.capitalize()} seen in {zone_name}",
                    zone_id=zone_id,
                    reason_codes=["object_on_property"],
                )
            )

        for rule in self.rules:
            if rule["type"] == "zone_entry" and self._rule_matches_zone(rule, zone_id) and self._rule_active(rule, site.label, now):
                if track.confidence < rule.get("min_confidence", 0.0):
                    continue
                key = f"{rule['id']}:{zone_id}:{site.zones[zone_id].since_ms}"
                if key in site.fired:
                    continue
                site.fired.add(key)
                events.append(
                    self._event(
                        "zone_entry", rule["severity"], track, site, now,
                        summary=f"{site.label.capitalize()} entered {zone_name}",
                        zone_id=zone_id, rule=rule,
                        reason_codes=[f"entered_{zone_type}_zone"],
                    )
                )

            if rule["type"] == "repeated_presence" and self._rule_matches_zone(rule, zone_id) and self._rule_active(rule, site.label, now):
                window_ms = int(rule["window_s"] * 1000)
                entries = [
                    entry
                    for entry in self._zone_entries.get(zone_id, ())
                    if now - entry[0] <= window_ms and entry[2] in rule.get("classes", DEFAULT_RULE_CLASSES)
                ]
                fired_key = f"{rule['id']}:{zone_id}"
                if len(entries) >= rule["count"] and now - self._repeat_last_fired.get(fired_key, -window_ms) >= window_ms:
                    self._repeat_last_fired[fired_key] = now
                    involved = sorted({entry[1] for entry in entries})
                    events.append(
                        self._event(
                            "repeated_presence", rule["severity"], track, site, now,
                            summary=f"{len(entries)} separate visits to {zone_name} within {_duration(rule['window_s'])}",
                            zone_id=zone_id, rule=rule,
                            reason_codes=["repeated_zone_visits"],
                            started_ms=entries[0][0],
                            details={
                                "visit_count": len(entries),
                                "site_track_ids": involved,
                                "identity_note": "visits may be by the same or different people",
                            },
                        )
                    )
        return events

    def _evaluate_dwell(self, site: SiteTrack, track: Track, now: int) -> list[SecurityEvent]:
        events: list[SecurityEvent] = []
        for zone_id, presence in site.zones.items():
            if presence.left_ms is not None:
                continue
            dwell_s = (now - presence.since_ms) / 1000
            for rule in self.rules:
                if rule["type"] != "dwell" or not self._rule_matches_zone(rule, zone_id):
                    continue
                if not self._rule_active(rule, site.label, now):
                    continue
                if rule.get("require_arrival") and not site.arrived:
                    continue
                threshold_s = rule["min_dwell_s"] * self.dwell_multiplier
                key = f"{rule['id']}:{zone_id}:{presence.since_ms}"
                if dwell_s < threshold_s or key in site.fired:
                    continue
                site.fired.add(key)
                events.append(
                    self._event(
                        "dwell", rule["severity"], track, site, now,
                        summary=f"{site.label.capitalize()} has remained in {self.zones.zone_name(zone_id)} for {_duration(dwell_s)}",
                        zone_id=zone_id, rule=rule,
                        reason_codes=["prolonged_presence"],
                        started_ms=presence.since_ms,
                        details={"dwell_s": round(dwell_s, 1), "threshold_s": round(threshold_s, 1)},
                    )
                )
        return events

    def _evaluate_site_rules(self, site: SiteTrack, track: Track, now: int) -> list[SecurityEvent]:
        events: list[SecurityEvent] = []
        for rule in self.rules:
            if rule["type"] not in {"sequence", "multi_camera"} or rule["id"] in site.fired:
                continue
            if not self._rule_active(rule, site.label, now):
                continue
            if rule["type"] == "sequence":
                match = _match_sequence(site.path, rule["zones"], int(rule["within_s"] * 1000))
                if match is None:
                    continue
                site.fired.add(rule["id"])
                names = " -> ".join(self.zones.zone_name(zone) for zone in rule["zones"])
                events.append(
                    self._event(
                        "sequence", rule["severity"], track, site, now,
                        summary=f"{site.label.capitalize()} moved {names}",
                        zone_id=rule["zones"][-1], rule=rule,
                        reason_codes=["zone_sequence"],
                        started_ms=match[0],
                        confidence=min(track.confidence, site.link_confidence),
                        details={"sequence": rule["zones"], "elapsed_s": round((match[1] - match[0]) / 1000, 1)},
                    )
                )
            else:
                window_ms = int(rule["window_s"] * 1000)
                cameras = site.cameras_since(now - window_ms)
                if len(cameras) < rule["min_cameras"]:
                    continue
                site.fired.add(rule["id"])
                events.append(
                    self._event(
                        "multi_camera", rule["severity"], track, site, now,
                        summary=f"{site.label.capitalize()} moved across {len(cameras)} camera views",
                        rule=rule,
                        reason_codes=["cross_camera_movement"],
                        started_ms=min(site.first_seen_ms, now),
                        confidence=site.link_confidence,
                        details={"cameras": cameras, "links": site.links[-5:]},
                    )
                )
        return events

    def _event(
        self,
        event_type: str,
        severity: str,
        track: Track,
        site: SiteTrack,
        now: int,
        *,
        summary: str,
        zone_id: str = "",
        rule: dict[str, Any] | None = None,
        reason_codes: list[str] | None = None,
        started_ms: int | None = None,
        confidence: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> SecurityEvent:
        self._event_seq += 1
        payload = dict(details or {})
        if site.ambiguous_with:
            payload["correlation_note"] = "possible match with other sightings was too uncertain to link"
        return SecurityEvent(
            event_id=f"evt_{now}_{self._event_seq}",
            event_type=event_type,
            severity=severity,
            camera_id=track.camera_id,
            timestamp_ms=now,
            started_ms=started_ms if started_ms is not None else now,
            confidence=round(track.confidence if confidence is None else confidence, 3),
            summary=summary,
            label=site.label,
            zone_id=zone_id,
            rule_id=(rule or {}).get("id", ""),
            track_ids=[track.track_id],
            site_track_id=site.site_track_id,
            reason_codes=list(reason_codes or []),
            details=payload,
        )


def _match_sequence(path: list[tuple[int, str, str]], zones: list[str], within_ms: int) -> tuple[int, int] | None:
    for start_index, (start_ms, _, zone_id) in enumerate(path):
        if zone_id != zones[0]:
            continue
        step = 1
        for ms, _, visited in path[start_index + 1 :]:
            if ms - start_ms > within_ms:
                break
            if visited == zones[step]:
                step += 1
                if step == len(zones):
                    return (start_ms, ms)
    return None


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)} s"
    return f"{seconds / 60:.0f} min"
