"""SpeciesNet crop classifier on ONNX Runtime, and the background worker that
keeps it off the camera threads (I/O edge: numpy, OpenCV, onnxruntime).

The model is Google's SpeciesNet v4 `always_crop` classifier (Apache-2.0),
exported once to ONNX by scripts/guardian/export_speciesnet_onnx.py. Crops come
from Sentinel's own detector boxes, so MegaDetector is never needed. Preprocessing
matches SpeciesNet: crop the box, resize to 480x480 (bilinear, no antialias),
RGB, NHWC float in 0..1, softmax over the logits."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .species import Geofence, SpeciesSettings, Taxon, parse_labels

log = logging.getLogger("sentinel.guardian.species")

IMG_SIZE = 480
TOP_K = 10


class SpeciesNetOnnx:
    """Labels and geofence load immediately (small); the ONNX session (about 300 MB)
    loads on the first classification, so it never competes with the detector's
    start-up and costs nothing on sites where no animal is ever tracked."""

    def __init__(self, model_dir: str | Path, providers: Any = "auto", cpu_threads: int = 2, lazy: bool = False) -> None:
        self.model_dir = Path(model_dir)
        info = json.loads((self.model_dir / "info.json").read_text(encoding="utf-8"))
        if info.get("type") != "always_crop":
            raise ValueError(f"{self.model_dir}: expected the SpeciesNet always_crop classifier")
        onnx_path = self.model_dir / "speciesnet.onnx"
        if not onnx_path.exists():
            raise FileNotFoundError(f"{onnx_path} missing: run scripts/guardian/export_speciesnet_onnx.py")
        self.onnx_path, self.providers, self.cpu_threads = onnx_path, providers, cpu_threads
        self.version = str(info.get("version", "unknown"))
        self.taxa: list[Taxon] = parse_labels(
            (self.model_dir / info["classifier_labels"]).read_text(encoding="utf-8").splitlines())
        geofence_path = self.model_dir / info.get("geofence", "")
        self.geofence = Geofence(json.loads(geofence_path.read_text(encoding="utf-8"))
                                 if info.get("geofence") and geofence_path.is_file() else None)
        self.session: Any = None
        self.load_ms = 0.0
        self._lock = threading.Lock()
        if not lazy:
            self.ensure_loaded()

    def ensure_loaded(self) -> None:
        from ..detectors_onnx import _session, resolve_providers

        with self._lock:
            if self.session is not None:
                return
            started = time.perf_counter()
            session = _session(self.onnx_path, resolve_providers(self.providers), self.cpu_threads)
            outputs = session.get_outputs()[0].shape[-1]
            if isinstance(outputs, int) and outputs != len(self.taxa):
                raise ValueError(f"model has {outputs} outputs but {len(self.taxa)} labels")
            self.session, self.load_ms = session, (time.perf_counter() - started) * 1000

    @property
    def provider(self) -> str | None:
        return self.session.get_providers()[0] if self.session is not None else None

    @staticmethod
    def cut(image_bgr: Any, bbox: tuple[float, float, float, float], pad: float = 0.0, min_px: int = 0) -> Any | None:
        """Copy of the box region (cheap, done on the camera thread), or None when the
        box is too small to say anything about the species."""
        height, width = image_bgr.shape[:2]
        x, y, w, h = bbox
        x0, y0 = (x - w * pad) * width, (y - h * pad) * height
        x1, y1 = (x + w * (1 + pad)) * width, (y + h * (1 + pad)) * height
        x0, y0 = max(0, int(x0)), max(0, int(y0))
        x1, y1 = min(width, int(x1)), min(height, int(y1))
        if x1 - x0 < max(2, min_px) or y1 - y0 < max(2, min_px):
            return None
        return image_bgr[y0:y1, x0:x1].copy()

    @staticmethod
    def prepare(cut_bgr: Any) -> Any:
        """480x480x3 float32 RGB in 0..1, as SpeciesNet expects."""
        import cv2
        import numpy as np

        crop = cv2.cvtColor(cut_bgr, cv2.COLOR_BGR2RGB)
        resized = cv2.resize(crop, (IMG_SIZE, IMG_SIZE), interpolation=cv2.INTER_LINEAR)
        return resized.astype(np.float32) / 255.0

    @classmethod
    def preprocess(cls, image_bgr: Any, bbox: tuple[float, float, float, float], pad: float = 0.0,
                   min_px: int = 0) -> Any | None:
        cut = cls.cut(image_bgr, bbox, pad, min_px)
        return None if cut is None else cls.prepare(cut)

    def classify(self, crops: list[Any]) -> list[dict[int, float]]:
        """Top-k softmax scores (label index -> probability) for each crop."""
        import numpy as np

        if not crops:
            return []
        self.ensure_loaded()
        batch = np.stack(crops, axis=0).astype(np.float32)
        logits = self.session.run(None, {self.session.get_inputs()[0].name: batch})[0]
        logits = logits - logits.max(axis=-1, keepdims=True)
        probs = np.exp(logits)
        probs /= probs.sum(axis=-1, keepdims=True)
        out = []
        for row in probs:
            top = np.argpartition(-row, TOP_K)[:TOP_K]
            out.append({int(i): float(row[i]) for i in top})
        return out


@dataclass
class SpeciesJob:
    camera_id: str
    guardian_id: str
    crop: Any
    submitted: float


class SpeciesWorker:
    """One thread, one model, a small bounded queue. Jobs are dropped (and counted)
    when the queue is full, so slow classification can never back up the cameras.
    The model loads lazily on the worker thread; if it fails, Guardian keeps its
    detector labels and the failure is reported, never raised into a camera."""

    def __init__(self, settings: SpeciesSettings, on_result: Callable[[SpeciesJob, dict[int, float]], None],
                 on_failure: Callable[[SpeciesJob], None], on_ready: Callable[["SpeciesNetOnnx"], None],
                 on_error: Callable[[str], None], model_factory: Callable[[], Any] | None = None) -> None:
        self.settings = settings
        self.on_result, self.on_failure, self.on_ready, self.on_error = on_result, on_failure, on_ready, on_error
        self.model_factory = model_factory or (lambda: SpeciesNetOnnx(settings.model_dir, settings.providers, settings.cpu_threads, lazy=True))
        self.queue: queue.Queue[SpeciesJob | None] = queue.Queue(maxsize=max(1, settings.queue_size))
        self.model: Any = None
        self.state = "starting"
        self.error = ""
        self.classified = 0
        self.dropped = 0
        self.failures = 0
        self._ms: list[float] = []
        self._thread = threading.Thread(target=self._run, name="guardian-species", daemon=True)
        self._thread.start()

    def submit(self, job: SpeciesJob) -> bool:
        if self.state in ("failed", "stopped"):
            return False
        try:
            self.queue.put_nowait(job)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def stop(self, wait: bool = True) -> None:
        """`wait=False` when the caller holds a lock the worker's callbacks need."""
        self.state = "stopped" if self.state != "failed" else self.state
        while True:  # drop queued jobs so the stop marker always fits
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break
        try:
            self.queue.put_nowait(None)
        except queue.Full:  # a racing submit refilled it; the thread still sees state "stopped"
            pass
        if wait:
            self._thread.join(timeout=5)

    def status(self) -> dict[str, Any]:
        recent = self._ms[-50:]
        return {
            "state": self.state,
            "error": self.error,
            "model": f"SpeciesNet {getattr(self.model, 'version', '')}".strip(),
            "provider": getattr(self.model, "provider", None),
            "load_ms": round(getattr(self.model, "load_ms", 0.0), 1) if self.model else None,
            "classified": self.classified,
            "dropped": self.dropped,
            "failures": self.failures,
            "mean_ms": round(sum(recent) / len(recent), 1) if recent else None,
        }

    def _run(self) -> None:
        try:
            self.model = self.model_factory()
            if self.state == "stopped":
                return
            self.state = "ready"
            log.info("species classifier ready (model loads on first animal)")
            self.on_ready(self.model)
        except Exception as error:
            self.state, self.error = "failed", str(error)[:300]
            log.warning("species classifier unavailable: %s", self.error)
            self.on_error(self.error)
        while True:
            job = self.queue.get()
            if job is None or self.state == "stopped":
                return
            if self.state != "ready":
                self.on_failure(job)
                continue
            started = time.perf_counter()
            try:
                prepare = getattr(self.model, "prepare", None)
                scores = self.model.classify([prepare(job.crop) if prepare else job.crop])[0]
            except Exception as error:
                self.failures += 1
                self.error = str(error)[:300]
                log.warning("species classification failed: %s", self.error)
                self.on_failure(job)
                continue
            self._ms.append((time.perf_counter() - started) * 1000)
            del self._ms[:-200]
            self.classified += 1
            self.on_result(job, scores)
