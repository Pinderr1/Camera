from __future__ import annotations

import logging
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from .models import Detection
from .vision import Frame

log = logging.getLogger("sentinel.detectors")

DEFAULT_CLASSES = ["person", "car", "truck", "bus", "motorcycle", "bicycle", "dog", "cat"]
MODEL_URLS = {
    "efficientdet_lite0": "https://storage.googleapis.com/mediapipe-models/object_detector/efficientdet_lite0/float16/latest/efficientdet_lite0.tflite",
    "efficientdet_lite2": "https://storage.googleapis.com/mediapipe-models/object_detector/efficientdet_lite2/float16/latest/efficientdet_lite2.tflite",
}


class Detector(Protocol):
    def detect(self, frame: Frame) -> list[Detection]: ...

    def close(self) -> None: ...


class ScriptedDetector:
    """Returns the ground truth carried by simulated frames."""

    def detect(self, frame: Frame) -> list[Detection]:
        return list(frame.truth or [])

    def close(self) -> None:
        pass


class MediaPipeObjectDetector:
    """EfficientDet-Lite via MediaPipe Tasks (Apache-2.0 runtime and model), CPU
    by default. Chosen over Ultralytics YOLO, whose AGPL license conflicts with a
    closed commercial product; any detector can replace it behind detect()."""

    def __init__(self, settings: dict[str, Any], score_threshold: float) -> None:
        from mediapipe.tasks.python import vision
        from mediapipe.tasks.python.core.base_options import BaseOptions

        model = settings.get("model", "efficientdet_lite0")
        model_path = Path(settings.get("model_path", f"models/{model}.tflite"))
        if not model_path.exists():
            model_path.parent.mkdir(parents=True, exist_ok=True)
            log.info("downloading %s to %s", model, model_path)
            urllib.request.urlretrieve(settings.get("model_url", MODEL_URLS[model]), model_path)
        delegate = BaseOptions.Delegate.GPU if settings.get("delegate") == "gpu" else BaseOptions.Delegate.CPU
        options = vision.ObjectDetectorOptions(
            base_options=BaseOptions(model_asset_path=str(model_path), delegate=delegate),
            running_mode=vision.RunningMode.IMAGE,
            score_threshold=score_threshold,
            max_results=int(settings.get("max_results", 15)),
            category_allowlist=list(settings.get("classes", DEFAULT_CLASSES)),
        )
        self._detector = vision.ObjectDetector.create_from_options(options)
        # Optional [cols, rows] tiling: the model sees a 320-448 px input, so on
        # 1080p cameras distant people shrink to a few pixels. Tiles trade CPU
        # (one extra inference per tile) for small-object recall.
        self.tiles = tuple(settings.get("tiles") or (1, 1))
        self.tile_overlap = float(settings.get("tile_overlap", 0.15))

    def detect(self, frame: Frame) -> list[Detection]:
        import cv2

        rgb = cv2.cvtColor(frame.image, cv2.COLOR_BGR2RGB)
        return detect_with_tiles(rgb, self._detect_region, self.tiles, self.tile_overlap)

    def _detect_region(self, rgb, region: tuple[float, float, float, float]) -> list[Detection]:
        import mediapipe as mp

        height, width = rgb.shape[:2]
        crop, x0, y0 = crop_region(rgb, region)
        result = self._detector.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=crop))
        out = []
        for item in result.detections:
            category = item.categories[0]
            box = item.bounding_box
            out.append(
                Detection(
                    label=category.category_name or "",
                    confidence=float(category.score),
                    bbox=((x0 + box.origin_x) / width, (y0 + box.origin_y) / height, box.width / width, box.height / height),
                )
            )
        return out

    def close(self) -> None:
        self._detector.close()


def detect_with_tiles(image, detect_region, tiles, overlap: float) -> list[Detection]:
    """Full-frame pass plus optional overlapping [cols, rows] tile passes.
    `detect_region(image, (x, y, w, h))` returns full-frame normalized boxes."""
    detections = detect_region(image, (0.0, 0.0, 1.0, 1.0))
    cols, rows = tiles
    if cols * rows == 1:
        return detections
    cut: set[int] = set()  # indexes of tile detections clipped by an inner tile edge
    tile_w = min(1.0, (1 + overlap * (cols - 1)) / cols)
    tile_h = min(1.0, (1 + overlap * (rows - 1)) / rows)
    for col in range(cols):
        for row in range(rows):
            x0 = 0.0 if cols == 1 else col * (1 - tile_w) / (cols - 1)
            y0 = 0.0 if rows == 1 else row * (1 - tile_h) / (rows - 1)
            for det in detect_region(image, (x0, y0, tile_w, tile_h)):
                if _touches_inner_edge(det.bbox, (x0, y0, tile_w, tile_h)):
                    cut.add(len(detections))
                detections.append(det)
    return non_max_suppression(detections, cut=cut)


