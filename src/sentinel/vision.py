"""Frame-level helpers. OpenCV/numpy are imported lazily so the event engine
and its tests stay standard-library only."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Detection, Track


@dataclass
class Frame:
    image: Any  # numpy BGR array
    timestamp_ms: int
    truth: list[Detection] | None = None  # simulated sources only
    meta: dict[str, Any] = field(default_factory=dict)


def apply_privacy_masks(image: Any, masks: list[list[list[float]]]) -> Any:
    """Black out masked regions before detection, recording, or streaming, so
    neighbours' windows and similar areas are never processed or stored."""
    if not masks:
        return image
    import cv2
    import numpy as np

    height, width = image.shape[:2]
    for mask in masks:
        points = np.array([[int(x * width), int(y * height)] for x, y in mask], dtype=np.int32)
        cv2.fillPoly(image, [points], (0, 0, 0))
    return image


def appearance_vector(image: Any, bbox: tuple[float, float, float, float]) -> list[float] | None:
    """Coarse hue/saturation histogram of the object crop. Only a weak hint for
    cross-camera linking; never used to identify anyone."""
    import cv2

    height, width = image.shape[:2]
    x, y, w, h = bbox
    x0, y0 = max(0, int(x * width)), max(0, int(y * height))
    x1, y1 = min(width, int((x + w) * width)), min(height, int((y + h) * height))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    hsv = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [8, 4], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    return [round(float(value), 4) for value in hist.flatten()]


