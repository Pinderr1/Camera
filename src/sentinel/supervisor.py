"""Camera worker supervision: restart crashed or stalled workers, with
exponential backoff and a restart budget so a broken camera cannot spin in an
endless restart loop.

Python threads cannot be killed. A stalled worker (stuck inside a decoder or a
GPU call) is told to stop and *abandoned*: its results are ignored from then
on, and a fresh worker takes over. Stuck threads that never return are
counted, and past a limit the supervisor reports a fatal condition so the
process can be restarted by its service manager (`sentinel run`)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

log = logging.getLogger("sentinel.supervisor")


class SupervisedWorker(Protocol):
    failure: str | None
    heartbeat: float  # time.monotonic() of the last loop iteration
    started_at: float
    phase: str  # starting | running

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def is_alive(self) -> bool: ...

    def join(self, timeout: float | None = None) -> None: ...


@dataclass
class SupervisorSettings:
    stall_after_s: float = 30.0  # running worker without a heartbeat for this long is stalled
    startup_timeout_s: float = 300.0  # detector load (and first model download) must finish within this
    backoff_initial_s: float = 5.0
    backoff_max_s: float = 300.0
    max_restarts: int = 5  # automatic restarts allowed per window ...
    window_s: float = 3600.0
    cooldown_s: float = 1800.0  # ... then one trial restart per cooldown (circuit breaker)
    max_stuck_threads: int = 3  # abandoned threads still alive: beyond this the process should restart

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SupervisorSettings":
        data = data or {}
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


@dataclass
class _Entry:
    camera: dict[str, Any]
    worker: Any = None
    state: str = "running"  # running | restarting | failed | stopped
    restarts: list[float] = field(default_factory=list)  # monotonic restart times inside the window
    total_restarts: int = 0
    next_attempt: float = 0.0
    last_error: str = ""


class WorkerSupervisor:
    def __init__(
        self,
        settings: SupervisorSettings,
        factory: Callable[[dict[str, Any]], SupervisedWorker],
        on_status: Callable[[str, dict[str, Any]], None],
        *,
        on_fatal: Callable[[str], None] | None = None,
        on_restart: Callable[[str, str, bool], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self.settings = settings
        self.factory = factory
        self.on_status = on_status
        self.on_fatal = on_fatal
        self.on_restart = on_restart  # (camera_id, reason, manual)
        self.clock = clock
        self.wall_ms = wall_ms
        self.entries: dict[str, _Entry] = {}
        self.abandoned: list[Any] = []
        self.started = False
        self._fatal_reported = False

    @property
    def workers(self) -> dict[str, Any]:
        return {camera_id: entry.worker for camera_id, entry in self.entries.items() if entry.worker is not None}

    def add(self, camera: dict[str, Any]) -> None:
        entry = _Entry(camera=camera, worker=self.factory(camera))
        self.entries[camera["id"]] = entry
        if self.started:
            self._launch(camera["id"], entry)

    def remove(self, camera_id: str, timeout: float = 5.0) -> None:
        entry = self.entries.pop(camera_id, None)
        if entry and entry.worker is not None:
            entry.worker.stop()
            if entry.worker.is_alive():
                entry.worker.join(timeout=timeout)

    def start(self) -> None:
        self.started = True
        for camera_id, entry in self.entries.items():
            self._launch(camera_id, entry)

    def stop(self, timeout: float = 5.0) -> None:
        self.started = False
        for entry in self.entries.values():
            entry.state = "stopped"
            if entry.worker is not None:
                entry.worker.stop()
        deadline = self.clock() + timeout
        for entry in self.entries.values():
            if entry.worker is not None and entry.worker.is_alive():
                entry.worker.join(timeout=max(0.1, deadline - self.clock()))

    def restart(self, camera_id: str, reason: str = "restarted by operator") -> None:
        """Manual restart: also resets the restart budget and an open circuit."""
        entry = self.entries[camera_id]
        self._retire(entry, abandon=entry.worker is not None and entry.worker.is_alive())
        entry.restarts.clear()
        entry.last_error = ""
        entry.worker = self.factory(entry.camera)
        if self.on_restart:
            self.on_restart(camera_id, reason, True)
        self._launch(camera_id, entry)

    def check(self) -> None:
        """Called about once a second."""
        if not self.started:
            return
        now = self.clock()
        for camera_id, entry in self.entries.items():
            if entry.state == "running":
                problem = self._problem(entry.worker, now)
                if problem:
                    self._schedule_restart(camera_id, entry, problem, now)
                else:
                    self.on_status(camera_id, self._status_fields(entry))  # lets the recent-restart count decay
            elif entry.state in ("restarting", "failed") and now >= entry.next_attempt:
                entry.worker = self.factory(entry.camera)
                if self.on_restart:
                    self.on_restart(camera_id, entry.last_error, False)
                self._launch(camera_id, entry)
        self.abandoned = [worker for worker in self.abandoned if worker.is_alive()]
        if len(self.abandoned) > self.settings.max_stuck_threads and self.on_fatal and not self._fatal_reported:
            self._fatal_reported = True
            self.on_fatal(f"{len(self.abandoned)} camera processing threads are stuck and cannot be stopped")

    def status(self, camera_id: str) -> dict[str, Any]:
        entry = self.entries[camera_id]
        return self._status_fields(entry)

    # ---- internals -------------------------------------------------------------

    def _problem(self, worker: Any, now: float) -> str:
        if not worker.is_alive():
            return worker.failure or "processing stopped unexpectedly"
        if worker.phase == "starting":
            if now - worker.started_at > self.settings.startup_timeout_s:
                return f"processing did not start within {self.settings.startup_timeout_s:.0f} s"
        elif now - worker.heartbeat > self.settings.stall_after_s:
            return f"processing stalled (no progress for {now - worker.heartbeat:.0f} s)"
        return ""

    def _schedule_restart(self, camera_id: str, entry: _Entry, problem: str, now: float) -> None:
        self._retire(entry, abandon=entry.worker.is_alive())
        entry.last_error = problem
        entry.restarts = [at for at in entry.restarts if now - at <= self.settings.window_s] + [now]
        entry.total_restarts += 1
        if len(entry.restarts) > self.settings.max_restarts:
            entry.state = "failed"
            entry.next_attempt = now + self.settings.cooldown_s
            log.error("camera %s: %s; %d restarts within %.0f min, next trial in %.0f min",
                      camera_id, problem, len(entry.restarts) - 1, self.settings.window_s / 60, self.settings.cooldown_s / 60)
        else:
            delay = min(self.settings.backoff_initial_s * 2 ** (len(entry.restarts) - 1), self.settings.backoff_max_s)
            entry.state = "restarting"
            entry.next_attempt = now + delay
            log.warning("camera %s: %s; restarting in %.0f s", camera_id, problem, delay)
        self.on_status(camera_id, self._status_fields(entry))

    def _retire(self, entry: _Entry, *, abandon: bool) -> None:
        worker = entry.worker
        if worker is None:
            return
        worker.stop()
        if abandon:
            self.abandoned.append(worker)

    def _launch(self, camera_id: str, entry: _Entry) -> None:
        entry.state = "running"
        entry.worker.start()
        self.on_status(camera_id, self._status_fields(entry) | {"worker_started_ms": self.wall_ms()})

    def _status_fields(self, entry: _Entry) -> dict[str, Any]:
        now = self.clock()
        next_ms = self.wall_ms() + int((entry.next_attempt - now) * 1000) if entry.state in ("restarting", "failed") else 0
        recent = [at for at in entry.restarts if now - at <= self.settings.window_s]
        return {
            "worker_state": entry.state, "worker_error": entry.last_error, "restarts_total": entry.total_restarts,
            "restarts_recent": len(recent), "next_restart_ms": max(0, next_ms), "stalled": False,
        }
