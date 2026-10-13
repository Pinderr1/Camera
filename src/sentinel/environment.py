"""Scene-level motion analysis: environmental effects and unidentified movers.

The object detector only knows COCO classes. Most of what moves in front of a
security camera is not an object at all: light changes, IR switching, rain and
snow, shadows, swaying vegetation, insects on the lens. This module classifies
that motion so it can be shown and filtered, and never mistaken for an intruder.

It also proposes `unknown_motion` boxes: coherent blobs that move consistently,
are not explained by any detection, and are outside areas with habitual motion.
They only reach the tracker when the `unknown_motion` category is enabled, and
by default that category cannot alert.

Heuristic and lightweight (MOG2 on a 320x180 copy, ~1-2 ms per frame). The
thresholds have not been validated on labeled real-world footage."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .models import Detection

WIDTH, HEIGHT = 320, 180


@dataclass
class EnvironmentSettings:
    lighting_delta: float = 18.0  # mean grey-level jump between frames
    lighting_fraction: float = 0.45  # share of the frame changing at once
    ir_saturation: float = 6.0  # mean HSV saturation below this = monochrome (IR) image
    precipitation_blobs: int = 80  # many tiny blobs (edge flicker gives ~20-30)...
    precipitation_max_fraction: float = 0.15  # ...but little total area
    shadow_fraction: float = 0.04
    # A pixel that changes in >8% of frames over ~20 s is habitual motion (branches,
    # flags, water); a person walking through moves each pixel only a few frames (~0.04).
    recurring_heat: float = 0.08
    recurring_fraction: float = 0.002
    near_camera_fraction: float = 0.25  # one blob covering a quarter of the view
    unknown_min_area: float = 0.002
    unknown_max_area: float = 0.15
    unknown_min_frames: int = 8
    unknown_min_travel: float = 0.05  # in frame widths

    @classmethod
    def from_dict(cls, data: dict | None) -> "EnvironmentSettings":
        data = data or {}
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


@dataclass
class EnvironmentReport:
    conditions: dict[str, bool]
    metrics: dict[str, float]
    unknown: list[Detection] = field(default_factory=list)


class EnvironmentMonitor:
    def __init__(self, settings: EnvironmentSettings | None = None) -> None:
        import cv2

        self.settings = settings or EnvironmentSettings()
        self._subtractor = cv2.createBackgroundSubtractorMOG2(history=150, varThreshold=25, detectShadows=True)
        self._kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self._heat = None
        self._previous_gray = None
        self._previous_mean: float | None = None
        self._blobs: list[dict[str, Any]] = []
        self._frames = 0

    def update(self, image, timestamp_ms: int, detections: list[Detection] | None = None) -> EnvironmentReport:
        import cv2
        import numpy as np

        s = self.settings
        small = cv2.resize(image, (WIDTH, HEIGHT), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        saturation = float(cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[:, :, 1].mean())
        mask = self._subtractor.apply(small)
        foreground = cv2.morphologyEx((mask == 255).astype(np.uint8), cv2.MORPH_OPEN, self._kernel)
        shadow_fraction = float((mask == 127).mean())
        changed = float(foreground.mean())
        mean = float(gray.mean())
        delta = 0.0 if self._previous_mean is None else mean - self._previous_mean
        self._previous_mean = mean
        self._frames += 1

        # Habitual-motion map from plain frame differencing: MOG2 deliberately learns
        # periodic motion (branches, flags) into its background, which hides it.
        heat_alpha = 0.01
        if self._heat is None:
            self._heat = np.zeros(foreground.shape, np.float32)
        if self._previous_gray is not None:
            moving = (cv2.absdiff(gray, self._previous_gray) > 20).astype(np.float32)
            self._heat = (1 - heat_alpha) * self._heat + heat_alpha * moving
        self._previous_gray = gray

        count, _, stats, centroids = cv2.connectedComponentsWithStats(foreground, connectivity=8)
        areas = stats[1:, cv2.CC_STAT_AREA] / float(WIDTH * HEIGHT) if count > 1 else np.zeros(0)
        # Rain and snow streaks are 1-2 px wide: count them on the raw mask, before the opening erases them.
        raw_count, _, raw_stats, _ = cv2.connectedComponentsWithStats((mask >= 127).astype(np.uint8), connectivity=8)
        tiny = int((raw_stats[1:, cv2.CC_STAT_AREA] < 0.0008 * WIDTH * HEIGHT).sum()) if raw_count > 1 else 0
        warming_up = self._frames < 30  # the background model needs a moment after start or reconnect

        lighting = not warming_up and (abs(delta) > s.lighting_delta or changed > s.lighting_fraction)
        precipitation = not warming_up and not lighting and tiny >= s.precipitation_blobs and changed < s.precipitation_max_fraction
        conditions = {
            "lighting_change": lighting,
            "ir_mode": saturation < s.ir_saturation,
            "precipitation_or_noise": precipitation,
            "moving_shadows": not (warming_up or lighting or precipitation) and shadow_fraction > s.shadow_fraction and shadow_fraction > changed,
            "recurring_motion": float((self._heat > s.recurring_heat).mean()) > s.recurring_fraction,
            "near_camera_object": not (warming_up or lighting) and bool(len(areas)) and float(areas.max()) > s.near_camera_fraction,
        }
        metrics = {
            "changed_fraction": round(changed, 4), "brightness": round(mean, 1), "brightness_delta": round(delta, 1),
            "saturation": round(saturation, 1), "tiny_blobs": tiny, "shadow_fraction": round(shadow_fraction, 4),
        }
        unknown: list[Detection] = []
        noisy = warming_up or conditions["lighting_change"] or conditions["precipitation_or_noise"] or conditions["near_camera_object"]
        if not noisy:
            unknown = self._unknown_movers(stats[1:], centroids[1:], areas, detections or [], timestamp_ms)
        else:
            self._blobs.clear()
        return EnvironmentReport(conditions, metrics, unknown)

    def _unknown_movers(self, stats, centroids, areas, detections: list[Detection], now: int) -> list[Detection]:
        s = self.settings
        candidates = []
        for (x, y, w, h, _), (cx, cy), area in zip(stats, centroids, areas):
            if not s.unknown_min_area <= area <= s.unknown_max_area:
                continue
            box = (x / WIDTH, y / HEIGHT, w / WIDTH, h / HEIGHT)
            centre = (cx / WIDTH, cy / HEIGHT)
            if any(_inside(centre, det.bbox) or _overlap(box, det.bbox) > 0.3 for det in detections):
                continue  # explained by a detected object
            heat = float(self._heat[int(y): int(y + h), int(x): int(x + w)].mean())
            if heat > s.recurring_heat:
                continue  # habitual motion (vegetation, flags, water)
            candidates.append((box, centre))
        updated = []
        for box, centre in candidates:
            match = min(
                (blob for blob in self._blobs if math.dist(blob["centre"], centre) < 0.08),
                key=lambda blob: math.dist(blob["centre"], centre),
                default=None,
            )
            if match is not None and match not in updated:
                match.update(box=box, centre=centre, hits=match["hits"] + 1, last=now)
                updated.append(match)
            else:
                updated.append({"box": box, "centre": centre, "start": centre, "hits": 1, "last": now})
        self._blobs = updated
        return [
            Detection("unknown_motion", 0.3, blob["box"], "unknown")
            for blob in self._blobs
            if blob["hits"] >= s.unknown_min_frames and math.dist(blob["start"], blob["centre"]) >= s.unknown_min_travel
        ]


def _inside(point: tuple[float, float], box) -> bool:
    x, y, w, h = box
    return x <= point[0] <= x + w and y <= point[1] <= y + h


def _overlap(a, b) -> float:
    """Intersection over the smaller box."""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    smaller = min(a[2] * a[3], b[2] * b[3])
    return inter / smaller if smaller > 0 else 0.0