def crop_region(image, region: tuple[float, float, float, float]):
    """Returns (crop, x0_px, y0_px) for a normalized region."""
    import numpy as np

    height, width = image.shape[:2]
    rx, ry, rw, rh = region
    x0, y0 = int(rx * width), int(ry * height)
    return np.ascontiguousarray(image[y0 : y0 + int(rh * height), x0 : x0 + int(rw * width)]), x0, y0


def non_max_suppression(
    detections: list[Detection], iou_threshold: float = 0.5, cut: set[int] | None = None, containment: float = 0.6
) -> list[Detection]:
    """Per-label NMS across the full-frame and tile passes. Detections clipped by
    an inner tile edge (`cut`) are partial bodies and are also dropped when they
    lie mostly inside another box. Uncut boxes are never dropped for being
    contained: on real footage the detector often emits one box around two
    people walking together, which must not swallow the individual boxes."""
    from .tracker import iou

    cut = cut or set()
    order = sorted(range(len(detections)), key=lambda index: detections[index].confidence, reverse=True)
    kept: list[int] = []
    for index in order:
        det = detections[index]
        if any(detections[k].label == det.label and iou(det.bbox, detections[k].bbox) >= iou_threshold for k in kept):
            continue
        kept.append(index)
    survivors = []
    for index in kept:
        det = detections[index]
        if index in cut and any(
            other != index and detections[other].label == det.label and _contained(det.bbox, detections[other].bbox) >= containment
            for other in kept
        ):
            continue
        survivors.append(det)
    return survivors


def _contained(inner, outer) -> float:
    x0, y0 = max(inner[0], outer[0]), max(inner[1], outer[1])
    x1, y1 = min(inner[0] + inner[2], outer[0] + outer[2]), min(inner[1] + inner[3], outer[1] + outer[3])
    area = inner[2] * inner[3]
    return max(0.0, x1 - x0) * max(0.0, y1 - y0) / area if area > 0 else 0.0


def _touches_inner_edge(bbox, tile, margin: float = 0.01) -> bool:
    x, y, w, h = bbox
    tx, ty, tw, th = tile
    return (
        (tx > 0 and x <= tx + margin)
        or (ty > 0 and y <= ty + margin)
        or (tx + tw < 1 and x + w >= tx + tw - margin)
        or (ty + th < 1 and y + h >= ty + th - margin)
    )


# Measured defaults (see ROADMAP "Detector upgrade" findings): YOLOX-s at 960x544
# on any GPU ONNX Runtime can use; YOLOX-tiny at 416 on CPU-only hosts, which costs
# about the same as EfficientDet-Lite0 but more than doubles person recall.
DEFAULT_PROFILES = {
    "gpu": {"model": "yolox_s", "input_size": [960, 544], "score_threshold": 0.5},
    "cpu": {"model": "yolox_tiny", "input_size": [416, 416], "score_threshold": 0.45},
}


def resolve_detector_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Expand `"model": "auto"` into a concrete profile for this machine, and fall
    back to EfficientDet-Lite0 (MediaPipe) when ONNX Runtime is not installed."""
    if settings.get("type") != "onnx":
        return settings
    try:
        from .detectors_onnx import gpu_available
    except ImportError:
        log.warning("onnxruntime is not installed; falling back to EfficientDet-Lite0")
        return {key: value for key, value in settings.items() if key not in {"model", "input_size", "providers"}} | {
            "type": "mediapipe", "model": "efficientdet_lite0", "score_threshold": 0.45,
        }
    if settings.get("model", "auto") != "auto":
        return settings
    profiles = DEFAULT_PROFILES | settings.get("profiles", {})
    profile = profiles["gpu" if gpu_available(settings.get("providers", "auto")) else "cpu"]
    resolved = {key: value for key, value in settings.items() if key != "profiles"} | profile
    log.info("detector profile: %s", {key: resolved.get(key) for key in ("model", "input_size", "score_threshold")})
    return resolved


def detection_thresholds(settings: dict[str, Any], offset: float = 0.0) -> tuple[float, float]:
    """(threshold handed to the detector, tracker high_score).

    `score_threshold` is the confidence at which a box counts as a reported
    detection. With `low_score_threshold` set, the detector also returns weaker
    boxes, which the tracker may only use to extend existing tracks."""
    report = float(settings.get("score_threshold", 0.45)) + offset
    low = settings.get("low_score_threshold")
    if low is None:
        return report, 0.0
    return min(float(low), report), report


def create_detector(settings: dict[str, Any], score_threshold: float) -> Detector:
    kind = settings.get("type", "mediapipe")
    if kind == "scripted":
        return ScriptedDetector()
    if kind == "mediapipe":
        return MediaPipeObjectDetector(settings, score_threshold)
    if kind == "onnx":
        from .detectors_onnx import OnnxDetector

        return OnnxDetector(settings, score_threshold)
    raise ValueError(f"unknown detector type {kind!r}")
