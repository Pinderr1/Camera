"""Wilderness Guardian movement monitor (stdlib only).

Consumes per-camera track updates (the same `Track`s the security engine gets),
keeps persistent guardian tracks for animals across short tracking losses, works
out how each animal moves relative to the camera, protected zones and people,
and emits explainable `wildlife_*` events when the risk level rises.

It is platform-neutral: a drone or rover camera can feed it tracks the same way,
with its own `moving_camera` flag (moving viewpoints cap every level at Observed
until ego-motion is compensated)."""

from __future__ import annotations

import dataclasses
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..models import SecurityEvent, Track
from .geometry import GroundCalibration, distance_to_polygon, point_in_polygon
from .movement import MovementEstimate, MovementSettings, Sample, distance_trend, estimate
from .risk import CONCERNS, LEVEL_NAMES, LEVEL_RANK, LEVEL_SEVERITY, LEVELS, RiskAssessment, RiskSettings, SpeciesProfile, apply_decision, assess, species_profile
from .species import SpeciesDecision, SpeciesPolicy, SpeciesSettings, TrackSpecies

TRAIL_POINTS = 30
HOLD_GAP_MS = 1000  # a level still counts as held across a dropout this short (one missed detection must not reset it)
MIXED_LABEL_SHARE = 0.25  # another detector label with at least this share of the track makes the species "mixed"
STALE_MS = 5000  # beyond reacquire_s: a track no frame has mentioned for this long is gone (e.g. its worker restarted)


@dataclass
class GuardianSettings:
    enabled: bool = False
    cameras: list[str] = field(default_factory=list)  # empty = every camera
    protected_zones: list[str] = field(default_factory=list)
    protected_zone_types: list[str] = field(default_factory=lambda: ["restricted"])
    approach_camera: bool = True  # the camera is usually on the building people use
    min_confidence: float = 0.4
    min_track_s: float = 1.0
    sustained_s: float = 3.0
    hold_s: float = 1.0
    cooldown_s: float = 120.0
    reacquire_s: float = 8.0
    reacquire_body_heights: float = 3.0
    closing_rate: float = 0.15  # body heights per second of shrinking image distance
    closing_rate_m: float = 0.3  # metres per second, calibrated cameras only
    notify_min_level: str = "elevated_concern"
    ignore_kinds: list[str] = field(default_factory=lambda: ["bird"])
    sensitivity: str = "normal"
    species: dict[str, dict[str, Any]] = field(default_factory=dict)
    species_classifier: dict[str, Any] = field(default_factory=dict)  # optional SpeciesNet, see guardian/species.py
    movement: MovementSettings = field(default_factory=MovementSettings)
    risk: RiskSettings = field(default_factory=RiskSettings)

    @classmethod
    def from_dict(cls, data: dict | None) -> "GuardianSettings":
        data = dict(data or {})
        movement = MovementSettings.from_dict(data.pop("movement", None))
        sensitivity = data.get("sensitivity", "normal")
        risk = RiskSettings.from_dict(data.pop("risk", None), sensitivity)
        settings = cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data and key not in {"movement", "risk"}})
        settings.movement, settings.risk = movement, risk
        return settings


