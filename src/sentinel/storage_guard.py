"""Disk-space protection for continuous recording.

Order of defence when the disk runs low: (1) delete the oldest unprotected
clips and snapshots, (2) if that is not enough, pause recording while
detection and alerts carry on, (3) resume recording once there is comfortable
headroom again. Protected recordings (open incidents, incidents marked
protected or resolved as a real concern) are never deleted to make room."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("sentinel.storage")


@dataclass
class StorageSettings:
    min_free_mb: float = 2048.0  # below this (or below min_free_pct) the disk is "low"
    min_free_pct: float = 5.0
    resume_margin: float = 1.5  # recording resumes once free space exceeds the threshold by this factor
    max_media_gb: float | None = None  # optional cap on <storage>/media
    check_interval_s: float = 60.0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "StorageSettings":
        data = data or {}
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


class StorageGuard:
    def __init__(
        self,
        settings: StorageSettings,
        media_root: Path,
        protected: Callable[[], set[str]],
        *,
        disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    ) -> None:
        self.settings = settings
        self.media_root = Path(media_root)
        self.protected = protected
        self.disk_usage = disk_usage
        self.recording_paused = False
        self.purged_total = 0
        self.status: dict[str, Any] = {}

    def threshold_mb(self, total_mb: float) -> float:
        return max(self.settings.min_free_mb, total_mb * self.settings.min_free_pct / 100)

    def check(self) -> dict[str, Any]:
        self.media_root.mkdir(parents=True, exist_ok=True)
        purged = 0
        if self.settings.max_media_gb:
            cap_mb = self.settings.max_media_gb * 1024
            size_mb = sum(path.stat().st_size for path in self.media_root.rglob("*") if path.is_file()) / 2**20  # protected files count too
            if size_mb > cap_mb:
                purged += self._purge(self._candidates(), lambda freed: size_mb - freed / 2**20 < cap_mb * 0.9)
        free_mb, total_mb = self._free()
        threshold = self.threshold_mb(total_mb)
        if free_mb < threshold:
            target = threshold * self.settings.resume_margin
            start_free = free_mb
            purged += self._purge(self._candidates(), lambda freed: start_free + freed / 2**20 >= target or self._free()[0] >= target)
            free_mb, total_mb = self._free()
        if free_mb < threshold and not self.recording_paused:
            self.recording_paused = True
            log.error("disk nearly full (%.0f MB free, need %.0f MB): recording paused, detection continues", free_mb, threshold)
        elif self.recording_paused and free_mb >= threshold * self.settings.resume_margin:
            self.recording_paused = False
            log.warning("disk space recovered (%.0f MB free): recording resumed", free_mb)
        if purged:
            self.purged_total += purged
            log.warning("disk space: deleted %d oldest unprotected media file(s)", purged)
        self.status = {
            "free_mb": round(free_mb), "total_mb": round(total_mb), "free_pct": round(100 * free_mb / total_mb, 1) if total_mb else 0.0,
            "threshold_mb": round(threshold), "low": free_mb < threshold, "recording_paused": self.recording_paused,
            "purged_now": purged, "purged_total": self.purged_total,
        }
        return self.status

    def _free(self) -> tuple[float, float]:
        usage = self.disk_usage(self.media_root)
        return usage.free / 2**20, usage.total / 2**20

    def _candidates(self) -> list[tuple[Path, int, float]]:
        """Unprotected media files, oldest first."""
        protected = self.protected()
        files = []
        for path in self.media_root.rglob("*"):
            try:
                if path.is_file() and path.relative_to(self.media_root).as_posix() not in protected:
                    stat = path.stat()
                    files.append((path, stat.st_size, stat.st_mtime))
            except OSError:
                continue
        return sorted(files, key=lambda item: item[2])

    def _purge(self, files: list[tuple[Path, int, float]], enough: Callable[[int], bool]) -> int:
        freed = deleted = 0
        for path, size, _ in files:
            if enough(freed):
                break
            try:
                path.unlink()
            except OSError:
                continue
            freed += size
            deleted += 1
        return deleted
