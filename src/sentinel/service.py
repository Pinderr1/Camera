from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import backup
from .config import cameras, resolve_env
from .daemon import RunState, downtime
from .drone import build_drone_manager
from .netdiag import SystemNetwork
from .pipeline import CameraWorker
from .resources import ResourceSampler, directory_size_mb
from .runtime import SentinelRuntime
from .site_config import SiteConfigManager
from .storage_guard import StorageGuard, StorageSettings
from .supervisor import SupervisorSettings, WorkerSupervisor

log = logging.getLogger("sentinel.service")


class SentinelService:
    """Process-level assembly: runtime + supervised camera workers +
    housekeeping (health, storage guard, resources, backups, run state) +
    drone safety loop + optional MQTT mirror. The API holds a reference to this."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        start_cameras: bool = True,
        disk_usage: Callable[[Path], Any] = shutil.disk_usage,
        worker_factory: Callable[[dict[str, Any], SentinelRuntime], Any] = CameraWorker,
    ) -> None:
        """`config` may still contain ${ENV} placeholders; they are resolved for
        the running pipeline but never written back by the setup editor."""
        self.storage_dir = Path(config.get("storage", {}).get("dir", "data/sentinel"))
        raw = SiteConfigManager.load_effective(config, self.storage_dir)
        self.config_recovery = raw.pop("_config_recovery", None)
        config = resolve_env(raw)
        self.config = config
        self.start_cameras = start_cameras
        self.mqtt = None
        if config.get("mqtt", {}).get("enabled", False):
            from .mqtt import MqttPublisher

            self.mqtt = MqttPublisher(config["mqtt"], config["site"]["id"])
        store, self.db_recovery = backup.open_store(self.storage_dir)
        self.runtime = SentinelRuntime(config, mqtt_publish=self.mqtt.publish if self.mqtt else None, store=store)
        if self.mqtt:
            self.runtime.bus.subscribe(self.mqtt.on_bus)
        self.runtime.drone = build_drone_manager(
            config,
            audit=self.runtime.store.audit,
            on_change=lambda status: self.runtime.bus.publish("drone", status),
        )
        self.site = SiteConfigManager(raw, self.storage_dir, self.runtime.secrets)
        self.network = SystemNetwork()  # onboarding diagnostics; tests and demos swap in a SimulatedNetwork
        self.site.subscribe(self.apply_config)
        self.on_fatal: Callable[[str], None] | None = None  # set by `serve --exit-on-fatal`
        self.supervisor = WorkerSupervisor(
            SupervisorSettings.from_dict(config.get("health", {}).get("supervisor")),
            factory=lambda camera: worker_factory(camera, self.runtime),
            on_status=lambda camera_id, fields: self.runtime.update_camera_status(camera_id, **fields),
            on_fatal=self._fatal,
            on_restart=self._worker_restarted,
        )
        if start_cameras:
            for camera in cameras(config):
                self.supervisor.add(camera)
        self.resources = ResourceSampler(self.storage_dir)
        self.storage_guard = StorageGuard(
            StorageSettings.from_dict(config.get("storage")), self.runtime.media_root, self.runtime.store.protected_media, disk_usage=disk_usage
        )
        self.runstate = RunState(self.storage_dir)
        self.last_backup: dict[str, Any] | None = None
        self.last_backup_error = ""
        self._started = False
        self._config_lock = threading.Lock()
        self._backup_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    @property
    def drone(self):
        return self.runtime.drone

    @property
    def workers(self) -> dict[str, Any]:
        return self.supervisor.workers

    def start(self) -> None:
        other = self.runstate.other_instance()
        if other:
            raise RuntimeError(f"another Sentinel process (pid {other['pid']}) is already using {self.storage_dir}")
        previous = self.runstate.begin()
        self._record_startup(previous)
        self.resources.sample()  # CPU percentages need a baseline sample
        self._started = True
        self.supervisor.start()
        self._threads = [threading.Thread(target=self._housekeeping_loop, name="sentinel-housekeeping", daemon=True)]
        if self.drone is not None:
            self._threads.append(threading.Thread(target=self._drone_loop, name="sentinel-drone", daemon=True))
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        """Graceful: stop cameras (clips are finalised), close open outages,
        flush the database, and mark the run as cleanly stopped."""
        self._stop.set()
        self.supervisor.stop()
        for thread in self._threads:
            thread.join(timeout=5)
        if self.mqtt:
            self.mqtt.close()
        self.runtime.health.close_all(int(time.time() * 1000), "service stopped")
        self.runtime.close()
        self.runstate.end()

    def apply_config(self, raw: dict[str, Any]) -> None:
        """Called by the setup editor after a validated save: update rules and
        zones in place and restart only the cameras whose settings changed."""
        change = raw.pop("_change", {})
        raw.pop("_config_recovery", None)
        config = resolve_env(raw)
        with self._config_lock:
            previous = {camera["id"]: json.dumps(camera, sort_keys=True) for camera in cameras(self.config)}
            self.config = config
            self.runtime.reconfigure(config)
            self.supervisor.settings = SupervisorSettings.from_dict(config.get("health", {}).get("supervisor"))
            self.storage_guard.settings = StorageSettings.from_dict(config.get("storage"))
            if self.start_cameras:
                current = {camera["id"]: camera for camera in cameras(config)}
                for camera_id in list(self.supervisor.entries):
                    if camera_id not in current or json.dumps(current[camera_id], sort_keys=True) != previous.get(camera_id):
                        self.supervisor.remove(camera_id)
                for camera_id, camera in current.items():
                    if camera_id not in self.supervisor.entries:
                        self.supervisor.add(camera)
        self.runtime.store.audit(change.get("actor", "system"), "config_changed", {"summary": change.get("summary", "")})
        self.runtime.bus.publish("config", {"summary": change.get("summary", "")})

    # ---- operator actions ------------------------------------------------------

    def restart_camera(self, camera_id: str, actor: str) -> None:
        with self._config_lock:
            if camera_id not in self.supervisor.entries:
                raise KeyError(camera_id)
            self.supervisor.restart(camera_id, f"restarted by {actor}")
        self.runtime.store.audit(actor, "camera_restarted", {"camera_id": camera_id})

    def create_backup(self, *, label: str = "manual", actor: str = "system", keep: int | None = None) -> dict[str, Any]:
        settings = self.config.get("storage", {}).get("backups", {})
        with self._backup_lock:
            info = backup.create_backup(
                self.storage_dir, config=self.site.config, store=self.runtime.store, label=label,
                include_credentials=bool(settings.get("include_credentials", False)), keep=keep,
            )
        self.last_backup, self.last_backup_error = info, ""
        self.runtime.store.audit(actor, "backup_created", {"name": info["name"], "label": label})
        return info

    def restore_backup(self, name: str, parts: list[str], actor: str) -> dict[str, Any]:
        """Live restore: the site config and settings apply immediately (validated,
        like any edit); the event database is staged and swapped in at the next
        start, because camera threads are writing to it."""
        path = backup.resolve_backup(self.storage_dir, name)
        check = backup.verify_backup(path)
        if not check["ok"]:
            raise backup.BackupError("backup failed verification: " + "; ".join(check["problems"]))
        safety = self.create_backup(label="pre-restore", actor=actor)
        result: dict[str, Any] = {"backup": name, "safety_backup": safety["name"], "applied": [], "staged": []}
        import zipfile

        with zipfile.ZipFile(path) as archive:
            files = set(check["manifest"]["files"])
            if "config" in parts and "site.json" in files:
                self.site.replace(json.loads(archive.read("site.json")), actor, f"restored from backup {name}")
                result["applied"].append("config")
            if "settings" in parts and "settings.json" in files:
                settings = json.loads(archive.read("settings.json"))
                self.runtime.update_settings({key: settings[key] for key in ("arm_mode", "sensitivity", "notifications_enabled") if key in settings}, actor)
                result["applied"].append("settings")
        if "database" in parts:
            backup.stage_database_restore(path, self.storage_dir)
            result["staged"].append("database")
        self.runtime.store.audit(actor, "backup_restored", result)
        return result

    def health_report(self) -> dict[str, Any]:
        runtime = self.runtime
        with runtime._lock:
            cameras_view = []
            for camera_id, status in runtime.camera_status.items():
                camera = runtime.camera_configs.get(camera_id, {})
                cameras_view.append({
                    "camera_id": camera_id, "name": camera.get("name", camera_id), "health": dict(status.get("health", {})),
                    **{key: status.get(key) for key in (
                        "connected", "fps", "target_fps", "last_frame_ms", "error", "error_kind", "reconnects", "stream", "resolution",
                        "hw_decode_active", "detector_state", "detector_provider", "detector_errors", "detector_error", "worker_state",
                        "worker_error", "restarts_total", "restarts_recent", "next_restart_ms", "metrics",
                    )},
                })
            coverage = runtime.health.coverage(runtime.camera_status)
        return {
            "time_ms": int(time.time() * 1000),
            "coverage": coverage,
            "cameras": cameras_view,
            "resources": self.resources.latest,
            "storage": runtime.storage_status | {"media_mb": self._media_mb},
            "database": {"ok": runtime.store.last_write_ok, "write_failures": runtime.store.write_failures, "last_error": runtime.store.last_error,
                         "recovery": self.db_recovery},
            "backups": {"last": self.last_backup, "last_error": self.last_backup_error, "count": len(backup.list_backups(self.storage_dir))},
            "service": {"started_ms": runtime.started_ms, "pid": self.runstate.state.get("pid"), "stuck_threads": len(self.supervisor.abandoned)},
            "notifications": {"enabled": runtime.settings.get("notifications_enabled", True), "recent": runtime.dispatcher.sent[-5:]},
        }

    # ---- internals ---------------------------------------------------------------

    _media_mb: float | None = None

    def _record_startup(self, previous: dict[str, Any] | None) -> None:
        runtime = self.runtime
        now_ms = int(time.time() * 1000)
        gap = downtime(previous, now_ms)
        if gap and gap["ended_ms"] > gap["started_ms"]:
            if gap["clean"]:
                runtime.health.record_past_outage("service", "service_stopped", gap["started_ms"], gap["ended_ms"], "Sentinel was stopped", notify=False)
            else:
                runtime.health.record_past_outage(
                    "service", "service_crash", gap["started_ms"], gap["ended_ms"],
                    "Sentinel stopped unexpectedly (crash, power cut, or forced shutdown)", notify=True,
                )
        if self.db_recovery:
            report = self.db_recovery
            runtime.store.audit("system", "database_recovered", report)
            if report["action"] == "restored_from_backup":
                summary = f"Event database was damaged and was restored from the backup of {time.strftime('%Y-%m-%d %H:%M', time.localtime(report['backup_created_ms'] / 1000))}; history recorded after it is lost"
            elif report["action"] == "started_empty":
                summary = "Event database was damaged and no backup was usable; a new, empty history was started"
            else:
                summary = f"Event database restored from {report.get('backup', 'a backup')} as requested"
            runtime._health_event("", "system_health", "medium" if report["action"] != "staged_restore" else "info", summary, now_ms)
            if report["action"] != "staged_restore":
                runtime.health.announce("Sentinel repaired its event database", [summary + ".", "Monitoring is running."], "medium", now_ms, "database", ())
        if self.config_recovery:
            report = self.config_recovery
            summary = ("Site configuration was damaged; the last good version was restored" if report["restored_from"]
                       else "Site configuration was damaged and no saved version was usable; the start configuration is in use")
            runtime.store.audit("system", "config_recovered", report)
            runtime._health_event("", "system_health", "medium", summary, now_ms)
            runtime.health.announce("Sentinel repaired its configuration", [summary + ". Check the Setup tab."], "medium", now_ms, "config", ())

    def _fatal(self, reason: str) -> None:
        log.critical("fatal: %s", reason)
        now_ms = int(time.time() * 1000)
        with self.runtime._lock:
            self.runtime.health.set_component("process", "failed", reason, now_ms)
        if self.on_fatal is not None:
            self.on_fatal(reason)

    def _worker_restarted(self, camera_id: str, reason: str, manual: bool) -> None:
        self.runtime.store.audit("system" if not manual else "operator", "camera_worker_restarted",
                                 {"camera_id": camera_id, "reason": reason[:200], "manual": manual})

    def _housekeeping_loop(self) -> None:
        last: dict[str, float] = {}
        storage_settings = lambda: self.storage_guard.settings  # noqa: E731 - re-read after config edits

        def due(name: str, every_s: float, first_after_s: float = 0.0) -> bool:
            now = time.monotonic()
            if name not in last:
                last[name] = now - every_s + first_after_s
            if now - last[name] >= every_s:
                last[name] = now
                return True
            return False

        while not self._stop.wait(1.0):
            for name, task, every_s, first_after_s in (
                ("supervise", self._supervise, 1.0, 0.0),
                ("runtime", self.runtime.housekeeping, 1.0, 0.0),
                ("runstate", self.runstate.beat, 10.0, 0.0),
                ("storage", self._check_storage, storage_settings().check_interval_s, 0.0),
                ("resources", self.resources.sample, 15.0, 0.0),
                ("backup", self._scheduled_backup, 600.0, 60.0),
                ("heartbeat", self._external_heartbeat, float(self.config.get("health", {}).get("heartbeat", {}).get("interval_s", 300)), 60.0),
            ):
                if due(name, every_s, first_after_s):
                    try:
                        task()
                    except Exception:
                        log.exception("housekeeping task %s failed", name)

    def _supervise(self) -> None:
        with self._config_lock:
            self.supervisor.check()

    def _check_storage(self) -> None:
        status = self.storage_guard.check()
        self._media_mb = directory_size_mb(self.runtime.media_root)
        self.runtime.apply_storage_status(status)

    def _scheduled_backup(self) -> None:
        settings = self.config.get("storage", {}).get("backups", {})
        if not settings.get("enabled", True) or self.storage_guard.status.get("recording_paused"):
            return
        interval_ms = float(settings.get("interval_h", 24)) * 3_600_000
        automatic = [item for item in backup.list_backups(self.storage_dir) if item.get("label") == "auto" and item.get("valid", True)]
        if automatic and time.time() * 1000 - automatic[0]["created_ms"] < interval_ms:
            return

        def run() -> None:
            try:
                self.create_backup(label="auto", keep=int(settings.get("keep", 7)))
            except Exception as error:
                self.last_backup_error = f"{type(error).__name__}: {error}"[:200]
                log.exception("scheduled backup failed")

        threading.Thread(target=run, name="sentinel-backup", daemon=True).start()

    def _external_heartbeat(self) -> None:
        """Optional dead-man's switch (healthchecks.io style): a hosted watchdog
        alerts people when these pings stop, which covers a dead computer or a
        power cut, the cases Sentinel cannot report itself. Pings carry no data."""
        settings = self.config.get("health", {}).get("heartbeat", {})
        coverage = self.runtime.health.coverage(self.runtime.camera_status)["state"]
        url = settings.get("url") if coverage in ("full", "not_running") else settings.get("fail_url") or None
        if not url:
            return

        def ping() -> None:
            try:
                with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=10) as response:
                    response.read(64)
            except Exception as error:
                log.warning("heartbeat ping failed: %s", type(error).__name__)

        threading.Thread(target=ping, name="sentinel-heartbeat", daemon=True).start()

    def _drone_loop(self) -> None:
        last = time.monotonic()
        published = 0.0
        while not self._stop.wait(0.5):
            now = time.monotonic()
            try:
                self.drone.step(now - last)
                if now - published >= 2.0:
                    published = now
                    self.runtime.bus.publish("drone", self.drone.status())
            except Exception:
                log.exception("drone safety loop failed")
            last = now