@dataclass
class GuardianTrack:
    guardian_id: str
    camera_id: str
    track_id: str
    kind: str
    first_seen_ms: int
    last_seen_ms: int
    bbox: tuple[float, float, float, float]
    samples: deque = field(default_factory=lambda: deque(maxlen=200))
    zone_distance: dict[str, deque] = field(default_factory=dict)
    person_distance: deque = field(default_factory=lambda: deque(maxlen=100))
    lost: bool = False
    reacquired: int = 0
    ambiguous_with: list[str] = field(default_factory=list)
    approach_since_ms: int | None = None
    last_approach_ms: int = 0
    held_since: dict[str, int] = field(default_factory=dict)  # level -> since when the track has been at or above it
    held_last: dict[str, int] = field(default_factory=dict)  # level -> last time it was at or above it
    emitted: str = ""
    below_since_ms: int | None = None
    movement: MovementEstimate = field(default_factory=MovementEstimate)
    assessment: RiskAssessment | None = None
    estimates: dict[str, Any] = field(default_factory=dict)
    targets: list[str] = field(default_factory=list)
    inside: list[str] = field(default_factory=list)  # protected zone ids the ground point is in
    kind_weights: dict[str, float] = field(default_factory=dict)  # detector label -> summed confidence over the track
    resets: int = 0
    species: TrackSpecies = field(default_factory=TrackSpecies)  # classifier results for this animal, if enabled

    @property
    def anchor(self) -> tuple[float, float]:
        x, y, w, h = self.bbox
        return (x + w / 2, y + h)

    def label_shares(self) -> dict[str, float]:
        total = sum(self.kind_weights.values())
        return {kind: weight / total for kind, weight in self.kind_weights.items()} if total > 0 else {}

    def view(self, overrides: dict | None = None) -> dict[str, Any]:
        assessment = self.assessment.as_dict() if self.assessment else None
        return {
            "guardian_track_id": self.guardian_id,
            "camera_id": self.camera_id,
            "track_id": self.track_id,
            "kind": self.kind,
            "species": species_view(self, overrides),
            "bbox": [round(v, 4) for v in self.bbox],
            "trail": [[round(s[1], 4), round(s[2], 4)] for s in list(self.samples)[-TRAIL_POINTS:]],
            "lost": self.lost,
            "reacquired": self.reacquired,
            "ambiguous_with": list(self.ambiguous_with),
            "first_seen_ms": self.first_seen_ms,
            "last_seen_ms": self.last_seen_ms,
            "movement": self.movement.as_dict(),
            "targets": list(self.targets),
            "estimates": dict(self.estimates),
            "assessment": assessment,
        }


def track_profile(gtrack: GuardianTrack, overrides: dict | None = None) -> SpeciesProfile:
    """The species wording for a whole track, not one frame. Detectors often flip
    between labels (bear/dog, cat/dog) on the same animal: the majority label
    (weighted by confidence) names it, and when another label is common too the
    display says so and the higher concern of the two is used. A species
    classifier result (optional) is layered on top and never lowers that concern."""
    return apply_decision(detector_profile(gtrack, overrides), gtrack.species.decision, overrides)


def detector_profile(gtrack: GuardianTrack, overrides: dict | None = None) -> SpeciesProfile:
    shares = gtrack.label_shares()
    base = species_profile(gtrack.kind, overrides)
    others = [kind for kind, share in shares.items() if kind != gtrack.kind and share >= MIXED_LABEL_SHARE]
    if not others:
        return base
    profiles = [base] + [species_profile(kind, overrides) for kind in others]
    concern = max((p.concern for p in profiles), key=CONCERNS.index)
    labels = ", ".join(f"{kind} {shares[kind] * 100:.0f} %" for kind in sorted(shares, key=shares.get, reverse=True) if shares[kind] >= MIXED_LABEL_SHARE)
    return SpeciesProfile(f"Animal (species not determined; detector labels: {labels})", concern, "detector_class", "the detector gave this animal more than one label")


def species_view(gtrack: GuardianTrack, overrides: dict | None = None) -> dict[str, Any]:
    profile = track_profile(gtrack, overrides)
    shares = {kind: round(share, 2) for kind, share in gtrack.label_shares().items()}
    decision = gtrack.species.decision
    confirmed = decision is not None and decision.status == "confirmed"
    return {
        "display": profile.display, "concern": profile.concern, "status": profile.status, "note": profile.note,
        "detector_label": gtrack.kind, "detector_labels": shares,
        "species_confidence": round(decision.confidence, 3) if confirmed else None,
        # Species-specific safety guidance may only ever follow a confirmed species.
        "species_specific_guidance_allowed": confirmed,
        "classifier": decision.as_dict() if decision else None,
    }


def overlay_label(profile: SpeciesProfile, decision: SpeciesDecision | None) -> str:
    if profile.status == "classifier" and decision is not None:
        name = profile.display.split(" —")[0].split(" (")[0]
        return f"{name} {decision.confidence:.0%}"
    if profile.status == "classifier_uncertain":
        return profile.display.split(" detected")[0] + "?"
    return profile.display.split(" (")[0]


