"""Household-member recognition settings (`identity` config block). Standard library only.

Recognition is off by default. When it is on, it runs only on people inside
the zones listed in `identity.zones` (never on street, sidewalk or ignore
zones), only against members enrolled with a recorded consent, and nothing
about a face that matched nobody is kept."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

DEFAULT_DETECTOR = "models/face/face_detection_yunet_2023mar.onnx"
DEFAULT_RECOGNIZER = "models/face/face_recognition_sface_2021dec.onnx"
# Public or excluded areas: recognition there would identify passers-by who never agreed to it.
FORBIDDEN_ZONE_TYPES = ("street", "sidewalk", "ignore")
STATUSES = ("known", "unknown", "unverified")

# key: (minimum, maximum) for numeric settings
LIMITS: dict[str, tuple[float, float]] = {
    "match_threshold": (0.30, 0.95), "unknown_below": (0.0, 0.90), "min_margin": (0.0, 0.5),
    "min_agreeing": (1, 20), "min_unknown": (1, 20), "min_face_px": (24, 400), "min_face_score": (0.5, 0.99),
    "recheck_s": (0.1, 30), "max_attempts": (1, 500), "queue_size": (1, 64), "cpu_threads": (1, 8),
    "enroll_min_face_px": (48, 1000), "enroll_min_photos": (1, 20), "max_templates": (1, 50),
}


@dataclass(frozen=True)
class IdentitySettings:
    enabled: bool = False
    zones: tuple[str, ...] = ()
    detector_model: str = DEFAULT_DETECTOR
    recognizer_model: str = DEFAULT_RECOGNIZER
    # Cosine similarity of SFace embeddings. OpenCV's reference operating point is 0.363;
    # Sentinel is stricter and leaves a band between `unknown_below` and the match
    # threshold where it says nothing ("unverified") rather than guess.
    match_threshold: float = 0.45
    unknown_below: float = 0.30
    min_margin: float = 0.06  # best member must beat the second-best member by this much
    min_agreeing: int = 3  # matching face observations needed before a person is "known"
    min_unknown: int = 3  # clear, non-matching face observations needed before "unknown"
    min_face_px: int = 40  # smaller faces are not compared at all
    min_face_score: float = 0.85
    recheck_s: float = 0.5  # per track, at most one face check this often
    max_attempts: int = 40  # per camera track
    queue_size: int = 4
    cpu_threads: int = 1
    enroll_min_face_px: int = 80
    enroll_min_photos: int = 3
    max_templates: int = 20

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "IdentitySettings":
        data = dict(data or {})
        known = {f.name for f in fields(cls)}
        values = {key: data[key] for key in known if key in data}
        if "zones" in values:
            values["zones"] = tuple(values["zones"])
        return cls(**values)

    def public(self) -> dict[str, Any]:
        return {f.name: (list(getattr(self, f.name)) if f.name == "zones" else getattr(self, f.name)) for f in fields(self)}
