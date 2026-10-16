from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable

from .config import sensitivity_adjustments
from .detectors import create_detector, detection_thresholds, resolve_detector_settings
from .engine import ZoneMap
from .environment import EnvironmentMonitor, EnvironmentSettings
from .models import Detection, canonical_class, detector_labels, effective_categories
from .recorder import ClipRecorder
from .runtime import SentinelRuntime
from .sources import open_source
from .streams import scrub
from .tracker import IoUTracker, TrackerConfig
from .vision import ContentMonitor, MotionGate, annotate, appearance_vector, apply_privacy_masks, encode_jpeg

log = logging.getLogger("sentinel.pipeline")

_runs: dict[str, int] = {}  # camera id -> worker runs started in this process (unique track ids per run)
_runs_lock = threading.Lock()


def _next_run(camera_id: str) -> int:
    with _runs_lock:
        _runs[camera_id] = _runs.get(camera_id, 0) + 1
        return _runs[camera_id]


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


def _error_text(error: BaseException) -> str:
    return scrub(f"{type(error).__name__}: {error}")[:300]


def _percentile(values: deque[float], percent: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(len(ordered) * percent / 100))], 1)


class CameraWorker(threading.Thread):
    """capture → privacy mask → motion gate → detect → track → temporal engine.
    One thread per camera; the shared runtime serialises engine access.

    Supervision contract (see `supervisor.py`): `heartbeat` advances every loop
    iteration, `phase` turns "running" once the detector and source are up, and
    `failure` holds the reason when the thread ends on an error. A stopped
    worker never publishes again, so an abandoned (stalled) one that wakes up
    late cannot overwrite the state of its replacement."""

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
        self.failure: str | None = None
        self.started_at = self.heartbeat = time.monotonic()
        self.phase = "starting"

    def stop(self) -> None:
        self._stop.set()

    def start(self) -> None:
        self.started_at = self.heartbeat = time.monotonic()
        super().start()

    def run(self) -> None:
        try:
            self._run()
        except Exception as error:  # reported to the supervisor, which restarts with backoff
            self.failure = _error_text(error)
            log.exception("camera %s processing crashed", self.camera["id"])

    def _report(self, **fields: Any) -> None:
        if not self._stop.is_set():
            self.runtime.update_camera_status(self.camera["id"], **fields)

    def _create_detector(self, categories: dict[str, dict[str, bool]], *, cpu_only: bool = False) -> tuple[Any, str]:
        raw = self.config.get("detector", {}) | self.camera.get("detector", {})
        if cpu_only:
            raw = raw | {"providers": ["cpu"]}
        settings = with_category_classes(resolve_detector_settings(raw), categories)
        _, threshold_offset = sensitivity_adjustments(self.runtime.settings["sensitivity"])
        score_threshold, _ = detection_thresholds(settings, threshold_offset)
        detector = create_detector(settings, score_threshold)
        providers = getattr(detector, "providers", None) or ["CPUExecutionProvider"]
        return detector, "gpu" if providers[0] != "CPUExecutionProvider" else "cpu"

    def _run(self) -> None:
        camera_id = self.camera["id"]
        categories = effective_categories(self.config, self.camera)
        detector_settings = with_category_classes(
            resolve_detector_settings(self.config.get("detector", {}) | self.camera.get("detector", {})), categories
        )
        _, threshold_offset = sensitivity_adjustments(self.runtime.settings["sensitivity"])
        _, high_score = detection_thresholds(detector_settings, threshold_offset)
        cpu_fallback = bool(detector_settings.get("cpu_fallback", True))
        failures_before_fallback = int(detector_settings.get("failures_before_fallback", 3))
        retry_ms = int(float(detector_settings.get("retry_s", 30)) * 1000)
        try:
            detector, provider = self._create_detector(categories)
            detector_state, detector_error = "ok", ""
        except Exception as error:
            if not cpu_fallback:
                raise
            log.warning("camera %s: detector failed to load (%s); trying CPU", camera_id, _error_text(error))
            detector, provider = self._create_detector(categories, cpu_only=True)
            detector_state, detector_error = "fallback", _error_text(error)
        on_fallback = detector_state == "fallback"  # running on CPU because the configured detector failed
        try:
            source = open_source(self.camera, self.config, self.runtime.secrets.get)
        except Exception:
            detector.close()
            raise

        tracker = IoUTracker(camera_id, TrackerConfig.from_dict((self.config.get("tracker") or {}) | {"high_score": high_score}), run=_next_run(camera_id))
        recorder = ClipRecorder(camera_id, self.runtime.media_root, self.config.get("recording", {}))
        recorder.enabled = recorder.enabled and self.config.get("privacy", {}).get("store_clips", True)
        recorder.on_finalized = self.runtime.record_evidence
        if not self._stop.is_set():
            self.runtime.recorders[camera_id] = recorder
        zones = ZoneMap(self.config)
        camera_zones = self.camera.get("zones", [])
        masks = self.camera.get("privacy_masks", [])
        motion = MotionGate(float(detector_settings.get("motion_min_fraction", 0.002)))
        content = ContentMonitor()
        last_pts, pts_stalled_since = None, 0
        last_inference_ms = 0
        decoded_window = (time.monotonic(), getattr(source, 'frames_read', 0) or 0)
        decoded_fps = 0.0
        idle_detect_ms = int(detector_settings.get("idle_detect_interval_s", 1.0) * 1000)
        min_area = float(detector_settings.get("min_box_area", 0.0004))
        enabled = {name for name, values in categories.items() if values.get("detect")}
        target_fps = float(self.camera.get("fps", 5))
        frame_interval = 1.0 / target_fps
        last_detect_ms = last_clean_ms = 0
        frames, fps_window_start, fps = 0, time.monotonic(), 0.0
        processed = consecutive_errors = errors_total = 0
        detector_retry_at = 0
        probing = False
        detect_ms: deque[float] = deque(maxlen=100)
        latency_ms: deque[float] = deque(maxlen=100)
        active: list = []
        last_detections: list[Detection] = []
        environment_settings = self.config.get("environment", {})
        environment = EnvironmentMonitor(EnvironmentSettings.from_dict(environment_settings)) if environment_settings.get("enabled", True) else None

        def detector_fields() -> dict[str, Any]:
            return {"detector_state": detector_state, "detector_error": detector_error, "detector_errors": errors_total, "detector_provider": provider}

        self._report(target_fps=target_fps, **detector_fields())
        self.phase = "running"
        log.info("camera %s running (%s source, detector on %s)", camera_id, self.camera["source"]["type"], provider)
        try:
            while not self._stop.is_set():
                started = self.heartbeat = time.monotonic()
                frame = source.read()
                if frame is None:
                    self._report(connected=source.connected, error=source.last_error, **source.status())
                    self._stop.wait(0.05)
                    continue
                image = apply_privacy_masks(frame.image, masks)
                now_ms = frame.timestamp_ms
                detections: list[Detection] = []
                timings = {"detect": 0.0, "track": 0.0, "engine": 0.0}
                if detector_state == "failed" and not probing and now_ms >= detector_retry_at:
                    try:
                        detector, provider = self._create_detector(categories, cpu_only=on_fallback)
                        probing = True  # stays "failed" until an inference on a real frame succeeds
                    except Exception as error:
                        detector_error, detector_retry_at = _error_text(error), now_ms + retry_ms
                detected = probing or (detector_state != "failed" and (motion.changed(image) or bool(active) or now_ms - last_detect_ms >= idle_detect_ms))
                if detected:
                    last_detect_ms = now_ms
                    stage = time.perf_counter()
                    try:
                        detections = filter_detections(detector.detect(frame), camera_id, zones, min_area, enabled)
                        consecutive_errors = 0
                        detect_ms.append((time.perf_counter() - stage) * 1000)
                        last_inference_ms = int(time.time() * 1000)
                        if probing:
                            detector_state, probing = ("fallback" if on_fallback else "ok"), False
                            log.info("camera %s: detector working again on %s", camera_id, provider)
                    except Exception as error:
                        consecutive_errors += 1
                        errors_total += 1
                        detector_error = _error_text(error)
                        detected = False
                        log.warning("camera %s: inference failed (%d in a row): %s", camera_id, consecutive_errors, detector_error)
                        if probing:
                            probing, detector_retry_at = False, now_ms + retry_ms
                            detector.close()
                        elif consecutive_errors >= failures_before_fallback:
                            detector, provider, detector_state = self._detector_failed(detector, provider, categories, cpu_fallback)
                            detector_retry_at, consecutive_errors = now_ms + retry_ms, 0
                            on_fallback = on_fallback or detector_state == "fallback"
                    timings["detect"] = (time.perf_counter() - stage) * 1000
                    if detected:
                        last_detections = detections
                if self._stop.is_set():
                    break
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
                    stage = time.perf_counter()
                    self.runtime.guardian_species_frame(camera_id, now_ms, image)  # copies due boxes only; never waits on the model
                    timings["species"] = (time.perf_counter() - stage) * 1000
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
                try:
                    recorder.add_frame(image, now_ms)
                except Exception as error:  # e.g. disk full: recording stops, monitoring continues
                    self.runtime.recording_failed(camera_id, error)
                site_ids = dict(self.runtime.engine.correlator.track_to_site)
                self.runtime.latest_jpeg[camera_id] = encode_jpeg(annotate(image, active, camera_zones, site_ids=site_ids, guardian=self.runtime.guardian_overlays(camera_id)))
                if now_ms - last_clean_ms >= 1000:  # clean frame for the setup editor, refreshed once a second
                    last_clean_ms = now_ms
                    self.runtime.latest_frame[camera_id] = encode_jpeg(image, 85)

                processed += 1
                frames += 1
                latency_ms.append(max(0.0, time.time() * 1000 - now_ms))
                elapsed_window = time.monotonic() - fps_window_start
                if elapsed_window > 5:
                    fps, frames, fps_window_start = frames / elapsed_window, 0, time.monotonic()
                elif elapsed_window > 1 and not fps:
                    fps = frames / elapsed_window
                frames_read = getattr(source, "frames_read", None)
                if frames_read is not None and time.monotonic() - decoded_window[0] >= 5:
                    decoded_fps = (frames_read - decoded_window[1]) / (time.monotonic() - decoded_window[0])
                    decoded_window = (time.monotonic(), frames_read)
                # Stream timestamps (PTS) stop advancing when a decoder or recorder repeats one frame.
                pts = frame.meta.get("pts_ms")
                if pts is None or last_pts is None or pts != last_pts:
                    pts_stalled_since = 0
                elif not pts_stalled_since:
                    pts_stalled_since = now_ms
                last_pts = pts
                self._report(
                    connected=True, last_frame_ms=now_ms, fps=round(fps, 1), error="", content=content.update(image, now_ms),
                    pts_ms=pts, pts_stalled_since_ms=pts_stalled_since, last_inference_ms=last_inference_ms,
                    decoded_fps=round(decoded_fps, 1) if frames_read is not None else None, frames_decoded=frames_read,
                    metrics={
                        "frames_processed": processed,
                        # The reader keeps only the newest frame, so frames decoded while the
                        # worker was busy are skipped by design (bounded latency).
                        "frames_skipped": max(0, frames_read - processed) if frames_read is not None else None,
                        "detect_ms_p50": _percentile(detect_ms, 50), "detect_ms_p95": _percentile(detect_ms, 95),
                        "latency_ms_p50": _percentile(latency_ms, 50), "latency_ms_p95": _percentile(latency_ms, 95),
                    },
                    **detector_fields(), **source.status(),
                )
                remaining = frame_interval - (time.monotonic() - started)
                if remaining > 0:
                    self._stop.wait(remaining)
        finally:
            # End this run's tracks so the engine, correlator and Guardian close them
            # instead of holding them open forever after a restart.
            ended = tracker.flush()
            if ended:
                try:
                    self.runtime.process_camera_update(camera_id, last_detect_ms or int(time.time() * 1000), [], ended)
                except Exception:
                    log.exception("camera %s: ending tracks on stop failed", camera_id)
            recorder.finish()
            source.close()
            detector.close()

    def _detector_failed(self, detector: Any, provider: str, categories: dict[str, dict[str, bool]], cpu_fallback: bool) -> tuple[Any, str, str]:
        """Repeated inference errors: move a GPU detector to CPU, otherwise mark
        AI processing as failed (the camera is then reported as not monitoring
        and the detector is reloaded every `retry_s`)."""
        camera_id = self.camera["id"]
        try:
            detector.close()
        except Exception:
            pass
        if provider == "gpu" and cpu_fallback:
            try:
                fallback, fallback_provider = self._create_detector(categories, cpu_only=True)
                log.error("camera %s: GPU inference keeps failing; continuing on CPU", camera_id)
                return fallback, fallback_provider, "fallback"
            except Exception as error:
                log.error("camera %s: CPU fallback failed too: %s", camera_id, _error_text(error))
        log.error("camera %s: AI processing failed; the detector will be reloaded", camera_id)
        return detector, provider, "failed"
