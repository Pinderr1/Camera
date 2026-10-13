from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .config import cameras
from .drone import build_drone_manager
from .pipeline import CameraWorker
from .runtime import SentinelRuntime

log = logging.getLogger("sentinel.service")


class SentinelService:
    """Process-level assembly: runtime + camera workers + housekeeping + drone
    safety loop + optional MQTT mirror. The API holds a reference to this."""

    def __init__(self, config: dict[str, Any], *, start_cameras: bool = True) -> None:
        self.config = config
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
        self.workers = [CameraWorker(camera, self.runtime) for camera in cameras(config)] if start_cameras else []
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    @property
    def drone(self):
        return self.runtime.drone

    def start(self) -> None:
        for worker in self.workers:
            worker.start()
        self._threads = [threading.Thread(target=self._housekeeping_loop, name="sentinel-housekeeping", daemon=True)]
        if self.drone is not None:
            self._threads.append(threading.Thread(target=self._drone_loop, name="sentinel-drone", daemon=True))
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        for worker in self.workers:
            worker.stop()
        for worker in self.workers:
            worker.join(timeout=5)
        for thread in self._threads:
            thread.join(timeout=5)
        if self.mqtt:
            self.mqtt.close()
        self.runtime.close()

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
