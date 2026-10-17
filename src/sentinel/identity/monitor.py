"""Per-track recognition state, recognition zones and cross-camera status.
Standard library only; runs under the runtime lock.

The monitor decides *where* and *when* a face may be checked (person tracks
whose ground point is inside a recognition zone, at most every `recheck_s`, at
most `max_attempts` per track), keeps the evidence per camera track, and
derives a status per site track (the correlator's cross-camera subject). It
never merges subjects: a face match labels the site track the correlator
already built, and evidence from linked camera tracks only adds up. Linked
tracks that point to different members make the subject "unverified"."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from senior_safety.pose_events import point_in_polygon

from ..models import Track, canonical_class
from .matching import Evidence, FaceObservation, combine, decide
from .settings import IdentitySettings

SITE_MEMORY_MS = 600_000  # evidence of a subject whose tracks all ended is kept this long (site tracks expire sooner)


@dataclass
class _TrackState:
    track_id: str
    camera_id: str
    site_id: str = ""
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    in_zone: bool = False
    zone_id: str = ""
    last_ms: int = 0
    next_due_ms: int = 0
    pending: bool = False
    ended: bool = False
    evidence: Evidence = field(default_factory=Evidence)


class IdentityMonitor:
    def __init__(self, config: dict[str, Any], settings: IdentitySettings | None = None) -> None:
        self.settings = settings or IdentitySettings.from_dict(config.get("identity"))
        self.zones: dict[str, list[tuple[str, list]]] = {}
        allowed = set(self.settings.zones)
        for camera in config.get("cameras", []):
            for zone in camera.get("zones", []):
                if zone.get("id") in allowed:
                    self.zones.setdefault(camera["id"], []).append((zone["id"], zone["polygon"]))
        self.tracks: dict[str, _TrackState] = {}
        self.names: dict[str, str] = {}  # member id -> display name, set when the gallery loads
        self.members_ready = False  # at least one member finished enrollment and the face worker runs
        self._reported: dict[str, str] = {}  # site id -> last status reported as an event
        self._site_seen: dict[str, int] = {}

    # ---- camera thread (under the runtime lock) ------------------------------

    def observe(self, camera_id: str, now_ms: int, active: list[Track], ended: list[Track], track_to_site: dict[str, str]) -> None:
        for track in active:
            if canonical_class(track.label) != "person" or not track.confirmed:
                continue
            state = self.tracks.get(track.track_id)
            if state is None:
                state = self.tracks[track.track_id] = _TrackState(track.track_id, camera_id)
            state.bbox, state.last_ms = track.bbox, now_ms
            state.site_id = track_to_site.get(track.track_id, state.site_id)
            state.zone_id = next((zone_id for zone_id, polygon in self.zones.get(camera_id, ()) if point_in_polygon(*track.anchor, polygon)), "")
            state.in_zone = bool(state.zone_id)
            if state.site_id:
                self._site_seen[state.site_id] = now_ms
        for track in ended:
            state = self.tracks.get(track.track_id)
            if state is not None:
                state.ended, state.pending = True, False
        for track_id, state in list(self.tracks.items()):  # forget subjects long gone
            if state.ended and now_ms - self._site_seen.get(state.site_id, state.last_ms) > SITE_MEMORY_MS:
                del self.tracks[track_id]
        for site_id, seen in list(self._site_seen.items()):
            if now_ms - seen > SITE_MEMORY_MS:
                del self._site_seen[site_id]
                self._reported.pop(site_id, None)

    def requests(self, camera_id: str, now_ms: int) -> list[tuple[str, tuple[float, float, float, float]]]:
        """Person boxes due for a face check on this camera. Marks them pending."""
        if not (self.settings.enabled and self.members_ready):
            return []
        due = []
        for state in self.tracks.values():
            if state.camera_id != camera_id or state.ended or state.pending or not state.in_zone or state.last_ms != now_ms:
                continue
            if state.evidence.attempts >= self.settings.max_attempts or now_ms < state.next_due_ms:
                continue
            state.pending = True
            # A settled subject is still re-checked, ten times less often, so a wrong early answer can change.
            state.next_due_ms = now_ms + int(self.settings.recheck_s * 1000 * (10 if self._settled(state) else 1))
            due.append((state.track_id, state.bbox))
        return due

    def _settled(self, state: _TrackState) -> bool:
        """Known or unknown with plenty of evidence: checking as often only costs CPU."""
        status, _, _ = decide(state.evidence, self.settings)
        return status != "unverified" and state.evidence.faces_compared() >= 3 * max(self.settings.min_agreeing, self.settings.min_unknown)

    # ---- face worker results (under the runtime lock) ------------------------

    def result(self, track_id: str, observation: FaceObservation) -> list[dict[str, Any]]:
        """Record one face observation; returns status changes of the subject to report."""
        state = self.tracks.get(track_id)
        if state is None:
            return []
        state.pending = False
        state.evidence.add(observation)
        return self._transitions([state.site_id] if state.site_id else [], state)

    def skipped(self, track_id: str) -> None:
        state = self.tracks.get(track_id)
        if state is not None:
            state.pending = False

    def forget_member(self, member_id: str) -> list[dict[str, Any]]:
        """Consent withdrawn: earlier matches to this member no longer count."""
        for state in self.tracks.values():
            state.evidence.forget(member_id)
        self.names.pop(member_id, None)
        sites = {state.site_id for state in self.tracks.values() if state.site_id}
        changes = []
        for site_id in sites:
            changes += self._transitions([site_id], next(s for s in self.tracks.values() if s.site_id == site_id))
        return changes

    def _transitions(self, site_ids: Iterable[str], state: _TrackState) -> list[dict[str, Any]]:
        changes = []
        for site_id in site_ids:
            identity = self.site_identity(site_id)
            previous = self._reported.get(site_id, "unverified")
            if identity["status"] == previous:
                continue
            self._reported[site_id] = identity["status"]
            changes.append({"site_track_id": site_id, "track_id": state.track_id, "camera_id": state.camera_id,
                            "zone_id": state.zone_id, "previous": previous, "identity": identity})
        return changes

    # ---- queries -----------------------------------------------------------------

    def site_identity(self, site_id: str) -> dict[str, Any]:
        """{status, member_id, display_name, reason, deciding, evidence} for a cross-camera subject."""
        members = [state for state in self.tracks.values() if site_id and state.site_id == site_id]
        evidence = combine(state.evidence for state in members)
        if not self.settings.enabled:
            status, member_id, reason = "unverified", "", "recognition_off"
        elif not self.members_ready:
            status, member_id, reason = "unverified", "", "no_members_enrolled"
        else:
            status, member_id, reason = decide(evidence, self.settings)
            if status == "unverified" and evidence.attempts == 0 and not any(state.in_zone for state in members):
                reason = "outside_recognition_zones"
        deciding = status == "unverified" and self.settings.enabled and self.members_ready and any(
            state.in_zone and not state.ended and state.evidence.attempts < self.settings.max_attempts for state in members)
        return {"status": status, "member_id": member_id, "display_name": self.names.get(member_id, "") if member_id else "",
                "reason": reason, "deciding": deciding, "evidence": evidence.summary()}

    def track_identity(self, track_id: str) -> dict[str, Any] | None:
        state = self.tracks.get(track_id)
        return None if state is None else self.site_identity(state.site_id) if state.site_id else None

    def overlays(self, camera_id: str) -> dict[str, str]:
        """track id -> short label for the live view ("household member", "not recognized")."""
        labels = {}
        for state in self.tracks.values():
            if state.camera_id != camera_id or state.ended:
                continue
            identity = self.site_identity(state.site_id) if state.site_id else None
            if identity and identity["status"] == "known":
                labels[state.track_id] = identity["display_name"] or "household member"
            elif identity and identity["status"] == "unknown":
                labels[state.track_id] = "not recognized"
        return labels

    def snapshot(self) -> list[dict[str, Any]]:
        sites = sorted({state.site_id for state in self.tracks.values() if state.site_id and not state.ended})
        return [{"site_track_id": site_id, **self.site_identity(site_id),
                 "cameras": sorted({s.camera_id for s in self.tracks.values() if s.site_id == site_id})} for site_id in sites]
