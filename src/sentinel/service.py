from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from .config import cameras, resolve_env
from .drone import build_drone_manager
from .pipeline import CameraWorker
from .runtime import SentinelRuntime
from .site_config import SiteConfigManager

log = logging.getLogger("sentinel.service")


class SentinelService:
    """Process-level assembly: runtime + camera workers + housekeeping + drone
    safety loop + optional MQTT mirror. The API holds a reference to this."""

    def __init__(self, config: dict[str, Any], *, start_cameras: bool = True) -> None:
        """`config` may still contain ${ENV} placeholders; they are resolved for
        the running pipeline but never written back by the setup editor."""
        storage_dir = Path(config.get("storage", {}).get("dir", "data/sentinel"))
        raw = SiteConfigManager.load_effective(config, storage_dir)
        config = resolve_env(raw)
        self.config = config
        self.start_cameras = start_cameras
        self.mqtt = None
        if config.get("mqtt", {}).get("enabled", False):
            from .mqtt import MqttPublisher

            self.mqtt = MqttPublisher(config["mqtt"], config["site"]["id"])
        self.runtime = SentinelRuntime(config, mqtt_publish=self.mqtt.publish if self.mqtt else None)
        if self.mqtt:
            self.runtime.bus.subscribe(self.mqtt.on_bus)
        self.runtime.drone = build_drone_manager(
            config,
            audit=self.runtime.store.audit,
            on_change=lambda status: self.runtime.bus.publish("drone", status),
        )
        self.site = SiteConfigManager(raw, storage_dir, self.runtime.secrets)
        self.site.subscribe(self.apply_config)
        self.workers: dict[str, CameraWorker] = (
            {camera["id"]: CameraWorker(camera, self.runtime) for camera in cameras(config)} if start_cameras else {}
        )
        self._started = False
        self._config_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    @property
    def drone(self):
        return self.runtime.drone

    def start(self) -> None:
        self._started = True
        for worker in self.workers.values():
            worker.start()
        self._threads = [threading.Thread(target=self._housekeeping_loop, name="sentinel-housekeeping", daemon=True)]
        if self.drone is not None:
            self._threads.append(threading.Thread(target=self._drone_loop, name="sentinel-drone", daemon=True))
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        for worker in self.workers.values():
            worker.stop()
        for worker in self.workers.values():
            worker.join(timeout=5)
        for thread in self._threads:
            thread.join(timeout=5)
        if self.mqtt:
            self.mqtt.close()
        self.runtime.close()

    def apply_config(self, raw: dict[str, Any]) -> None:
        """Called by the setup editor after a validated save: update rules and
        zones in place and restart only the cameras whose settings changed."""
        change = raw.pop("_change", {})
        config = resolve_env(raw)
        with self._config_lock:
            previous = {camera["id"]: json.dumps(camera, sort_keys=True) for camera in cameras(self.config)}
            self.config = config
            self.runtime.reconfigure(config)
            if self.start_cameras:
                current = {camera["id"]: camera for camera in cameras(config)}
                for camera_id in list(self.workers):
                    if camera_id not in current or json.dumps(current[camera_id], sort_keys=True) != previous.get(camera_id):
                        worker = self.workers.pop(camera_id)
                        worker.stop()
                        if worker.is_alive():
                            worker.join(timeout=5)
                for camera_id, camera in current.items():
                    if camera_id not in self.workers:
                        self.workers[camera_id] = CameraWorker(camera, self.runtime)
                        if self._started:
                            self.workers[camera_id].start()
        self.runtime.store.audit(change.get("actor", "system"), "config_changed", {"summary": change.get("summary", "")})
        self.runtime.bus.publish("config", {"summary": change.get("summary", "")})

    def _housekeeping_loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.runtime.housekeeping()
            except Exception:
                log.exception("housekeeping failed")

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
