from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Protocol

from .onboarding import open_capture, probe_rtsp, snapshot_opener
from .streams import SecretLookup, build_url, log_name, scrub, selected_stream
from .vision import Frame

log = logging.getLogger("sentinel.sources")


class FrameSource(Protocol):
    connected: bool
    last_error: str

    def read(self) -> Frame | None: ...

    def close(self) -> None: ...

    def status(self) -> dict[str, Any]: ...


class _ThreadedSource:
    """Shared reader-thread plumbing: keep only the newest frame, track health."""

    def __init__(self, camera: dict[str, Any]) -> None:
        self.camera = camera
        self.name = log_name(camera)
        self.connected = False
        self.last_error = ""
        self.error_kind = ""
        self.reconnects = 0
        self.frames_read = 0
        self.resolution: tuple[int, int] | None = None
        self.source_fps: float | None = None
        self.hw_decode_active = False
        self.stream = "main"
        self._latest: Frame | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"capture-{camera['id']}", daemon=True)

    def start(self) -> "_ThreadedSource":
        self._thread.start()
        return self

    def read(self) -> Frame | None:
        with self._lock:
            frame, self._latest = self._latest, None
        return frame

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def status(self) -> dict[str, Any]:
        return {
            "stream": self.stream,
            "resolution": list(self.resolution) if self.resolution else None,
            "source_fps": round(self.source_fps, 1) if self.source_fps else None,
            "hw_decode_active": self.hw_decode_active,
            "reconnects": self.reconnects,
            "error_kind": self.error_kind,
        }

    def _publish(self, image, pts_ms: float | None = None) -> None:
        meta = {"pts_ms": round(pts_ms, 1)} if pts_ms and pts_ms > 0 else {}
        with self._lock:
            self._latest = Frame(image=image, timestamp_ms=int(time.time() * 1000), meta=meta)
            self.frames_read += 1
        if self.resolution is None:
            self.resolution = (image.shape[1], image.shape[0])

    def _failed(self, message: str, kind: str, backoff: float) -> float:
        self.connected = False
        self.last_error = scrub(message)
        self.error_kind = kind
        self.reconnects += 1
        log.warning("camera %s: %s (%s); retrying in %.0fs", self.name, self.last_error, kind, backoff)
        self._stop.wait(backoff)
        return min(backoff * 2, 30.0)

    def _run(self) -> None:  # pragma: no cover - implemented by subclasses
        raise NotImplementedError


class OpenCVSource(_ThreadedSource):
    """RTSP/HTTP-MJPEG/USB/capture-card/file capture through OpenCV's FFmpeg
    backend. Prefers the camera's sub-stream for AI, tries hardware decoding
    first when `hw_decode` is "auto", and reconnects with backoff."""

    def __init__(self, camera: dict[str, Any], secrets: SecretLookup | None = None) -> None:
        super().__init__(camera)
        source = camera["source"]
        self.kind = source["type"]
        self.is_file = self.kind == "file"
        self.loop_file = self.is_file and source.get("loop", True)
        self.hw_decode = source.get("hw_decode", "auto")
        if self.kind in {"usb", "capture"}:
            self.target: Any = int(source.get("index", 0))
        elif self.is_file:
            self.target = source["path"]
        else:
            self.stream = selected_stream(source, "ai")
            self.target = build_url(source, self.stream, secrets)
        if self.kind == "rtsp":
            transport = source.get("transport", "tcp")
            os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", f"rtsp_transport;{transport}")
        self.start()

    def _open(self):
        source = self.camera["source"]
        capture, self.hw_decode_active = open_capture(
            self.target, hw_decode=self.hw_decode, backend=source.get("backend", "auto"), resolution=source.get("resolution")
        )
        return capture

    def _run(self) -> None:
        import cv2

        backoff = 1.0
        while not self._stop.is_set():
            capture = self._open()
            if not capture.isOpened():
                backoff = self._failed("cannot open stream", self._classify_failure(), backoff)
                continue
            self.connected = True
            self.last_error = self.error_kind = ""
            backoff = 1.0
            reported = capture.get(cv2.CAP_PROP_FPS)
            self.source_fps = reported if 0 < reported < 240 else None
            # Files would otherwise be consumed as fast as they decode; pace them like a live feed.
            file_fps = reported if self.is_file and reported > 0 else 0
            while not self._stop.is_set():
                ok, image = capture.read()
                if not ok:
                    if self.loop_file:
                        capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    self.connected = False
                    self.last_error, self.error_kind = "stream ended or dropped", "stream_dropped"
                    self.reconnects += 1
                    break
                self._publish(image, capture.get(cv2.CAP_PROP_POS_MSEC))  # stream timestamp: shows whether frames are new
                if file_fps:
                    time.sleep(1.0 / file_fps)
            capture.release()

    def _classify_failure(self) -> str:
        if self.kind == "rtsp":
            return probe_rtsp(self.target, timeout=4)["kind"]
        if isinstance(self.target, int):
            return "device_unavailable"
        return "open_failed"


class HttpSnapshotSource(_ThreadedSource):
    """Polls a JPEG snapshot URL at the camera fps. The fallback for DVRs and
    cameras that expose no RTSP stream, only `/snapshot.jpg`-style endpoints."""

    def __init__(self, camera: dict[str, Any], secrets: SecretLookup | None = None) -> None:
        super().__init__(camera)
        source = camera["source"]
        self.url = build_url(source, "main", secrets)
        self.interval = 1.0 / float(camera.get("fps", 5))
        self.stream = "snapshot"
        self.start()

    def _run(self) -> None:
        import cv2
        import numpy as np

        opener, plain_url = snapshot_opener(self.url)
        backoff = 1.0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                with opener.open(plain_url, timeout=5) as response:
                    image = cv2.imdecode(np.frombuffer(response.read(), np.uint8), cv2.IMREAD_COLOR)
                if image is None:
                    raise ValueError("response is not an image")
            except Exception as error:  # network, auth, or decode failure: report and retry
                kind = "auth_failed" if "401" in str(error) else "not_found" if "404" in str(error) else "unreachable"
                backoff = self._failed(type(error).__name__ + ": " + str(error), kind, backoff)
                continue
            self.connected, self.last_error, self.error_kind, backoff = True, "", "", 1.0
            self._publish(image)
            self._stop.wait(max(0.0, self.interval - (time.monotonic() - started)))


def open_source(camera: dict[str, Any], config: dict[str, Any], secrets: SecretLookup | None = None) -> FrameSource:
    kind = camera["source"]["type"]
    if kind == "simulated":
        from .simulation import SimulatedSource, load_scenario_for

        return SimulatedSource(camera, load_scenario_for(config, camera["source"]["scenario"]))
    if kind == "http" and camera["source"].get("mode") == "snapshot":
        return HttpSnapshotSource(camera, secrets)
    return OpenCVSource(camera, secrets)
