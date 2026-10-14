"""Host resource sampling with the standard library only: process and system
CPU, memory, disk space, and (when `nvidia-smi` exists) NVIDIA GPU load.

DirectML exposes no utilisation counters, so on non-NVIDIA GPUs the GPU block
is absent; inference health then comes from detector latency and errors."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("sentinel.resources")


class ResourceSampler:
    def __init__(self, storage_dir: Path, *, gpu_interval_s: float = 30.0) -> None:
        self.storage_dir = Path(storage_dir)
        self.gpu_interval_s = gpu_interval_s
        self._cpu_last: tuple[float, float] | None = None  # (wall, process cpu seconds)
        self._system_last: tuple[int, int] | None = None  # (idle, total) ticks
        self._gpu: dict[str, Any] | None = None
        self._gpu_checked = 0.0
        self._gpu_tool = shutil.which("nvidia-smi")
        self._lock = threading.Lock()
        self.latest: dict[str, Any] = {}

    def sample(self) -> dict[str, Any]:
        now = time.monotonic()
        cpu_seconds = sum(os.times()[:2])
        process_cpu = None
        if self._cpu_last is not None and now > self._cpu_last[0]:
            process_cpu = round(100 * (cpu_seconds - self._cpu_last[1]) / (now - self._cpu_last[0]) / (os.cpu_count() or 1), 1)
        self._cpu_last = (now, cpu_seconds)
        result: dict[str, Any] = {
            "time_ms": int(time.time() * 1000),
            "cpu_count": os.cpu_count(),
            "process_cpu_pct": process_cpu,  # share of the whole machine
            "system_cpu_pct": self._system_cpu(),
            "process_rss_mb": _process_rss_mb(),
            "system_memory": _system_memory(),
            "disk": disk_status(self.storage_dir),
            "threads": threading.active_count(),
        }
        gpu = self._gpu_sample(now)
        if gpu:
            result["gpu"] = gpu
        with self._lock:
            self.latest = result
        return result

    def _system_cpu(self) -> float | None:
        ticks = _system_cpu_ticks()
        if ticks is None:
            return None
        previous, self._system_last = self._system_last, ticks
        if previous is None or ticks[1] <= previous[1]:
            return None
        busy = 1 - (ticks[0] - previous[0]) / (ticks[1] - previous[1])
        return round(max(0.0, min(1.0, busy)) * 100, 1)

    def _gpu_sample(self, now: float) -> dict[str, Any] | None:
        if not self._gpu_tool or now - self._gpu_checked < self.gpu_interval_s:
            return self._gpu
        self._gpu_checked = now
        try:
            output = subprocess.run(
                [self._gpu_tool, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout.strip().splitlines()
            name, util, used, total, temperature = [part.strip() for part in output[0].split(",")]
            self._gpu = {"name": name, "utilization_pct": float(util), "memory_used_mb": float(used),
                         "memory_total_mb": float(total), "temperature_c": float(temperature)}
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            self._gpu = None
        return self._gpu


def disk_status(path: Path) -> dict[str, Any]:
    path = Path(path)
    while not path.exists() and path.parent != path:
        path = path.parent
    usage = shutil.disk_usage(path)
    return {"total_mb": round(usage.total / 2**20), "free_mb": round(usage.free / 2**20), "free_pct": round(100 * usage.free / usage.total, 1) if usage.total else 0.0}


def directory_size_mb(path: Path) -> float:
    total = 0
    if Path(path).exists():
        for item in Path(path).rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    return round(total / 2**20, 1)


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _MemoryStatus(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD), ("ullTotalPhys", ctypes.c_uint64),
                    ("ullAvailPhys", ctypes.c_uint64), ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64), ("ullAvailExtendedVirtual", ctypes.c_uint64)]

    class _ProcessMemory(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessMemory), wintypes.DWORD]

    def _process_rss_mb() -> float | None:
        counters = _ProcessMemory()
        counters.cb = ctypes.sizeof(counters)
        if not _psapi.GetProcessMemoryInfo(_kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return None
        return round(counters.WorkingSetSize / 2**20, 1)

    def _system_memory() -> dict[str, Any] | None:
        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        if not _kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return {"total_mb": round(status.ullTotalPhys / 2**20), "available_mb": round(status.ullAvailPhys / 2**20), "used_pct": float(status.dwMemoryLoad)}

    def _system_cpu_ticks() -> tuple[int, int] | None:
        idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not _kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        value = lambda ft: (ft.dwHighDateTime << 32) | ft.dwLowDateTime  # noqa: E731
        return value(idle), value(kernel) + value(user)  # kernel time includes idle time

else:

    def _process_rss_mb() -> float | None:
        try:
            pages = int(Path("/proc/self/statm").read_text().split()[1])
            return round(pages * os.sysconf("SC_PAGE_SIZE") / 2**20, 1)
        except (OSError, ValueError, IndexError):
            return None

    def _system_memory() -> dict[str, Any] | None:
        try:
            info = {line.split(":")[0]: int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines() if line.split()}
            total, available = info["MemTotal"], info.get("MemAvailable", info.get("MemFree", 0))
            return {"total_mb": round(total / 1024), "available_mb": round(available / 1024), "used_pct": round(100 * (1 - available / total), 1)}
        except (OSError, ValueError, KeyError, ZeroDivisionError):
            return None

    def _system_cpu_ticks() -> tuple[int, int] | None:
        try:
            values = [int(part) for part in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
            return values[3] + (values[4] if len(values) > 4 else 0), sum(values)
        except (OSError, ValueError, IndexError):
            return None
