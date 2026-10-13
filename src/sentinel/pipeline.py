from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from .config import sensitivity_adjustments
from .detectors import create_detector, detection_thresholds, resolve_detector_settings
from .engine import ZoneMap
from .environment import EnvironmentMonitor, EnvironmentSettings
from .models import Detection, canonical_class, detector_labels, effective_categories
from .recorder import ClipRecorder
from .runtime import SentinelRuntime
from .sources import open_source
from .tracker import IoUTracker, TrackerConfig
from .vision import MotionGate, annotate, appearance_vector, apply_privacy_masks, encode_jpeg

log = logging.getLogger("sentinel.pipeline")


def filter_detections(
    detections: list[Detection], camera_id: str, zones: ZoneMap, min_area: float, categories: set[str] | None = None
) -> list[Detection]:
    """Drop tiny boxes, boxes in ignore zones, and labels whose category is not
    enabled for detection (`categories` None = no category filter)."""
    kept = []
    for det in detections:
        _, _, w, h = det.bbox
        if w * h < min_area:
            continue
        if categories is not None and canonical_class(det.label) not in categories:
            continue
        if zones.is_ignored(camera_id, det.anchor):
            continue
        kept.append(det)
    return kept


def with_category_classes(settings: dict[str, Any], categories: dict[str, dict[str, bool]]) -> dict[str, Any]:
    """Restrict the detector's class allowlist to enabled categories (an
    explicit `detector.classes` list can only narrow it further)."""
    allowed = detector_labels(categories)
    if settings.get("classes"):
        allowed = [label for label in settings["classes"] if label in allowed]
    return settings | {"classes": allowed}


class CameraWorker(threading.Thread):
    """capture → privacy mask → motion gate → detect → track → temporal engine.
    One thread per camera; the shared runtime serialises engine access."""

    def __init__(
        self,
        camera: dict[str, Any],
        runtime: SentinelRuntime,
        on_frame: Callable[[dict[str, Any], Any], None] | None = None,
    ) -> None:
        super().__init__(name=f"camera-{camera['id']}", daemon=True)
        self.camera = camera
        self.runtime = runtime
        self.config = runtime.config
        self.on_frame = on_frame  # benchmark/diagnostics hook: (frame record, masked image)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        camera_id = self.camera["id"]
        categories = effective_categories(self.config, self.camera)
        detector_settings = with_category_classes(
            resolve_detector_settings(self.config.get("detector", {}) | self.camera.get("detector", {})), categories
        )
        _, threshold_offset = sensitivity_adjustments(self.runtime.settings["sensitivity"])
        score_threshold, high_score = detection_thresholds(detector_settings, threshold_offset)
        try:
            detector = create_detector(detector_settings, score_threshold)
            source = open_source(self.camera, self.config, self.runtime.secrets.get)
        except Exception as error:
            log.exception("camera %s failed to start", camera_id)
            self.runtime.update_camera_status(camera_id, connected=False, error=str(error))
            return

        tracker = IoUTracker(camera_id, TrackerConfig.from_dict((self.config.get("tracker") or {}) | {"high_score": high_score}))
        recorder = ClipRecorder(camera_id, self.runtime.media_root, self.config.get("recording", {}))
        recorder.enabled = recorder.enabled and self.config.get("privacy", {}).get("store_clips", True)
        self.runtime.recorders[camera_id] = recorder
        zones = ZoneMap(self.config)
        camera_zones = self.camera.get("zones", [])
        masks = self.camera.get("privacy_masks", [])
        motion = MotionGate(float(detector_settings.get("motion_min_fraction", 0.002)))
        idle_detect_ms = int(detector_settings.get("idle_detect_interval_s", 1.0) * 1000)
        min_area = float(detector_settings.get("min_box_area", 0.0004))
        enabled = {name for name, values in categories.items() if values.get("detect")}
        frame_interval = 1.0 / float(self.camera.get("fps", 5))
        last_detect_ms = last_clean_ms = 0
        frames, fps_window_start = 0, time.monotonic()
        active: list = []
        last_detections: list[Detection] = []
        environment_settings = self.config.get("environment", {})
        environment = EnvironmentMonitor(EnvironmentSettings.from_dict(environment_settings)) if environment_settings.get("enabled", True) else None

        log.info("camera %s running (%s source)", camera_id, self.camera["source"]["type"])
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                frame = source.read()
                if frame is None:
                    self.runtime.update_camera_status(camera_id, connected=source.connected, error=source.last_error, **source.status())
                    self._stop.wait(0.05)
                    continue
                image = apply_privacy_masks(frame.image, masks)
                now_ms = frame.timestamp_ms
                detections: list[Detection] = []
                timings = {"detect": 0.0, "track": 0.0, "engine": 0.0}
                detected = motion.changed(image) or bool(active) or now_ms - last_detect_ms >= idle_detect_ms
                if detected:
                    last_detect_ms = now_ms
                    stage = time.perf_counter()
                    detections = filter_detections(detector.detect(frame), camera_id, zones, min_area, enabled)
                    timings["detect"] = (time.perf_counter() - stage) * 1000
                    last_detections = detections
                scene = environment.update(image, now_ms, last_detections) if environment else None
                if scene is not None:
                    self.runtime.scene_update(camera_id, scene.conditions, now_ms)
                    if detected and "unknown_motion" in enabled:
                        detections = detections + filter_detections(scene.unknown, camera_id, zones, min_area, enabled)
                if detected:
                    stage = time.perf_counter()
                    active, ended = tracker.update(detections, now_ms)
                    for track in active:
                        if track.appearance is None or track.hits % 10 == 0:
                            track.appearance = appearance_vector(image, track.bbox)
                    timings["track"] = (time.perf_counter() - stage) * 1000
                    stage = time.perf_counter()
                    self.runtime.process_camera_update(camera_id, now_ms, active, ended, image=image)
                    timings["engine"] = (time.perf_counter() - stage) * 1000
                if self.on_frame is not None:
                    self.on_frame(
                        {
                            "camera_id": camera_id,
                            "capture_ms": now_ms,
                            "done_ms": int(time.time() * 1000),
                            "detected": detected,
                            "timings": timings,
                            "detections": detections,
                            "tracks": list(active),
                            "frames_read": getattr(source, "frames_read", None),
                        },
                        image,
                    )
                recorder.add_frame(image, now_ms)
                site_ids = dict(self.runtime.engine.correlator.track_to_site)
                self.runtime.latest_jpeg[camera_id] = encode_jpeg(annotate(image, active, camera_zones, site_ids=site_ids))
                if now_ms - last_clean_ms >= 1000:  # clean frame for the setup editor, refreshed once a second
                    last_clean_ms = now_ms
                    self.runtime.latest_frame[camera_id] = encode_jpeg(image, 85)

                frames += 1
                elapsed_window = time.monotonic() - fps_window_start
                fps = frames / elapsed_window if elapsed_window > 0 else 0.0
                if elapsed_window > 5:
                    frames, fps_window_start = 0, time.monotonic()
                self.runtime.update_camera_status(camera_id, connected=True, last_frame_ms=now_ms, fps=round(fps, 1), error="", **source.status())
                remaining = frame_interval - (time.monotonic() - started)
                if remaining > 0:
                    self._stop.wait(remaining)
        finally:
            recorder.finish()
            source.close()
            detector.close()
