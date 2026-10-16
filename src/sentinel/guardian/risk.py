"""Explainable wildlife risk levels from species, movement, persistence and
proximity (stdlib only).

The levels describe what was observed and how much attention it deserves. They
never predict an attack or state what an animal intends: "Urgent Review" means
"a person should look at this now", not "this animal will charge"."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

LEVELS = ("observed", "approaching", "elevated_concern", "urgent_review")
LEVEL_RANK = {name: rank for rank, name in enumerate(LEVELS)}
LEVEL_NAMES = {"observed": "Observed", "approaching": "Approaching", "elevated_concern": "Elevated Concern", "urgent_review": "Urgent Review"}
LEVEL_SEVERITY = {"observed": "info", "approaching": "low", "elevated_concern": "medium", "urgent_review": "high"}
CONCERNS = ("low", "unknown", "moderate", "high")
CONCERN_POINTS = {"low": -1, "unknown": 0, "moderate": 1, "high": 2}

# Words that must never appear in generated wildlife text (tested).
FORBIDDEN_CLAIMS = ("attack", "charging", "aggressive", "hunting", "stalking", "predatory", "intends", "will charge", "safe")


@dataclass(frozen=True)
class SpeciesProfile:
    display: str
    concern: str
    status: str  # detector_class (generic COCO label) | classifier (a species model said so) | unknown
    note: str = ""


# What the current detectors (COCO-trained YOLOX/DETR/EfficientDet) can actually say.
# COCO has one "bear" class (black, brown/grizzly and polar bears together) and no
# cougar, wolf, coyote, moose, elk or deer classes: such animals are often reported
# as "dog", "cat", "horse", "cow" or "sheep", or missed. Those labels are therefore
# shown as unconfirmed and never renamed into a species.
DETECTOR_PROFILES: dict[str, SpeciesProfile] = {
    "bear": SpeciesProfile("Bear (species not determined)", "high", "detector_class", "detector cannot tell black bears from grizzly bears"),
    "dog": SpeciesProfile("Dog-like animal (could be a dog, coyote or wolf)", "moderate", "detector_class", "detector has no coyote or wolf class"),
    "cat": SpeciesProfile("Cat-like animal (species not determined)", "unknown", "detector_class", "detector has no cougar class"),
    "horse": SpeciesProfile("Large hoofed animal (detector label: horse)", "moderate", "detector_class", "could be a horse, moose, elk or deer"),
    "cow": SpeciesProfile("Large hoofed animal (detector label: cow)", "moderate", "detector_class", "could be livestock, moose or elk"),
    "sheep": SpeciesProfile("Animal (detector label: sheep)", "unknown", "detector_class"),
    "elephant": SpeciesProfile("Large animal (detector label: elephant)", "unknown", "detector_class", "label unlikely in North America; check the clip"),
    "zebra": SpeciesProfile("Animal (detector label: zebra)", "unknown", "detector_class"),
    "giraffe": SpeciesProfile("Animal (detector label: giraffe)", "unknown", "detector_class"),
    "bird": SpeciesProfile("Bird", "low", "detector_class"),
}
# Labels a future species classifier (see docs/wilderness-guardian.md, Phase 1) may
# produce. Nothing in Sentinel produces them yet.
CLASSIFIER_PROFILES: dict[str, SpeciesProfile] = {
    "black_bear": SpeciesProfile("Black bear", "high", "classifier"),
    "grizzly_bear": SpeciesProfile("Grizzly bear", "high", "classifier"),
    "cougar": SpeciesProfile("Cougar", "high", "classifier"),
    "wolf": SpeciesProfile("Wolf", "high", "classifier"),
    "coyote": SpeciesProfile("Coyote", "moderate", "classifier"),
    "moose": SpeciesProfile("Moose", "high", "classifier"),
    "elk": SpeciesProfile("Elk", "moderate", "classifier"),
    "deer": SpeciesProfile("Deer", "low", "classifier"),
}
UNKNOWN = SpeciesProfile("Animal (species unknown)", "unknown", "unknown")


def species_profile(kind: str, overrides: dict[str, dict[str, Any]] | None = None) -> SpeciesProfile:
    kind = (kind or "").strip().lower()
    base = CLASSIFIER_PROFILES.get(kind) or DETECTOR_PROFILES.get(kind) or UNKNOWN
    override = (overrides or {}).get(kind)
    if override:  # the owner may raise or lower concern; display wording stays honest
        base = SpeciesProfile(base.display, override.get("concern", base.concern), base.status, base.note)
    return base


@dataclass
class RiskSettings:
    approaching_score: int = 2
    elevated_score: int = 4
    urgent_score: int = 6
    persist_s: float = 60.0
    urgent_time_to_zone_s: float = 10.0
    near_distance_m: float = 15.0

    @classmethod
    def from_dict(cls, data: dict | None, sensitivity: str = "normal") -> "RiskSettings":
        data = data or {}
        settings = cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})
        shift = {"low": 1, "normal": 0, "high": -1}.get(sensitivity, 0)
        settings.approaching_score = max(1, settings.approaching_score + shift)
        settings.elevated_score += shift
        settings.urgent_score += shift
        return settings


@dataclass
class Factor:
    code: str
    points: int
    text: str
    basis: str  # observed | inferred | estimate


@dataclass
class RiskAssessment:
    level: str
    score: int
    confidence: float
    factors: list[Factor] = field(default_factory=list)
    held_back: list[str] = field(default_factory=list)  # why the level was capped

    @property
    def name(self) -> str:
        return LEVEL_NAMES[self.level]

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "level_name": self.name,
            "score": self.score,
            "confidence": round(self.confidence, 2),
            "factors": [asdict(f) for f in self.factors],
            "held_back": list(self.held_back),
        }


def assess(
    *,
    species: SpeciesProfile,
    approach_targets: list[str],
    approach_sustained: bool,
    inside_zones: list[str],
    person_closing: bool,
    person_present: bool,
    turned_toward: bool,
    sped_up: bool,
    present_s: float,
    time_to_zone_s: float | None,
    distance_m: float | None,
    confidence: float,
    caps: list[str],
    settings: RiskSettings,
) -> RiskAssessment:
    """Additive points; every point is a named factor shown to the person."""
    factors: list[Factor] = []
    points = CONCERN_POINTS[species.concern]
    if points:
        factors.append(Factor(f"species_{species.concern}", points, f"{species.display}: {species.concern} concern", "inferred"))
    if approach_targets:
        factors.append(Factor("approaching", 1, "moving toward " + ", ".join(approach_targets), "observed"))
    if approach_sustained and (approach_targets or inside_zones):
        factors.append(Factor("sustained_approach", 1, "approach continued over several seconds", "observed"))
        if any(target != "the camera" for target in approach_targets):
            factors.append(Factor("approaching_protected", 1, "kept moving toward a protected area or person", "observed"))
    if inside_zones:
        factors.append(Factor("in_protected_zone", 2, "inside " + ", ".join(inside_zones), "observed"))
    if person_closing:
        factors.append(Factor("closing_on_person", 2, "distance to a person in view is shrinking", "observed"))
    elif person_present:
        factors.append(Factor("person_in_view", 1, "a person is in view at the same time", "observed"))
    if turned_toward:
        factors.append(Factor("turned_toward", 1, "changed direction sharply and is now approaching", "observed"))
    if sped_up and approach_targets:
        factors.append(Factor("sped_up", 1, "speed increased while approaching", "observed"))
    if present_s >= settings.persist_s:
        factors.append(Factor("persistent", 1, f"in view for {int(present_s)} s", "observed"))
    if time_to_zone_s is not None and time_to_zone_s <= settings.urgent_time_to_zone_s:
        factors.append(Factor("short_time_to_zone", 1, f"about {time_to_zone_s:.0f} s from a protected area at its current pace", "estimate"))
    if distance_m is not None and distance_m <= settings.near_distance_m:
        factors.append(Factor("near_protected", 1, f"about {distance_m:.0f} m from a protected area", "estimate"))

    score = sum(f.points for f in factors)
    engaged = bool(approach_targets or inside_zones or person_closing)
    if score >= settings.urgent_score and engaged:
        level = "urgent_review"
    elif score >= settings.elevated_score and engaged:
        level = "elevated_concern"
    elif score >= settings.approaching_score and engaged:
        level = "approaching"
    else:
        level = "observed"
    held_back = list(caps)
    if caps and level != "observed":
        level = "observed"
    return RiskAssessment(level=level, score=score, confidence=confidence, factors=factors, held_back=held_back)
