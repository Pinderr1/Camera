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
            running_mode=vision.RunningMode.VIDEO,
            score_threshold=score_threshold,
            max_results=int(settings.get("max_results", 15)),
            category_allowlist=list(settings.get("classes", DEFAULT_CLASSES)),
        )
        self._detector = vision.ObjectDetector.create_from_options(options)
        self._last_ts = -1

    def detect(self, frame: Frame) -> list[Detection]:
        import cv2
        import mediapipe as mp

        timestamp = max(frame.timestamp_ms, self._last_ts + 1)  # VIDEO mode needs strictly increasing timestamps
        self._last_ts = timestamp
        height, width = frame.image.shape[:2]
        rgb = cv2.cvtColor(frame.image, cv2.COLOR_BGR2RGB)
        result = self._detector.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), timestamp)
        detections = []
        for item in result.detections:
            category = item.categories[0]
            box = item.bounding_box
            detections.append(
                Detection(
                    label=category.category_name or "",
                    confidence=float(category.score),
                    bbox=(box.origin_x / width, box.origin_y / height, box.width / width, box.height / height),
                )
            )
        return detections

    def close(self) -> None:
        self._detector.close()


def create_detector(settings: dict[str, Any], score_threshold: float) -> Detector:
    kind = settings.get("type", "mediapipe")
    if kind == "scripted":
        return ScriptedDetector()
    if kind == "mediapipe":
        return MediaPipeObjectDetector(settings, score_threshold)
    raise ValueError(f"unknown detector type {kind!r}")
