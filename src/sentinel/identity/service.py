"""Face worker and enrollment (I/O edge: numpy, OpenCV).

Flow per processed frame (camera thread, after the runtime update):
  monitor.requests() under the runtime lock -> copy the upper part of each due
  person box -> worker queue (bounded; full means skip, never wait).
The worker finds the face, computes its template, compares it with the
enrolled templates and hands only the verdict (member id and similarity, or
"no match") back to monitor.result() under the same lock. The template of a
face that matched nobody is discarded right there: it is never stored, logged,
returned by the API or sent anywhere.

Recognition never runs on drone video: only fixed-camera workers call frame()."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .faces import FaceAnalyzer, person_crop
from .gallery import FaceGallery
from .matching import FaceObservation, judge_scores
from .settings import IdentitySettings

log = logging.getLogger("sentinel.identity")
COMPONENT = "identity"
GALLERY_CHECK_S = 2.0


@dataclass
class FaceJob:
    camera_id: str
    track_id: str
    crop: Any
    frame_ms: int


class IdentityService:
    def __init__(self, settings: IdentitySettings, gallery: FaceGallery, lock: Any, get_monitor: Callable[[], Any],
                 on_changes: Callable[[list[dict[str, Any]], int], None], set_health: Callable[[str, str], None],
                 analyzer_factory: Callable[[], Any] | None = None) -> None:
        self.settings = settings
        self.gallery = gallery
        self.lock = lock
        self.get_monitor = get_monitor
        self.on_changes = on_changes
        self.set_health = set_health
        self.analyzer_factory = analyzer_factory or (lambda: FaceAnalyzer(settings.detector_model, settings.recognizer_model,
                                                                           cpu_threads=settings.cpu_threads))
        self.queue: queue.Queue[FaceJob | None] = queue.Queue(maxsize=max(1, settings.queue_size))
        self.state, self.error = "starting", ""
        self.checked = self.dropped = self.failures = 0
        self.members = 0
        self._matrix: Any = None
        self._owners: list[str] = []
        self._gallery_version = -1
        self._ms: list[float] = []
        self._thread = threading.Thread(target=self._run, name="identity-faces", daemon=True)
        self._thread.start()

    # ---- camera thread ----------------------------------------------------------

    def attach(self) -> None:
        """(Re)attach to the current monitor, e.g. after a config edit replaced it."""
        with self.lock:
            monitor = self.get_monitor()
            monitor.names = self.gallery.names()
            monitor.members_ready = self.state == "ready" and self.members > 0

    def frame(self, camera_id: str, now_ms: int, image: Any) -> None:
        if image is None or self.state != "ready":
            return
        with self.lock:
            requests = self.get_monitor().requests(camera_id, now_ms)
        for track_id, bbox in requests:
            crop = person_crop(image, bbox)
            if crop is None or not self._submit(FaceJob(camera_id, track_id, crop, now_ms)):
                with self.lock:
                    self.get_monitor().skipped(track_id)

    def _submit(self, job: FaceJob) -> bool:
        try:
            self.queue.put_nowait(job)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def status(self) -> dict[str, Any]:
        recent = self._ms[-50:]
        return {"state": self.state, "error": self.error, "members_ready": self.members, "faces_checked": self.checked,
                "dropped": self.dropped, "failures": self.failures,
                "mean_ms": round(sum(recent) / len(recent), 1) if recent else None}

    def stop(self, wait: bool = True) -> None:
        self.state = "stopped" if self.state != "failed" else self.state
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass
        if wait:
            self._thread.join(timeout=5)

    # ---- worker thread -------------------------------------------------------------

    def _load_gallery(self) -> None:
        import numpy as np

        version = self.gallery.refresh()
        if version == self._gallery_version:
            return
        self._gallery_version = version
        templates = self.gallery.templates(self.settings)
        owners, rows = [], []
        for member_id, member_templates in templates.items():
            for template in member_templates:
                owners.append(member_id)
                rows.append(template)
        self._owners = owners
        self._matrix = np.asarray(rows, dtype=np.float32) if rows else None
        self.members = len(templates)
        with self.lock:
            monitor = self.get_monitor()
            monitor.names = self.gallery.names()
            monitor.members_ready = self.state == "ready" and self.members > 0
        if self.gallery.error:
            self.set_health("degraded", self.gallery.error)
        elif self.members == 0:
            self.set_health("ok", "recognition on, but no household member has finished enrollment: everyone is unverified")
        else:
            self.set_health("ok", f"recognizing {self.members} enrolled household member(s) in the recognition zones")

    def _scores(self, template: Any) -> dict[str, float]:
        if self._matrix is None:
            return {}
        similarities = self._matrix @ template
        scores: dict[str, float] = {}
        for owner, value in zip(self._owners, similarities.tolist()):
            if value > scores.get(owner, -2.0):
                scores[owner] = value
        return scores

    def _run(self) -> None:
        try:
            analyzer = self.analyzer_factory()
            if self.state == "stopped":
                return
            self.state = "ready"
            self._load_gallery()
            log.info("household-member recognition ready (%d member(s))", self.members)
        except Exception as error:
            self.state, self.error = "failed", str(error)[:300]
            log.warning("household-member recognition unavailable: %s", self.error)
            self.set_health("degraded", f"recognition unavailable ({self.error}); everyone is shown as unverified")
            return
        last_check = time.monotonic()
        while True:
            try:
                job = self.queue.get(timeout=GALLERY_CHECK_S)
            except queue.Empty:
                job = False
            if job is None or self.state == "stopped":
                return
            if time.monotonic() - last_check >= GALLERY_CHECK_S:
                last_check = time.monotonic()
                try:
                    self._load_gallery()
                except Exception as error:
                    self.set_health("degraded", f"enrolled members could not be loaded: {str(error)[:160]}")
            if job is False:
                continue
            started = time.perf_counter()
            try:
                outcome, template, size = analyzer.person_face(job.crop, self.settings)
                if template is not None:
                    observation = judge_scores(self._scores(template), self.settings, size, job.frame_ms)
                    del template  # never kept: only the verdict leaves the worker
                else:
                    observation = FaceObservation(outcome, face_px=size, ms=job.frame_ms)
            except Exception as error:
                self.failures += 1
                self.error = str(error)[:300]
                log.warning("face check failed: %s", self.error)
                with self.lock:
                    self.get_monitor().skipped(job.track_id)
                continue
            self.checked += 1
            self._ms.append((time.perf_counter() - started) * 1000)
            del self._ms[:-200]
            with self.lock:
                changes = self.get_monitor().result(job.track_id, observation)
                if changes:
                    self.on_changes(changes, job.frame_ms)


class Enrollment:
    """Face templates from enrollment photos or a live camera frame. Loads the models
    on first use, independently of whether recognition is switched on."""

    def __init__(self, settings: IdentitySettings, analyzer_factory: Callable[[], Any] | None = None) -> None:
        self.settings = settings
        self._factory = analyzer_factory or (lambda: FaceAnalyzer(settings.detector_model, settings.recognizer_model))
        self._analyzer: Any = None
        self._lock = threading.Lock()

    def templates(self, images: list[Any]) -> list[list[float]]:
        with self._lock:
            if self._analyzer is None:
                self._analyzer = self._factory()
            return [self._analyzer.enrollment_template(image, self.settings) for image in images]
