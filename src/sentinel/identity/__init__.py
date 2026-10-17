"""Household-member recognition (optional, off by default): consent-based
enrollment, cautious known / unknown / unverified statuses, recognition zones.

The core (settings, matching, gallery, monitor, validate) is standard library
only. Face detection and embeddings (faces.py, OpenCV) and the worker
(service.py) are I/O edges. See docs/identity.md."""

from .matching import FaceObservation
from .monitor import IdentityMonitor
from .settings import IdentitySettings

__all__ = ["FaceObservation", "IdentityMonitor", "IdentitySettings"]
