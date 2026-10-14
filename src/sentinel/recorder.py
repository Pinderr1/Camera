from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("sentinel.recorder")
_working_codec: str | None = None


class ClipRecorder:
    """Per-camera pre-roll ring buffer (JPEG-compressed to bound memory) plus an
    MP4 writer that runs while an incident on this camera stays active."""

    def __init__(self, camera_id: str, media_root: Path, settings: dict[str, Any]) -> None:
        self.camera_id = camera_id
        self.media_root = media_root
        self.enabled = bool(settings.get("enabled", True))
        self.pre_roll_ms = int(settings.get("pre_roll_s", 5) * 1000)
        self.post_roll_ms = int(settings.get("post_roll_s", 8) * 1000)
        self.max_clip_ms = int(settings.get("max_clip_s", 60) * 1000)
        self.fps = float(settings.get("fps", 5))
        self.max_width = int(settings.get("max_width", 960))
        self._buffer: deque[tuple[int, bytes]] = deque()
        self._last_buffered_ms = 0
        self._writer = None
        self._clip: dict[str, Any] | None = None
        self.paused = False  # set by the storage guard when the disk is nearly full
        self.on_finalized = None  # callback(media-relative path) once a clip or snapshot is complete (evidence hashing)

    def set_paused(self, paused: bool) -> None:
        """Called from another thread: only flips the flag; the camera thread,
        which owns the writer, closes any open clip on its next frame."""
        self.paused = paused

    def add_frame(self, image: Any, timestamp_ms: int) -> None:
        if self.paused:
            if self._clip or self._buffer:
                self.finish()
                self._buffer.clear()
            return
        if not self.enabled or timestamp_ms - self._last_buffered_ms < 1000 / self.fps:
            return
        import cv2

        self._last_buffered_ms = timestamp_ms
        image = self._resize(image)
        ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            self._buffer.append((timestamp_ms, jpeg.tobytes()))
        while self._buffer and timestamp_ms - self._buffer[0][0] > self.pre_roll_ms:
            self._buffer.popleft()
        if self._clip:
            self._writer.write(image)
            if timestamp_ms > self._clip["until_ms"] or timestamp_ms - self._clip["started_ms"] > self.max_clip_ms:
                self.finish()

    def trigger(self, incident_id: str, timestamp_ms: int) -> str:
        """Start or extend the clip for an incident; returns its media-relative path."""
        if not self.enabled or self.paused:
            return ""
        if self._clip:
            self._clip["until_ms"] = timestamp_ms + self.post_roll_ms
            return self._clip["relative_path"]
        if not self._buffer:
            return ""
        import cv2
        import numpy as np

        stamp = datetime.fromtimestamp(timestamp_ms / 1000)
        relative = Path("clips") / self.camera_id / f"{stamp:%Y%m%d}" / f"{stamp:%H%M%S}_{incident_id}.mp4"
        target = self.media_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        first = cv2.imdecode(np.frombuffer(self._buffer[0][1], dtype=np.uint8), cv2.IMREAD_COLOR)
        height, width = first.shape[:2]
        global _working_codec
        writer = None
        # H.264 plays in browsers; mp4v is the portable fallback. Probe once per process.
        for codec in [_working_codec] if _working_codec else ["avc1", "mp4v"]:
            writer = cv2.VideoWriter(str(target), cv2.VideoWriter_fourcc(*codec), self.fps, (width, height))
            if writer.isOpened():
                _working_codec = codec
                break
        if writer is None or not writer.isOpened():
            log.error("no usable video codec for %s", target)
            return ""
        for _, jpeg in self._buffer:
            writer.write(cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR))
        self._writer = writer
        self._clip = {
            "relative_path": relative.as_posix(),
            "started_ms": self._buffer[0][0],
            "until_ms": timestamp_ms + self.post_roll_ms,
        }
        return self._clip["relative_path"]

    def snapshot(self, image: Any, timestamp_ms: int, name: str) -> str:
        if self.paused:
            return ""
        import cv2

        stamp = datetime.fromtimestamp(timestamp_ms / 1000)
        relative = Path("snapshots") / self.camera_id / f"{stamp:%Y%m%d}" / f"{stamp:%H%M%S}_{name}.jpg"
        target = self.media_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(target), self._resize(image), [cv2.IMWRITE_JPEG_QUALITY, 85]):
            raise OSError(f"could not write snapshot {relative.as_posix()}")
        return relative.as_posix()

    def clip_started_ms(self) -> int | None:
        return self._clip["started_ms"] if self._clip else None

    def finish(self) -> None:
        clip = self._clip
        if self._writer is not None:
            self._writer.release()
        self._writer = None
        self._clip = None
        if clip and self.on_finalized:
            self.on_finalized(clip["relative_path"])

    def _resize(self, image: Any) -> Any:
        import cv2

        height, width = image.shape[:2]
        if width <= self.max_width:
            return image
        scale = self.max_width / width
        return cv2.resize(image, (self.max_width, int(height * scale)))
