"""Flame Mode: the Sentinel drone's optional agricultural burn-monitoring
configuration (fields, burn operations, thermal watch, reports). Monitoring
only: Sentinel never controls ignition or release hardware."""

from .service import FlameError, FlameService

__all__ = ["FlameError", "FlameService"]
