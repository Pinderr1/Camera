from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

SEVERITIES = ("info", "low", "medium", "high")
SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}

# Security categories and the COCO labels (what YOLOX/EfficientDet were trained
# on) that feed them. `unknown_motion` comes from motion analysis, not a detector.
# COCO has no "package/parcel", "insect", or "vegetation" classes: see
# docs/environmental-intelligence.md for what needs other models or motion analysis.
CATEGORY_LABELS: dict[str, tuple[str, ...]] = {
    "person": ("person",),
    "vehicle": ("car", "truck", "bus", "motorcycle"),
    "bicycle": ("bicycle",),
    "animal": ("bird", "cat", "dog", "horse", "sheep", "cow", "bear", "elephant", "zebra", "giraffe"),
    "bag": ("backpack", "handbag", "suitcase"),
    "unknown_motion": (),
}
# Defaults: everything recognised is recorded; only people and vehicles may alert.
DEFAULT_CATEGORIES: dict[str, dict[str, bool]] = {
    "person": {"detect": True, "alerts": True},
    "vehicle": {"detect": True, "alerts": True},
    "bicycle": {"detect": True, "alerts": False},
    "animal": {"detect": True, "alerts": False},
    "bag": {"detect": True, "alerts": False},
    "unknown_motion": {"detect": False, "alerts": False},
}
CANONICAL_CLASSES = {label: category for category, labels in CATEGORY_LABELS.items() for label in labels}


def effective_categories(config: dict, camera: dict | None = None) -> dict[str, dict[str, bool]]:
    """Defaults, overridden by site-wide `categories`, overridden per camera."""
    merged = {name: dict(values) for name, values in DEFAULT_CATEGORIES.items()}
    for layer in (config.get("categories", {}), (camera or {}).get("categories", {})):
        for name, values in layer.items():
            merged.setdefault(name, {"detect": False, "alerts": False}).update(values)
    return merged


def detector_labels(categories: dict[str, dict[str, bool]]) -> list[str]:
    """COCO labels the detector should report, given enabled categories."""
    return [label for name, values in categories.items() if values.get("detect") for label in CATEGORY_LABELS.get(name, ())]


def canonical_class(label: str) -> str:
    label = label.strip().lower()
    return CANONICAL_CLASSES.get(label, label)


def severity_at_least(severity: str, minimum: str) -> bool:
    return SEVERITY_RANK.get(severity, -1) >= SEVERITY_RANK.get(minimum, len(SEVERITIES))


def max_severity(*severities: str) -> str:
    return max(severities, key=lambda name: SEVERITY_RANK.get(name, -1), default="info")


BBox = tuple[float, float, float, float]  # x, y, w, h normalized to the frame


@dataclass(frozen=True)
class Detection:
    label: str
    confidence: float
    bbox: BBox
    kind: str = ""  # specific detector label ("dog") when `label` is a category ("animal")

    @property
    def anchor(self) -> tuple[float, float]:
        """Bottom-centre of the box: where the object touches the ground."""
        x, y, w, h = self.bbox
        return (x + w / 2, y + h)


@dataclass
class Track:
    track_id: str
    camera_id: str
    label: str
    bbox: BBox
    confidence: float
    first_seen_ms: int
    last_seen_ms: int
    hits: int = 1
    missed_frames: int = 0
    confirmed: bool = False
    kind: str = ""  # most recent specific label, e.g. "dog" for an "animal" track
    velocity: tuple[float, float] = (0.0, 0.0)  # bbox x/y change per second
    first_anchor: tuple[float, float] = (0.0, 0.0)
    max_displacement: float = 0.0
    appearance: list[float] | None = None
    history: deque = field(default_factory=lambda: deque(maxlen=120))

    @property
    def anchor(self) -> tuple[float, float]:
        x, y, w, h = self.bbox
        return (x + w / 2, y + h)

    @property
    def age_s(self) -> float:
        return (self.last_seen_ms - self.first_seen_ms) / 1000.0

    def summary(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "camera_id": self.camera_id,
            "label": self.label,
            "bbox": [round(value, 4) for value in self.bbox],
            "confidence": round(self.confidence, 3),
            "first_seen_ms": self.first_seen_ms,
            "last_seen_ms": self.last_seen_ms,
        }


@dataclass
class SecurityEvent:
    event_id: str
    event_type: str
    severity: str
    camera_id: str
    timestamp_ms: int
    started_ms: int
    confidence: float
    summary: str
    label: str = ""
    zone_id: str = ""
    rule_id: str = ""
    track_ids: list[str] = field(default_factory=list)
    site_track_id: str = ""
    reason_codes: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    incident_id: str = ""
    snapshot_path: str = ""
    clip_path: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Incident:
    incident_id: str
    opened_ms: int
    updated_ms: int
    severity: str
    title: str
    status: str = "open"  # open | closed
    closed_ms: int | None = None
    cameras: list[str] = field(default_factory=list)
    zones: list[str] = field(default_factory=list)
    site_track_ids: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    event_types: list[str] = field(default_factory=list)
    notified_severity: str = ""
    peak_event_severity: str = "info"
    acknowledged_ms: int | None = None
    acknowledged_by: str = ""
    resolution: str = ""
    resolved_by: str = ""
    clip_paths: list[str] = field(default_factory=list)
    snapshot_path: str = ""
    protected: bool = False  # recordings kept past normal retention and never purged for disk space
    labels: list[str] = field(default_factory=list)  # object categories involved (person, vehicle, ...)
    clip_starts: dict[str, dict[str, Any]] = field(default_factory=dict)  # clip path -> {camera_id, started_ms}
    notes: list[dict[str, Any]] = field(default_factory=list)  # operator annotations: {by, ms, text}

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Notification:
    incident_id: str
    severity: str
    title: str
    message: str
    priority: int
    camera_ids: tuple[str, ...]
    timestamp_ms: int
    snapshot_path: str = ""
    kind: str = "incident"  # incident | escalation | system | digest

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["camera_ids"] = list(self.camera_ids)
        return payload
