"""Drone video on the Hub: ingest, detect, track, and keep evidence.

The worker runs only while an approved inspection is in progress (privacy: the
drone camera is not analysed or recorded at any other time). It reads the
drone's stream (RTSP from the companion, a recorded file, or the simulated
aerial camera), runs the site's detector, follows detections across frames
with `InspectionObserver`, saves a fingerprinted snapshot when an object is
first confirmed, and hands a summary to the drone manager, which attaches it
to the inspection and therefore to the incident.

Drone frames never reach the temporal engine or zone rules.

This module is an I/O edge (OpenCV, numpy); the logic lives in
`observation.py`."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..models import Detection
from ..vision import Frame
from .base import map_point_to_geo
from .observation import CameraModel, InspectionObserver, ground_to_image, pose_from_telemetry

log = logging.getLogger("sentinel.drone.video")
SIM_WIDTH, SIM_HEIGHT = 640, 360
OBJECT_SIZES = {"person": (0.6, 0.5, 1.7), "vehicle": (1.9, 4.5, 1.5), "animal": (0.4, 0.9, 0.6)}  # width, length, height (m)


class SimulatedAerialSource:
    """Renders what a camera on the drone would see of scripted ground objects,
    using the drone's live telemetry (SITL or the built-in simulator) as the
    camera pose. Ground-truth boxes ride along in `Frame.truth` for the
    scripted detector, as for simulated fixed cameras.

    `objects`: [{"label", "path": [{"t": seconds, "map": [x, y]}, ...]}] in site
    map metres; the path loops."""

    def __init__(self, objects: list[dict[str, Any]], origin: tuple[float, float], camera: CameraModel,
                 telemetry: Callable[[], Any], fps: float = 5.0) -> None:
        self.objects = objects
        self.origin = origin
        self.camera = camera
        self.telemetry = telemetry
        self.period = 1.0 / max(0.5, fps)
        self.started = time.monotonic()
        self.connected = True
        self.last_error = ""
        self._last = 0.0

    def _position(self, path: list[dict[str, Any]], t: float) -> tuple[float, float]:
        if len(path) == 1:
            return tuple(path[0]["map"])
        span = path[-1]["t"] or 1.0
        t %= span
        for a, b in zip(path, path[1:]):
            if a["t"] <= t <= b["t"]:
                f = (t - a["t"]) / max(1e-6, b["t"] - a["t"])
                return (a["map"][0] + f * (b["map"][0] - a["map"][0]), a["map"][1] + f * (b["map"][1] - a["map"][1]))
        return tuple(path[-1]["map"])

    def read(self) -> Frame | None:
        import cv2
        import numpy as np

        wait = self._last + self.period - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        now_ms = int(time.time() * 1000)
        telemetry = self.telemetry()
        pose = pose_from_telemetry(telemetry, self.camera, now_ms)
        image = np.full((SIM_HEIGHT, SIM_WIDTH, 3), (60, 92, 70), dtype=np.uint8)
        truth = []
        if pose is not None and pose.height_m > 1.0:
            elapsed = time.monotonic() - self.started
            for item in self.objects:
                x, y = self._position(item["path"], elapsed)
                box = self._project_box(pose, item.get("label", "person"), x, y)
                if box is None:
                    continue
                u0, v0, u1, v1 = box
                truth.append(Detection(item.get("label", "person"), 0.88, (u0, v0, u1 - u0, v1 - v0), item.get("kind", "")))
                color = (90, 90, 230) if item.get("label") == "person" else (230, 170, 60)
                cv2.rectangle(image, (int(u0 * SIM_WIDTH), int(v0 * SIM_HEIGHT)), (int(u1 * SIM_WIDTH), int(v1 * SIM_HEIGHT)), color, -1)
        label = "SIM DRONE CAMERA" + (f"  alt {telemetry.alt_m:.0f} m" if pose else "  no position")
        cv2.putText(image, label, (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (235, 235, 235), 1)
        return Frame(image=image, timestamp_ms=now_ms, truth=truth, meta={"pose_ms": telemetry.telemetry_ms})

    def _project_box(self, pose: Any, label: str, x: float, y: float) -> tuple[float, float, float, float] | None:
        width, length, height = OBJECT_SIZES.get(label, (0.6, 0.6, 1.0))
        corners = []
        for dx in (-width / 2, width / 2):
            for dy in (-length / 2, length / 2):
                c_lat, c_lon = map_point_to_geo(self.origin, x + dx, y + dy)
                for up in (0.0, height):
                    point = ground_to_image(pose, self.camera, c_lat, c_lon, up)
                    if point is None:
                        return None
                    corners.append(point)
        us, vs = [c[0] for c in corners], [c[1] for c in corners]
        u0, v0, u1, v1 = max(0.0, min(us)), max(0.0, min(vs)), min(1.0, max(us)), min(1.0, max(vs))
        if u1 - u0 < 0.004 or v1 - v0 < 0.004 or u0 >= 1 or v0 >= 1 or u1 <= 0 or v1 <= 0:
            return None
        return u0, v0, u1, v1

    def close(self) -> None:
        self.connected = False

    def status(self) -> dict[str, Any]:
        return {"connected": self.connected, "last_error": self.last_error}


def video_source_for(settings: dict[str, Any], *, origin: tuple[float, float], camera: CameraModel, telemetry: Callable[[], Any],
                     config: dict[str, Any], secrets: Callable[[str], str | None] | None) -> Any:
    source = settings.get("source", {})
    if source.get("type") == "simulated_aerial":
        return SimulatedAerialSource(source.get("objects", []), origin, camera, telemetry, fps=float(settings.get("fps", 5)))
    from ..sources import open_source

    pseudo_camera = {"id": "drone_video", "name": "Drone camera", "source": source, "fps": float(settings.get("fps", 5))}
    return open_source(pseudo_camera, config, secrets)


class DroneVideoWorker(threading.Thread):
    """One per drone with `drone.video` configured. Idle (source closed) unless
    an inspection is in progress."""

    def __init__(self, manager: Any, settings: dict[str, Any], *, config: dict[str, Any], media_root: Path,
                 record_evidence: Callable[[str, str, int], None], secrets: Callable[[str], str | None] | None = None,
                 detector_factory: Callable[[], Any] | None = None, source_factory: Callable[[], Any] | None = None) -> None:
        super().__init__(name=f"drone-video-{manager.drone_id}", daemon=True)
        self.manager = manager
        self.settings = settings
        self.config = config
        self.media_root = media_root
        self.record_evidence = record_evidence
        self.secrets = secrets
        self.camera = CameraModel.from_dict(settings.get("camera"))
        self.detector_factory = detector_factory or self._default_detector
        self.source_factory = source_factory or (lambda: video_source_for(
            settings, origin=manager.site_origin, camera=self.camera, telemetry=manager.adapter.telemetry, config=config, secrets=secrets))
        self.fps = float(settings.get("fps", 5))
        self.state = "idle"
        self.detail = "waiting for an approved inspection"
        self.frames = 0
        self.last_frame_ms = 0
        self.latest_jpeg: bytes | None = None
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        return {"configured": True, "state": self.state, "detail": self.detail, "frames": self.frames, "last_frame_ms": self.last_frame_ms,
                "source": self.settings.get("source", {}).get("type", "")}

    def _default_detector(self) -> Any:
        from ..detectors import create_detector, detection_thresholds, resolve_detector_settings

        if self.settings.get("source", {}).get("type") == "simulated_aerial":
            raw = {"type": "scripted"}
        else:
            raw = self.config.get("detector", {}) | self.settings.get("detector", {})
        settings = resolve_detector_settings(raw)
        threshold, _ = detection_thresholds(settings)
        return create_detector(settings, threshold)

    def run(self) -> None:
        while not self._stop.is_set():
            active = self.manager.active_inspection()
            if active is None:
                self.state, self.detail = "idle", "waiting for an approved inspection"
                self._stop.wait(0.5)
                continue
            try:
                self._inspect(active)
            except Exception as error:  # contained: a video failure must never affect the safety loop
                self.state, self.detail = "error", f"{type(error).__name__}: {error}"[:200]
                log.exception("drone video failed during %s", active["request_id"])
                self._stop.wait(2.0)

    def _inspect(self, request: dict[str, Any]) -> None:
        import cv2

        from ..vision import encode_jpeg

        request_id = request["request_id"]
        observer = InspectionObserver(request_id, (request["target_lat"], request["target_lon"]), request["target_label"], self.camera,
                                      self.manager.site_origin)
        evidence: list[str] = []
        detector = self.detector_factory()
        source = self.source_factory()
        self.state, self.detail = "streaming", f"analysing inspection {request_id}"
        last_publish = 0.0
        try:
            while not self._stop.is_set():
                current = self.manager.active_inspection()
                if current is None or current["request_id"] != request_id:
                    break
                frame = source.read()
                if frame is None:
                    self.detail = "no frame from the drone camera"
                    self._stop.wait(0.2)
                    continue
                self.frames += 1
                self.last_frame_ms = frame.timestamp_ms
                pose = pose_from_telemetry(self.manager.adapter.telemetry(), self.camera, frame.timestamp_ms)
                detections = detector.detect(frame)
                new = observer.update(detections, frame.timestamp_ms, pose)
                image = frame.image.copy()
                h, w = image.shape[:2]
                for det in detections:
                    x, y, bw, bh = det.bbox
                    cv2.rectangle(image, (int(x * w), int(y * h)), (int((x + bw) * w), int((y + bh) * h)), (0, 220, 255), 2)
                    cv2.putText(image, f"{det.label} {det.confidence:.2f}", (int(x * w), max(12, int(y * h) - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 220, 255), 1)
                self.latest_jpeg = encode_jpeg(image)
                for observation in new:
                    observation.snapshot_path = self._save_snapshot(request_id, observation.observation_id, frame.timestamp_ms, self.latest_jpeg)
                    evidence.append(observation.snapshot_path)
                if time.monotonic() - last_publish >= 2.0:
                    last_publish = time.monotonic()
                    self.manager.attach_observations(request_id, observer.summary(), evidence)
        finally:
            source.close()
            close = getattr(detector, "close", None)
            if close:
                close()
            self.manager.attach_observations(request_id, observer.summary() | {"final": True}, evidence)
            self.state, self.detail = "idle", "inspection finished"

    def _save_snapshot(self, request_id: str, observation_id: str, frame_ms: int, jpeg: bytes) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{frame_ms}_{observation_id.rsplit('_', 1)[-1]}")
        relative = f"drone/{re.sub(r'[^A-Za-z0-9_.-]', '_', request_id)}/{safe}.jpg"
        path = self.media_root / relative
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(jpeg)
            self.record_evidence(relative, hashlib.sha256(jpeg).hexdigest(), len(jpeg))
        except OSError as error:  # contained, like other recording writes
            log.error("drone snapshot not saved: %s", error)
            return ""
        return relative


__all__ = ["DroneVideoWorker", "SimulatedAerialSource", "video_source_for"]
