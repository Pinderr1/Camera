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


class FreezeDetector:
    """A live camera's picture is never bit-for-bit still: sensor noise and
    periodic key frames keep changing it. Pixels are sampled (not averaged, so
    noise survives); when the samples stay identical, the feed is frozen, e.g. a
    decoder or recorder repeating its last image. Returns the timestamp since
    which the picture has not changed, or 0."""

    def __init__(self) -> None:
        self._previous = None
        self._previous_ms = 0
        self.unchanged_since_ms = 0

    def update(self, image: Any, timestamp_ms: int) -> int:
        height, width = image.shape[:2]
        sample = image[:: max(1, height // 90), :: max(1, width // 160)].copy()
        if self._previous is not None and sample.shape == self._previous.shape and not (sample != self._previous).any():
            self.unchanged_since_ms = self.unchanged_since_ms or self._previous_ms
        else:
            self.unchanged_since_ms = 0
        self._previous, self._previous_ms = sample, timestamp_ms
        return self.unchanged_since_ms


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


def annotate(image: Any, tracks: list[Track], zones: list[dict[str, Any]], *, site_ids: dict[str, str] | None = None) -> Any:
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
        cv2.putText(canvas, f"{track.label} {tag}", (top_left[0], max(12, top_left[1] - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)
    return canvas


def encode_jpeg(image: Any, quality: int = 75) -> bytes:
    import cv2

    ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buffer.tobytes() if ok else b""