class GuardianMonitor:
    def __init__(self, config: dict[str, Any], next_id: int = 1) -> None:
        """`next_id` continues the numbering of a monitor this one replaces (a
        config edit), so two different animals never share a guardian id."""
        self.settings = GuardianSettings.from_dict(config.get("guardian"))
        self.zones: dict[str, list[dict[str, Any]]] = {}
        self.calibration: dict[str, GroundCalibration | None] = {}
        self.moving_camera: dict[str, bool] = {}
        protected_ids = set(self.settings.protected_zones)
        for camera in config.get("cameras", []):
            options = camera.get("guardian", {})
            self.calibration[camera["id"]] = GroundCalibration.from_config(options.get("calibration"))
            self.moving_camera[camera["id"]] = bool(options.get("moving_camera", False))
            self.zones[camera["id"]] = [
                zone for zone in camera.get("zones", [])
                if zone["id"] in protected_ids or (not protected_ids and zone.get("type") in self.settings.protected_zone_types)
            ]
        self.tracks: dict[str, GuardianTrack] = {}
        self._by_track: dict[str, str] = {}
        self._camera_motion_until: dict[str, int] = {}
        self._next_id = next_id
        self._seq = 0
        self.species_settings = SpeciesSettings.from_dict(self.settings.species_classifier)
        self.species_policy: SpeciesPolicy | None = None  # attached once a classifier is ready

    @property
    def next_id(self) -> int:
        return self._next_id

    def watches(self, camera_id: str) -> bool:
        return self.settings.enabled and (not self.settings.cameras or camera_id in self.settings.cameras)

    # ---- species classifier (optional) ------------------------------------

    def attach_species(self, policy: SpeciesPolicy | None) -> None:
        self.species_policy = policy

    def species_requests(self, camera_id: str, now_ms: int) -> list[tuple[str, tuple[float, float, float, float]]]:
        """Guardian tracks seen in this frame that are due for a (re)classification.
        Each animal is classified a few times while visible, never every frame."""
        if self.species_policy is None or not self.watches(camera_id):
            return []
        due = []
        for gtrack in self.tracks.values():
            if gtrack.camera_id != camera_id or gtrack.lost or gtrack.last_seen_ms != now_ms:
                continue
            if gtrack.species.due(now_ms, self.species_settings):
                gtrack.species.pending, gtrack.species.last_request_ms = True, now_ms
                due.append((gtrack.guardian_id, gtrack.bbox))
        return due

    def species_result(self, guardian_id: str, scores: dict[int, float]) -> SpeciesDecision | None:
        gtrack = self.tracks.get(guardian_id)
        if gtrack is None or self.species_policy is None:
            return None
        return gtrack.species.add(scores, self.species_policy)

    def species_skipped(self, guardian_id: str) -> None:
        gtrack = self.tracks.get(guardian_id)
        if gtrack is not None:
            gtrack.species.pending = False

    # ---- update ----------------------------------------------------------

    def process(
        self, camera_id: str, now_ms: int, active: list[Track], ended: list[Track], site_ids: dict[str, str] | None = None
    ) -> list[SecurityEvent]:
        if not self.watches(camera_id):
            return []
        settings = self.settings
        animals = [t for t in active if t.label == "animal" and (t.kind or "").lower() not in settings.ignore_kinds]
        people = [t for t in active if t.label == "person"]
        for track in ended:
            gid = self._by_track.pop(track.track_id, None)
            if gid and gid in self.tracks:
                self.tracks[gid].lost = True
        self._expire(camera_id, now_ms)
        self._link_new(camera_id, animals, now_ms)
        camera_motion = self._camera_motion(camera_id, active, now_ms)
        events: list[SecurityEvent] = []
        for track in animals:
            gtrack = self.tracks[self._by_track[track.track_id]]
            self._observe(gtrack, track, now_ms)
            assessment = self._assess(gtrack, track, people, now_ms, camera_motion)
            event = self._maybe_emit(gtrack, track, assessment, now_ms, (site_ids or {}).get(track.track_id, ""))
            if event:
                events.append(event)
        return events

    def _expire(self, camera_id: str, now_ms: int) -> None:
        limit = self.settings.reacquire_s * 1000
        for gid, gtrack in list(self.tracks.items()):
            if gtrack.camera_id != camera_id:
                continue
            age = now_ms - gtrack.last_seen_ms
            # A track that was never reported as ended (its camera worker restarted) is dropped too.
            if (gtrack.lost and age > limit) or age > limit + STALE_MS:
                del self.tracks[gid]
                if self._by_track.get(gtrack.track_id) == gid:
                    del self._by_track[gtrack.track_id]

    def _link_new(self, camera_id: str, animals: list[Track], now_ms: int) -> None:
        """A new camera track continues a recently lost guardian track only when it
        appears where that animal was heading and nothing else could explain it.
        Two candidates either way means a fresh guardian track (never a guess)."""
        new = [t for t in animals if t.track_id not in self._by_track]
        active_ids = {t.track_id for t in animals}
        # Lost, or no longer matched by the camera tracker (it may keep a missed track for a few seconds).
        lost = [
            g for g in self.tracks.values()
            if g.camera_id == camera_id and g.track_id not in active_ids and (g.lost or now_ms - g.last_seen_ms >= 500)
        ]
        pairs: dict[str, list[str]] = {}
        reverse: dict[str, list[str]] = {}
        for track in new:
            for gtrack in lost:
                if self._plausible(gtrack, track, now_ms):
                    pairs.setdefault(track.track_id, []).append(gtrack.guardian_id)
                    reverse.setdefault(gtrack.guardian_id, []).append(track.track_id)
        for track in new:
            candidates = pairs.get(track.track_id, [])
            if len(candidates) == 1 and len(reverse[candidates[0]]) == 1:
                gtrack = self.tracks[candidates[0]]
                self._by_track.pop(gtrack.track_id, None)
                gtrack.lost, gtrack.track_id = False, track.track_id
                gtrack.reacquired += 1
                self._by_track[track.track_id] = gtrack.guardian_id
                continue
            gid = f"g:{camera_id}:{self._next_id}"
            self._next_id += 1
            self.tracks[gid] = GuardianTrack(gid, camera_id, track.track_id, track.kind, track.first_seen_ms, now_ms, track.bbox)
            self.tracks[gid].ambiguous_with = list(candidates) if len(candidates) > 1 or any(len(reverse[c]) > 1 for c in candidates) else []
            self._by_track[track.track_id] = gid

    def _plausible(self, gtrack: GuardianTrack, track: Track, now_ms: int) -> bool:
        if gtrack.kind and track.kind and gtrack.label_shares().get(track.kind, 0.0) < MIXED_LABEL_SHARE:
            return False  # a label this animal was rarely or never given
        dt = min((now_ms - gtrack.last_seen_ms) / 1000, self.settings.reacquire_s)
        vx, vy = gtrack.movement.velocity
        predicted = (gtrack.anchor[0] + vx * dt, gtrack.anchor[1] + vy * dt)
        body = max(gtrack.bbox[3], track.bbox[3], 1e-6)
        ratio = track.bbox[3] / max(gtrack.bbox[3], 1e-6)
        distance = min(math.dist(predicted, track.anchor), math.dist(gtrack.anchor, track.anchor))
        return 0.5 <= ratio <= 2.0 and distance <= self.settings.reacquire_body_heights * body

    def _camera_motion(self, camera_id: str, active: list[Track], now_ms: int) -> bool:
        """Every tracked object sliding the same way at the same rate is more likely
        the camera moving (PTZ, wind, a vehicle mount) than a coordinated herd."""
        moving = [t for t in active if math.hypot(*t.velocity) / max(t.bbox[3], 1e-6) >= 0.3]
        if len(active) >= 2 and len(moving) == len(active):
            ref = moving[0].velocity
            coherent = all(
                (ref[0] * t.velocity[0] + ref[1] * t.velocity[1]) / (math.hypot(*ref) * math.hypot(*t.velocity)) >= 0.95
                and 0.7 <= math.hypot(*t.velocity) / math.hypot(*ref) <= 1.4
                for t in moving[1:]
            )
            if coherent:
                self._camera_motion_until[camera_id] = now_ms + 3000
        return now_ms <= self._camera_motion_until.get(camera_id, 0)

    def _observe(self, gtrack: GuardianTrack, track: Track, now_ms: int) -> None:
        x, y, w, h = track.bbox
        anchor = track.anchor
        if gtrack.samples:
            last = gtrack.samples[-1]
            body = max(h, last[4], 1e-6)
            if math.dist((last[1], last[2]), anchor) > self.settings.movement.jump_body_heights * body and now_ms - last[0] < 1000:
                gtrack.samples.clear()  # tracker swapped boxes: restart kinematics rather than invent a dash
                gtrack.zone_distance.clear()
                gtrack.person_distance.clear()
                gtrack.resets += 1
        edge = x <= 0.005 or y <= 0.005 or x + w >= 0.995 or y + h >= 0.995
        gtrack.samples.append((now_ms, anchor[0], anchor[1], w, h, track.confidence, edge))
        gtrack.bbox, gtrack.last_seen_ms, gtrack.track_id = track.bbox, now_ms, track.track_id
        if track.kind:
            gtrack.kind_weights[track.kind] = gtrack.kind_weights.get(track.kind, 0.0) + max(track.confidence, 0.01)
            gtrack.kind = max(gtrack.kind_weights, key=gtrack.kind_weights.get)

    def _assess(self, gtrack: GuardianTrack, track: Track, people: list[Track], now_ms: int, camera_motion: bool) -> RiskAssessment:
        settings = self.settings
        camera_id = gtrack.camera_id
        movement = estimate(gtrack.samples, now_ms, settings.movement)
        gtrack.movement = movement
        calibration = self.calibration.get(camera_id)
        anchor = gtrack.anchor
        body = movement.body_height or track.bbox[3] or 1e-6
        ground = _safe_ground(calibration, anchor)
        targets: list[str] = []
        inside: list[str] = []
        inside_ids: list[str] = []
        estimates: dict[str, Any] = {"calibrated": calibration is not None and ground is not None}
        time_to_zone: float | None = None
        nearest_m: float | None = None
        window = settings.movement.window_s

        for zone in self.zones.get(camera_id, []):
            name = zone.get("name", zone["id"])
            if point_in_polygon(anchor[0], anchor[1], zone["polygon"]):
                inside.append(name)
                inside_ids.append(zone["id"])
                continue
            if ground is not None:
                polygon = [p for p in (_safe_ground(calibration, tuple(pt)) for pt in zone["polygon"]) if p is not None]
                distance, rate = (distance_to_polygon(ground, polygon) if len(polygon) >= 3 else None), settings.closing_rate_m
            else:
                distance, rate = distance_to_polygon(anchor, zone["polygon"]) / body, settings.closing_rate
            if distance is None:
                continue
            series = gtrack.zone_distance.setdefault(zone["id"], deque(maxlen=100))
            series.append((now_ms, distance))
            fitted = distance_trend(series, now_ms, window)
            if fitted and fitted[0] <= -rate:
                targets.append(name)
                if ground is not None:
                    closing = -fitted[0]
                    eta = max(0.0, fitted[1]) / closing
                    time_to_zone = eta if time_to_zone is None else min(time_to_zone, eta)
            if ground is not None:
                nearest_m = distance if nearest_m is None else min(nearest_m, distance)

        person_closing = person_nearing = False
        if people:
            nearest: Track | None = None
            if ground is not None:
                pairs = [(math.dist(ground, p), person) for person in people if (p := _safe_ground(calibration, person.anchor)) is not None]
                gap, nearest = min(pairs, key=lambda pair: pair[0], default=(None, None))
                rate = settings.closing_rate_m
            else:
                gap, nearest = min(((math.dist(anchor, person.anchor) / max(body, person.bbox[3]), person) for person in people), key=lambda pair: pair[0])
                rate = settings.closing_rate
            if gap is not None and nearest is not None:
                gtrack.person_distance.append((now_ms, gap))
                fitted = distance_trend(gtrack.person_distance, now_ms, window)
                if fitted and fitted[0] <= -rate:
                    # Who is closing the gap? Only the animal's own movement toward the person counts as
                    # "moving toward a person"; a person walking up to a still animal is reported as that.
                    dx, dy = nearest.anchor[0] - anchor[0], nearest.anchor[1] - anchor[1]
                    toward = (movement.velocity[0] * dx + movement.velocity[1] * dy) / (math.hypot(dx, dy) or 1e-6) / body
                    if toward >= settings.closing_rate:
                        person_closing = True
                    else:
                        person_nearing = True
                if ground is not None:
                    estimates["distance_to_person_m"] = round(gap, 1)
        if person_closing:
            targets.append("a person")
        if settings.approach_camera and movement.movement == "approaching":
            targets.append("the camera")

        if targets or inside:  # reaching a protected zone continues the approach
            gtrack.approach_since_ms = gtrack.approach_since_ms or now_ms
            gtrack.last_approach_ms = now_ms
        elif gtrack.approach_since_ms and now_ms - gtrack.last_approach_ms > 1000:
            gtrack.approach_since_ms = None
        approach_s = (now_ms - gtrack.approach_since_ms) / 1000 if gtrack.approach_since_ms else 0.0
        if ground is not None:
            if nearest_m is not None:
                estimates["distance_to_zone_m"] = round(nearest_m, 1)
            if time_to_zone is not None:
                estimates["time_to_zone_s"] = round(time_to_zone, 1)
            estimates["calibration"] = calibration.status
            estimates["assumptions"] = "flat ground; box bottom is where the animal stands; approximate"
        gtrack.estimates = estimates
        gtrack.targets = targets
        gtrack.inside = inside_ids

        window_samples = [s for s in gtrack.samples if s[0] >= now_ms - window * 1000]
        mean_conf = sum(s[5] for s in window_samples) / len(window_samples) if window_samples else track.confidence
        confidence = mean_conf * min(1.0, len(window_samples) / 10) * (1.0 if movement.movement != "unclear" else 0.6)
        caps: list[str] = []
        if (now_ms - gtrack.first_seen_ms) / 1000 < settings.min_track_s or mean_conf < settings.min_confidence:
            caps.append("low_confidence")
        if self.moving_camera.get(camera_id):
            caps.append("camera_moves")
        if camera_motion:
            caps.append("camera_motion_suspected")
        profile = track_profile(gtrack, settings.species)
        assessment = assess(
            species=profile,
            approach_targets=targets,
            approach_sustained=approach_s >= settings.sustained_s,
            inside_zones=inside,
            person_closing=person_closing,
            person_nearing=person_nearing,
            person_present=bool(people),
            turned_toward=movement.turned and bool(targets),
            sped_up=movement.sped_up,
            present_s=(now_ms - gtrack.first_seen_ms) / 1000,
            time_to_zone_s=time_to_zone,
            distance_m=nearest_m,
            confidence=confidence,
            caps=caps,
            settings=settings.risk,
        )
        gtrack.estimates["approach_s"] = round(approach_s, 1)
        gtrack.assessment = assessment
        return assessment

    def _maybe_emit(self, gtrack: GuardianTrack, track: Track, assessment: RiskAssessment, now_ms: int, site_track_id: str) -> SecurityEvent | None:
        """Emit the highest level held for `hold_s`. A level counts as held while the
        track stays at it *or above*, across dropouts up to HOLD_GAP_MS: a missed
        person detection or a score oscillating between two levels must not keep
        restarting the clock (it used to suppress every alert)."""
        rank = LEVEL_RANK[assessment.level]
        for name in LEVELS[1:]:
            if LEVEL_RANK[name] <= rank:
                if name not in gtrack.held_since or now_ms - gtrack.held_last[name] > HOLD_GAP_MS:
                    gtrack.held_since[name] = now_ms
                gtrack.held_last[name] = now_ms
            elif name in gtrack.held_last and now_ms - gtrack.held_last[name] > HOLD_GAP_MS:
                del gtrack.held_since[name], gtrack.held_last[name]
        emitted_rank = LEVEL_RANK.get(gtrack.emitted, -1)
        if rank < emitted_rank:
            gtrack.below_since_ms = gtrack.below_since_ms or now_ms
            if now_ms - gtrack.below_since_ms >= self.settings.cooldown_s * 1000:
                gtrack.emitted, gtrack.below_since_ms = assessment.level, None  # calmed down long enough: a new rise may alert again
            return None
        gtrack.below_since_ms = None
        held = [name for name in LEVELS[1:] if LEVEL_RANK[name] <= rank and now_ms - gtrack.held_since[name] >= self.settings.hold_s * 1000]
        if not held or LEVEL_RANK[held[-1]] <= emitted_rank:
            return None
        gtrack.emitted = held[-1]
        if held[-1] != assessment.level:  # the higher level has not held long enough yet
            assessment = dataclasses.replace(assessment, level=held[-1])
        return self._event(gtrack, track, assessment, now_ms, site_track_id)

    def _event(self, gtrack: GuardianTrack, track: Track, assessment: RiskAssessment, now_ms: int, site_track_id: str) -> SecurityEvent:
        self._seq += 1
        profile = track_profile(gtrack, self.settings.species)
        level = assessment.level
        notify = "always" if LEVEL_RANK[level] >= LEVEL_RANK.get(self.settings.notify_min_level, 2) else "never"
        zones = self.zones.get(gtrack.camera_id, [])
        zone_id = next((z["id"] for z in zones if z["id"] in gtrack.inside), "") or next(
            (z["id"] for z in zones if z.get("name", z["id"]) in gtrack.targets), ""
        )
        return SecurityEvent(
            event_id=f"evt_{now_ms}_wild_{gtrack.guardian_id.replace(':', '_')}_{self._seq}",
            event_type=f"wildlife_{level}",
            severity=LEVEL_SEVERITY[level],
            camera_id=gtrack.camera_id,
            timestamp_ms=now_ms,
            started_ms=gtrack.first_seen_ms,
            confidence=round(assessment.confidence, 3),
            summary=describe(profile.display, gtrack, assessment, classified=profile.status.startswith("classifier")),
            label="animal",
            zone_id=zone_id,
            track_ids=[track.track_id],
            site_track_id=site_track_id,
            reason_codes=[f.code for f in assessment.factors] + [f"held:{c}" for c in assessment.held_back] + gtrack.movement.reasons,
            details={
                "guardian": True,
                "notify": notify,
                "guardian_track_id": gtrack.guardian_id,
                "level": level,
                "level_name": LEVEL_NAMES[level],
                "assessment": assessment.as_dict(),
                "movement": gtrack.movement.as_dict(),
                "species": species_view(gtrack, self.settings.species),
                "targets": list(gtrack.targets),
                "estimates": dict(gtrack.estimates),
                "reacquired": gtrack.reacquired,
                "limits": "Movement is measured in the image. Levels describe observed movement, not what the animal intends.",
            },
        )

    # ---- views -----------------------------------------------------------

    def snapshot(self, camera_id: str | None = None) -> list[dict[str, Any]]:
        return [g.view(self.settings.species) for g in self.tracks.values() if camera_id is None or g.camera_id == camera_id]

    def overlays(self, camera_id: str) -> list[dict[str, Any]]:
        """Compact drawing data for the live view: box, trail, heading, level."""
        out = []
        for g in self.tracks.values():
            if g.camera_id != camera_id or g.lost:
                continue
            out.append({
                "track_id": g.track_id,
                "bbox": g.bbox,
                "trail": [(s[1], s[2]) for s in list(g.samples)[-TRAIL_POINTS:]],
                "velocity": g.movement.velocity,
                "movement": g.movement.movement,
                "level": g.assessment.level if g.assessment else "observed",
                "label": f"{overlay_label(track_profile(g, self.settings.species), g.species.decision)} {g.guardian_id.rsplit(':', 1)[-1]}",
            })
        return out


