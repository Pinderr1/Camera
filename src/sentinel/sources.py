from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Protocol

from .config import redact_url
from .vision import Frame

log = logging.getLogger("sentinel.sources")


class FrameSource(Protocol):
    connected: bool
    last_error: str

    def read(self) -> Frame | None: ...

    def close(self) -> None: ...


class OpenCVSource:
    """RTSP/HTTP/USB/file capture. A reader thread keeps only the newest frame
    so a slow detector never builds up stream latency, and reconnects with
    backoff when an IP camera drops off the network."""

    def __init__(self, camera: dict[str, Any]) -> None:
        source = camera["source"]
        kind = source["type"]
        if kind == "usb":
            self.target: Any = int(source.get("index", 0))
        elif kind == "file":
            self.target = source["path"]
        else:
            self.target = source["url"]
        self.is_file = kind == "file"
        self.loop_file = self.is_file and source.get("loop", True)
        self.display_name = redact_url(str(self.target))
        if kind == "rtsp":
            os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
        self.connected = False
        self.last_error = ""
        self._latest: Frame | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"capture-{camera['id']}", daemon=True)
        self._thread.start()

    def read(self) -> Frame | None:
        with self._lock:
            frame, self._latest = self._latest, None
        return frame

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _open(self):
        import cv2

        capture = cv2.VideoCapture(self.target)
        if not capture.isOpened() and isinstance(self.target, int):
            capture = cv2.VideoCapture(self.target, cv2.CAP_DSHOW)
        return capture

    def _run(self) -> None:
        import cv2

        backoff = 1.0
        while not self._stop.is_set():
            capture = self._open()
            if not capture.isOpened():
                self.connected = False
                self.last_error = f"cannot open {self.display_name}"
                log.warning("%s; retrying in %.0fs", self.last_error, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            self.connected = True
            self.last_error = ""
            backoff = 1.0
            # Files would otherwise be consumed as fast as they decode; pace them like a live feed.
            file_fps = capture.get(cv2.CAP_PROP_FPS) if self.is_file else 0
            while not self._stop.is_set():
                ok, image = capture.read()
                if not ok:
                    if self.loop_file:
                        capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    self.connected = False
                    self.last_error = "stream ended or dropped"
                    break
                with self._lock:
                    self._latest = Frame(image=image, timestamp_ms=int(time.time() * 1000))
                if file_fps:
                    time.sleep(1.0 / file_fps)
            capture.release()


def open_source(camera: dict[str, Any], config: dict[str, Any]) -> FrameSource:
    if camera["source"]["type"] == "simulated":
        from .simulation import SimulatedSource, load_scenario_for

        return SimulatedSource(camera, load_scenario_for(config, camera["source"]["scenario"]))
    return OpenCVSource(camera)