class ContentMonitor:
    """Is the picture itself still live? Classifies each frame-to-frame change
    on sampled pixels (not averages, so sensor noise survives):

    - `noise`: small changes spread over most of the frame. Live sensors do this
      even when nothing moves, so a noisy static scene is *not* frozen.
    - `changed`: any other change over a large area (movement, light).
    - `overlay`: changes confined to one small box, such as a recorder's
      on-screen clock that keeps ticking over a frozen picture.
    - `identical`: nothing changed.

    A run of `overlay`/`identical` frames is a static period. The health monitor
    calls it frozen only when the picture was noisy right before it went still
    (a live sensor does not lose its noise abruptly). A scene that was already
    noise-free cannot be told apart from a frozen feed, so it is reported as
    unverified instead of frozen."""

    def __init__(self, *, overlay_max_area: float = 0.03, fine_max_diff: int = 6, noise_cells: float = 0.75, noise_fraction: float = 0.02) -> None:
        self.overlay_max_area = overlay_max_area
        self.fine_max_diff = fine_max_diff
        self.noise_cells = noise_cells
        self.noise_fraction = noise_fraction
        self._previous = None
        self._previous_ms = 0
        self.noise_level = 0.0  # moving average of "this frame showed sensor noise" (0..1)
        self.last_change_ms = 0
        self.static_since_ms = 0
        self.static_kind = ""
        self.noisy_before_static = False

    def update(self, image: Any, timestamp_ms: int) -> dict[str, Any]:
        import numpy as np

        height, width = image.shape[:2]
        sample = image[:: max(1, height // 90), :: max(1, width // 160)].astype(np.int16)
        kind = "changed"
        if self._previous is not None and sample.shape == self._previous.shape:
            diff = np.abs(sample - self._previous)
            if diff.ndim == 3:
                diff = diff.max(axis=2)
            changed = diff > 0
            if not changed.any():
                kind = "identical"
            else:
                rows, cols = np.nonzero(changed)
                box = (rows.max() - rows.min() + 1) * (cols.max() - cols.min() + 1) / changed.size
                fine = changed & (diff <= self.fine_max_diff)
                h, w = fine.shape
                cells = [fine[r * h // 4:(r + 1) * h // 4, c * w // 4:(c + 1) * w // 4].any() for r in range(4) for c in range(4)]
                if sum(cells) / 16 >= self.noise_cells and fine.mean() >= self.noise_fraction:
                    kind = "noise"
                elif box <= self.overlay_max_area:
                    kind = "overlay"
            self.noise_level = 0.9 * self.noise_level + 0.1 * (kind == "noise")
        if kind in ("noise", "changed"):
            self.last_change_ms = timestamp_ms
            self.static_since_ms, self.static_kind = 0, ""
        elif not self.static_since_ms:
            self.static_since_ms = self._previous_ms
            self.static_kind = kind
            self.noisy_before_static = self.noise_level >= 0.5
        elif kind == "overlay":
            self.static_kind = "overlay"
        self._previous, self._previous_ms = sample, timestamp_ms
        return {
            "kind": kind, "static_since_ms": self.static_since_ms, "static_kind": self.static_kind,
            "noisy_before_static": self.noisy_before_static if self.static_since_ms else None,
            "noise_level": round(self.noise_level, 2), "last_change_ms": self.last_change_ms,
        }


class MotionGate:
    """Cheap frame differencing that lets the object detector idle on static
    scenes, which is what makes several CPU-only cameras practical."""

    def __init__(self, min_changed_fraction: float = 0.002, threshold: int = 25) -> None:
        self.min_changed_fraction = min_changed_fraction
        self.threshold = threshold
        self._previous = None

    def changed(self, image: Any) -> bool:
        import cv2

        small = cv2.GaussianBlur(cv2.cvtColor(cv2.resize(image, (160, 90)), cv2.COLOR_BGR2GRAY), (5, 5), 0)
        previous, self._previous = self._previous, small
        if previous is None:
            return True
        diff = cv2.absdiff(previous, small)
        _, mask = cv2.threshold(diff, self.threshold, 255, cv2.THRESH_BINARY)
        return cv2.countNonZero(mask) / mask.size >= self.min_changed_fraction


CLASS_COLORS = {"person": (60, 60, 255), "vehicle": (255, 160, 40), "animal": (80, 200, 80)}


GUARDIAN_COLORS = {"observed": (80, 200, 80), "approaching": (0, 200, 255), "elevated_concern": (0, 120, 255), "urgent_review": (40, 40, 230)}
GUARDIAN_NAMES = {"observed": "Observed", "approaching": "Approaching", "elevated_concern": "Elevated", "urgent_review": "Urgent review"}


def annotate(
    image: Any, tracks: list[Track], zones: list[dict[str, Any]], *, site_ids: dict[str, str] | None = None, guardian: list[dict[str, Any]] | None = None,
    identity: dict[str, str] | None = None,
) -> Any:
    import cv2

    canvas = image.copy()
    height, width = canvas.shape[:2]
    for zone in zones:
        points = [(int(x * width), int(y * height)) for x, y in zone["polygon"]]
        color = (0, 0, 220) if zone["type"] == "restricted" else (0, 200, 255)
        for index in range(len(points)):
            cv2.line(canvas, points[index], points[(index + 1) % len(points)], color, 1)
        cv2.putText(canvas, zone.get("name", zone["id"]), points[0], cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)
    for track in tracks:
        x, y, w, h = track.bbox
        top_left = (int(x * width), int(y * height))
        bottom_right = (int((x + w) * width), int((y + h) * height))
        color = CLASS_COLORS.get(track.label, (200, 200, 200))
        cv2.rectangle(canvas, top_left, bottom_right, color, 2)
        tag = (site_ids or {}).get(track.track_id, track.track_id)
        if (identity or {}).get(track.track_id):  # household-member recognition (live view only)
            tag += f" {identity[track.track_id]}"
        cv2.putText(canvas, f"{track.label} {tag}", (top_left[0], max(12, top_left[1] - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)
    for item in guardian or []:  # wildlife movement: trail, image heading, risk level
        color = GUARDIAN_COLORS.get(item["level"], (200, 200, 200))
        trail = [(int(x * width), int(y * height)) for x, y in item["trail"]]
        for a, b in zip(trail, trail[1:]):
            cv2.line(canvas, a, b, color, 2)
        x, y, w, h = item["bbox"]
        cv2.rectangle(canvas, (int(x * width), int(y * height)), (int((x + w) * width), int((y + h) * height)), color, 3)
        if trail and item["movement"] not in {"stationary", "unclear"}:
            vx, vy = item["velocity"]
            tip = (int(trail[-1][0] + vx * width), int(trail[-1][1] + vy * height))
            cv2.arrowedLine(canvas, trail[-1], tip, color, 2, tipLength=0.3)
        text = f"{item['label']} {item['movement']} {GUARDIAN_NAMES.get(item['level'], '')}"
        cv2.putText(canvas, text, (int(x * width), min(height - 4, int((y + h) * height) + 14)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
    return canvas


def encode_jpeg(image: Any, quality: int = 75) -> bytes:
    import cv2

    ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buffer.tobytes() if ok else b""