def describe(display: str, gtrack: GuardianTrack, assessment: RiskAssessment, classified: bool = False) -> str:
    """With a classifier result: 'Black bear — 91% species confidence — moving toward
    Driveway (...)'. Without: 'Bear (species not determined) moving toward ...'."""
    codes = {f.code for f in assessment.factors}
    approach_s = gtrack.estimates.get("approach_s", 0)
    parts = []
    inside = next((f.text for f in assessment.factors if f.code == "in_protected_zone"), "")
    if inside:
        parts.append(inside)
    if gtrack.targets:
        phrase = "moving toward " + ", ".join(gtrack.targets)
        if approach_s >= 1:
            phrase += f" for {approach_s:.0f} s"
        parts.append(phrase)
    if "person_nearing" in codes:
        parts.append("in view while a person gets closer to it")
    if "lingering" in codes:
        parts.append(next(f.text for f in assessment.factors if f.code == "lingering"))
    if "turned_toward" in codes:
        parts.append("after a sharp change of direction")
    if gtrack.estimates.get("time_to_zone_s") is not None:
        parts.append(f"about {gtrack.estimates['time_to_zone_s']:.0f} s from a protected area at its current pace (estimate)")
    joiner = " — " if classified else " "
    return f"{display}{joiner}{'; '.join(parts) or 'in view'} ({LEVEL_NAMES[assessment.level]})"


def _safe_ground(calibration: GroundCalibration | None, point: tuple[float, float]) -> tuple[float, float] | None:
    if calibration is None:
        return None
    try:
        return calibration.to_ground(point)
    except ValueError:
        return None


__all__ = ["GuardianMonitor", "GuardianSettings", "LEVELS", "species_view"]
