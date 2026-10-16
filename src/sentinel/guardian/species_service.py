"""Glue between the camera workers, the Guardian monitor and the species worker
(I/O edge). Optional: when the classifier is off, missing or broken, Guardian
runs exactly as before on detector labels.

Flow per processed frame (camera thread, after the runtime update):
  monitor.species_requests() under the runtime lock -> copy each due box ->
  SpeciesWorker queue (bounded; full means skip, never wait).
The worker thread classifies and hands scores back to monitor.species_result()
under the same lock. A track is classified a few times while visible
(`reclassify_s`, `max_crops_per_track`), never every frame."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from .risk import CLASSIFIER_PROFILES
from .species import SpeciesPolicy, SpeciesSettings
from .species_onnx import SpeciesJob, SpeciesNetOnnx, SpeciesWorker

log = logging.getLogger("sentinel.guardian.species")
COMPONENT = "guardian_species"


class SpeciesService:
    def __init__(self, settings: SpeciesSettings, lock: Any, get_monitor: Callable[[], Any],
                 set_health: Callable[[str, str], None], model_factory: Callable[[], Any] | None = None) -> None:
        self.settings = settings
        self.lock = lock
        self.get_monitor = get_monitor
        self.set_health = set_health
        self.policy: SpeciesPolicy | None = None
        self.worker = SpeciesWorker(settings, on_result=self._result, on_failure=self._failure,
                                    on_ready=self._ready, on_error=self._error, model_factory=model_factory)

    # ---- worker callbacks (worker thread) ---------------------------------

    def _ready(self, model: Any) -> None:
        display = {key: profile.display for key, profile in CLASSIFIER_PROFILES.items()}
        self.policy = SpeciesPolicy(model.taxa, self.settings, getattr(model, "geofence", None), display)
        self.attach()
        self.set_health("ok", f"SpeciesNet {getattr(model, 'version', '')} ready; loads on the first animal")

    def _error(self, error: str) -> None:
        self.set_health("degraded", f"species classifier unavailable ({error}); detector labels are used")

    def _result(self, job: SpeciesJob, scores: dict[int, float]) -> None:
        with self.lock:
            self.get_monitor().species_result(job.guardian_id, scores)

    def _failure(self, job: SpeciesJob) -> None:
        with self.lock:
            self.get_monitor().species_skipped(job.guardian_id)

    # ---- camera thread ------------------------------------------------------

    def attach(self) -> None:
        """(Re)attach the policy, e.g. after a config edit replaced the monitor."""
        with self.lock:
            self.get_monitor().attach_species(self.policy)

    def frame(self, camera_id: str, now_ms: int, image: Any) -> None:
        if self.policy is None or image is None:
            return
        with self.lock:
            requests = self.get_monitor().species_requests(camera_id, now_ms)
        for guardian_id, bbox in requests:
            cut = SpeciesNetOnnx.cut(image, bbox, self.settings.pad, self.settings.min_box_px)
            if cut is None or not self.worker.submit(SpeciesJob(camera_id, guardian_id, cut, time.time())):
                with self.lock:
                    self.get_monitor().species_skipped(guardian_id)

    def status(self) -> dict[str, Any]:
        return {"enabled": True, **self.worker.status(),
                "settings": {k: getattr(self.settings, k) for k in ("min_confidence", "min_margin", "min_agreeing",
                                                                     "reclassify_s", "max_crops_per_track", "country")}}

    def stop(self, wait: bool = True) -> None:
        self.worker.stop(wait)
