"""Wilderness Guardian: movement intelligence for animals (Phase 2).

Stdlib only, platform-neutral. See docs/wilderness-guardian.md."""

from .monitor import GuardianMonitor, GuardianSettings, species_view
from .risk import LEVEL_NAMES, LEVELS

__all__ = ["GuardianMonitor", "GuardianSettings", "LEVELS", "LEVEL_NAMES", "species_view"]
