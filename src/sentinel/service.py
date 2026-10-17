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
from .devices import SimulatedSensors
from .drone import build_drone_fleet
from .drone.pairing import CompanionRegistry
from .installation import InstallationManager
from .netdiag import SystemNetwork
from .pipeline import CameraWorker
from .resources import ResourceSampler, directory_size_mb
from .runtime import SentinelRuntime
from .site_config import SiteConfigManager
from .storage_guard import StorageGuard, StorageSettings
from .supervisor import SupervisorSettings, WorkerSupervisor

log = logging.getLogger("sentinel.service")


def _when(ms: int | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000)) if ms else "unknown"


def _age(seconds: float) -> str:
    return f"{seconds / 3600:.1f} h" if seconds >= 5400 else f"{round(seconds / 60)} min"


def database_recovery_lines(report: dict[str, Any]) -> list[str]:
    """Plain statements for the owner: what happened, which backup, how old it
    was, which period may be missing, and where the original is kept."""
    action = report["action"]
    lines = []
    if action == "restored_from_backup":
        lines.append(f"The event database was damaged. It was restored from the backup made {_when(report['backup_created_ms'])} "
                     f"({_age(report['backup_age_s'])} old)")
        loss = report.get("possible_loss") or {}
        lines.append(f"Events, incidents and outages recorded between {_when(loss.get('from_ms'))} and {_when(loss.get('to_ms'))} may be missing")
    elif action == "salvaged_only":
        lines.append("The event database was damaged and no usable backup existed; readable records were copied into a new database")
    elif action == "started_empty":
        lines.append("The event database was damaged, no usable backup existed and nothing could be salvaged; a new, empty history was started")
    else:
        lines.append(f"Event history was replaced by the backup {report.get('backup', '')} made {_when(report.get('backup_created_ms'))}, as requested")
    merged = {table: count for table, count in (report.get("merged") or {}).items() if count}
    if merged:
        lines.append("Recovered from the damaged file: " + ", ".join(f"{count} {table}" for table, count in sorted(merged.items())))
    if report.get("quarantined"):
        lines.append(f"The original file is preserved unchanged in recovery/{report['quarantined'][0]['file']}")
    return lines


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
        self.drones: dict[str, Any] = {}
        self.drone_video: dict[str, Any] = {}
        self.primary_drone = ""
        self._drone_key = ""
        self._drone_rebuild_pending: dict[str, Any] | None = None
        self._drone_lock = threading.RLock()
        self.companions = CompanionRegistry(self.runtime.secrets, load=self.runtime.store.list_drone_companions,
                                            save=self.runtime.store.save_drone_companion, audit=self.runtime.store.audit)
        self._build_drones(config)
        self.sensors = SimulatedSensors()
        self.sensors.sync(config, int(time.time() * 1000))
        self.installation = InstallationManager(self.storage_dir)
        self.site = SiteConfigManager(raw, self.storage_dir, self.runtime.secrets)
        self.network = SystemNetwork()  # onboarding diagnostics; tests and demos swap in a SimulatedNetwork
        self.site.subscribe(self.apply_config)
        from .flame import FlameService

        self.flame = FlameService(self)  # agricultural configuration of the drone (Flame Mode)
        from .lab.session import LabManager
        from .training.registry import lab_profiles

        self.lab = LabManager(self.storage_dir, extra_profiles=lambda: lab_profiles(self.storage_dir))  # AI Test Lab (recorded clips only)
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
        # The drone loop always runs: drones and sensors can be registered while Sentinel is running.
        self._threads = [threading.Thread(target=self._housekeeping_loop, name="sentinel-housekeeping", daemon=True),
                         threading.Thread(target=self._drone_loop, name="sentinel-drone", daemon=True)]
        for thread in self._threads:
            thread.start()
        self.flame.start()
        for worker in self.drone_video.values():
            if not worker.is_alive():
                worker.start()

    def stop(self) -> None:
        """Graceful: stop cameras (clips are finalised), close open outages,
        flush the database, and mark the run as cleanly stopped."""
        self._stop.set()
        self.flame.stop()
        self.lab.stop()
        self.supervisor.stop()
        for thread in self._threads:
            thread.join(timeout=5)
        self._stop_drone_video()
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
            self._build_drones(config)
            self.sensors.sync(config, int(time.time() * 1000))
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

    # ---- drones ----------------------------------------------------------------

    @staticmethod
    def _drone_config_key(config: dict[str, Any]) -> str:
        devices = [d for d in config.get("devices", []) if d.get("category") == "drone"]
        return json.dumps([config.get("drone", {}), devices, config.get("site", {}).get("map", {}).get("origin_geo")], sort_keys=True)

    def _build_drones(self, config: dict[str, Any]) -> None:
        """(Re)build drone managers when drone settings change. Never while an
        aircraft is flying or on a mission: the rebuild waits until all are down."""
        key = self._drone_config_key(config)
        with self._drone_lock:
            if key == self._drone_key:
                self._drone_rebuild_pending = None
                return
            if any(manager.adapter.telemetry().in_air or manager._mission for manager in self.drones.values()):
                self._drone_rebuild_pending = config
                log.warning("drone settings changed during a flight; they apply after landing")
                return
            self._stop_drone_video()
            for manager in self.drones.values():
                try:
                    manager.adapter.disconnect()
                except Exception:
                    log.exception("drone adapter disconnect failed")
            self.primary_drone, self.drones = build_drone_fleet(
                config, audit=self.runtime.store.audit, on_change=lambda status: self.runtime.bus.publish("drone", status),
                records=self.runtime.store,
            )
            self.runtime.drone = self.drones.get(self.primary_drone)
            for drone_id, manager in self.drones.items():
                self._wire_drone(drone_id, manager, config)
            self._drone_key = key
            self._drone_rebuild_pending = None
            if getattr(self, "flame", None) is not None:
                self.flame.apply_hooks()  # rebuilt managers get the configuration guard before any request arrives

    def _wire_drone(self, drone_id: str, manager: Any, config: dict[str, Any]) -> None:
        """Companion status, drone video, and inspection results for one aircraft."""
        def companion() -> dict[str, Any] | None:
            paired = self.companions.for_drone(drone_id)
            return paired.public(int(time.time() * 1000)) if paired else None

        manager.companion_status = companion
        manager.on_inspection_finished = lambda request: self.runtime.bus.publish("drone_inspection", request)
        manager.people_areas = self.runtime.person_map_areas  # person standoff: people the fixed cameras see now
        from .drone import catalog

        device = next((d for d in config.get("devices", []) if d.get("id") == drone_id), None)
        site_video = config.get("drone", {}).get("video")
        aircraft = dict(device) if device else dict(config.get("drone", {}))
        if not aircraft.get("video") and drone_id == self.primary_drone and site_video:
            aircraft["video"] = site_video  # legacy: the drone section's video belongs to the primary drone
        settings = catalog.video_settings(manager.model, aircraft, simulated=not manager.adapter.is_physical, site_video=site_video)
        if not settings:
            return
        from .drone.video import DroneVideoWorker

        worker = DroneVideoWorker(manager, settings, config=config, media_root=self.runtime.media_root,
                                  record_evidence=self.runtime.store.record_evidence, secrets=self.runtime.secrets.get)
        manager.video_status = worker.status
        self.drone_video[drone_id] = worker
        if getattr(self, "_started", False):
            worker.start()

    def _stop_drone_video(self) -> None:
        for worker in self.drone_video.values():
            worker.stop()
        for worker in self.drone_video.values():
            if worker.is_alive():
                worker.join(timeout=3)
        self.drone_video = {}

    def drone_manager(self, drone_id: str | None = None) -> Any:
        with self._drone_lock:
            return self.drones.get(drone_id or self.primary_drone)

    # ---- devices and installation ------------------------------------------------

    def devices_view(self) -> list[dict[str, Any]]:
        from .devices import camera_entry, device_entry, hub_entry

        now = int(time.time() * 1000)
        config = self.site.config
        with self.runtime._lock:
            statuses = {camera_id: dict(status) for camera_id, status in self.runtime.camera_status.items()}
        entries = [hub_entry(self.resources.latest)]
        running = self.start_cameras and self._started
        cameras_view = [camera_entry(camera, statuses.get(camera["id"]), now, running=running) for camera in config.get("cameras", [])]
        # A recorder's channels: cameras linked to it explicitly, or streaming from its address.
        recorder_hosts = {device["host"]: device["id"] for device in config.get("devices", []) if device.get("category") == "recorder" and device.get("host")}
        by_recorder: dict[str, list[dict[str, Any]]] = {}
        for camera, entry in zip(config.get("cameras", []), cameras_view):
            entry["recorder"] = entry["recorder"] or recorder_hosts.get(camera.get("source", {}).get("host", ""), "")
            if entry["recorder"]:
                by_recorder.setdefault(entry["recorder"], []).append(entry)
        entries += cameras_view
        with self._drone_lock:
            drone_status = {drone_id: manager.status() | {"primary": drone_id == self.primary_drone} for drone_id, manager in self.drones.items()}
        registered = {device["id"] for device in config.get("devices", [])}
        for drone_id, status in drone_status.items():
            if drone_id not in registered:  # legacy single-drone configuration
                legacy = {"id": drone_id, "category": "drone", "name": "Drone (drone settings)", "simulated": status["adapter"] == "simulated",
                          "profile": "simulated_drone" if status["adapter"] == "simulated" else "mavlink_drone"}
                entries.append(device_entry(legacy, status, by_recorder) | {"removable": False})
        for device in config.get("devices", []):
            if not device.get("enabled", True):
                continue
            live = None
            if device["category"] == "drone":
                live = drone_status.get(device["id"])
            elif device["category"] == "dock":
                drone_id = device.get("drone_id") or self.primary_drone
                if device.get("simulated") and drone_id in drone_status:
                    live = {"drone_telemetry": drone_status[drone_id]["telemetry"]}
            elif device["category"] == "accessory":
                view = self.flame.accessory_view(device, now)
                live = view | {"configuration": self.flame.configuration(device.get("drone_id", ""))} if view["state"] != "not_connected" else None
            elif device.get("simulated"):
                live = self.sensors.live(device["id"], now)
            entries.append(device_entry(device, live, by_recorder))
        return entries

    def coverage_analysis(self, plan: dict[str, Any] | None = None) -> dict[str, Any]:
        from .planner import analyse_coverage

        config = self.site.config
        site_map = config.get("site", {}).get("map", {})
        plan = plan if plan is not None else self.installation.state["plan"]
        return analyse_coverage(plan, float(site_map.get("width_m", 40)), float(site_map.get("height_m", 30)), config.get("cameras", []))

    def installation_view(self) -> dict[str, Any]:
        from .installation import PATH_LABELS, STEPS, readiness
        from .planner import PROPERTY_TYPES

        state = self.installation.state
        config = self.site.config
        devices = self.devices_view()
        analysis = self.coverage_analysis() if state["plan"].get("items") else None
        site_map = config.get("site", {}).get("map", {})
        drone = config.get("drone", {})
        return {
            "state": state, "steps": [{"id": step, "label": label} for step, label in STEPS], "paths": PATH_LABELS, "property_types": PROPERTY_TYPES,
            "site": {"id": config["site"]["id"], "name": config["site"].get("name", ""), "width_m": site_map.get("width_m"),
                     "height_m": site_map.get("height_m"), "origin_geo": site_map.get("origin_geo"), "has_image": bool(site_map.get("image")),
                     "buildings": site_map.get("buildings", [])},
            "devices": devices,
            "readiness": readiness(state=state, config=config, devices=devices, health=self.health_report(), coverage_analysis=analysis),
            "drone": {"enabled": bool(drone.get("enabled")), "primary": self.primary_drone, "inspection_points": drone.get("inspection_points", []),
                      "geofence": drone.get("geofence"), "home_map": drone.get("home_map")},
            "notifications": self.notification_view(),
            "processing": bool(self.start_cameras and self._started),
            "recovered": self.installation.recovered,
        }

    def notification_view(self) -> dict[str, Any]:
        config = self.site.config
        channels = config.get("alerts", {}).get("channels", [])
        ntfy = next((c for c in channels if c.get("type") == "ntfy"), {})
        mqtt = next((c for c in channels if c.get("type") == "mqtt"), {})
        topic_set = bool(ntfy.get("topic")) or bool(ntfy.get("topic_credential") and self.runtime.secrets.get(ntfy["topic_credential"]))
        return {
            "ntfy": {"enabled": bool(ntfy) and ntfy.get("enabled", True), "server": ntfy.get("server", "https://ntfy.sh"), "topic_set": topic_set,
                     "attach_snapshot": bool(ntfy.get("attach_snapshot"))},
            "mqtt": {"enabled": bool(mqtt) and mqtt.get("enabled", True), "broker_configured": bool(config.get("mqtt", {}).get("enabled"))},
            "thresholds": config.get("alerts", {}).get("notify_min_severity", {}),
            "last_test": self.installation.state.get("notification_test"),
        }

    def send_test_notification(self, actor: str, wait_s: float = 12.0) -> dict[str, Any]:
        """An operator-requested test push through every enabled channel. It
        bypasses arm mode and rate limits on purpose and says it is a test."""
        from .incidents import DISCLAIMER
        from .models import Notification

        now = int(time.time() * 1000)
        reference = f"test_{now}"
        message = f"This is a test requested from Sentinel's installation wizard by {actor[:40]}. No activity was observed.\n{DISCLAIMER}"
        self.runtime.dispatcher.submit(Notification(reference, "low", "Sentinel test notification", message, 3, (), now, kind="system"))
        deadline = time.time() + wait_s
        outcome = None
        while outcome is None and time.time() < deadline:
            outcome = next((sent for sent in reversed(self.runtime.dispatcher.sent) if sent["incident_id"] == reference), None)
            if outcome is None:
                time.sleep(0.1)
        result = {"ms": now, "by": actor[:64], "outcomes": outcome["channels"] if outcome else {"dispatcher": "timed out"}}
        self.installation.record_notification_test(result)
        self.runtime.store.audit(actor, "test_notification", {"outcomes": result["outcomes"]})
        return result

    def test_sensor(self, device_id: str, actor: str) -> dict[str, Any]:
        """A simulated sensor's test press, logged as an information event. Sensors never open incidents in this build."""
        from .models import SecurityEvent

        device = next((d for d in self.site.config.get("devices", []) if d["id"] == device_id), None)
        if device is None:
            raise KeyError(device_id)
        if device_id not in self.sensors.state:
            raise ValueError("only simulated sensors can be tested in this build")
        now = int(time.time() * 1000)
        self.sensors.trigger(device_id, now)
        event = SecurityEvent(f"evt_sensor_{now}_{device_id}", "sensor_test", "info", "", now, now, 1.0, f"{device['name']}: test signal received",
                              zone_id=device.get("zone_id", ""), reason_codes=["sensor_test"], details={"device_id": device_id, "simulated": True})
        self.runtime.store.add_event(event)
        self.runtime.bus.publish("event", event.as_dict())
        self.runtime.store.audit(actor, "sensor_tested", {"device_id": device_id})
        return event.as_dict()

    def remove_device(self, device_id: str, actor: str) -> dict[str, Any]:
        """Removes a camera or device from the configuration (credentials are
        deleted with it) and unlinks it from the installation plan."""
        if any(camera["id"] == device_id for camera in self.site.config.get("cameras", [])):
            result = self.site.delete_camera(device_id, actor)
        else:
            result = self.site.delete_device(device_id, actor)
        result["plan_items_unlinked"] = self.installation.unlink_device(device_id)
        return result

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
                if "installation.json" in files:
                    target = self.storage_dir / "installation.json"
                    temporary = target.with_suffix(".restoring")
                    temporary.write_bytes(archive.read("installation.json"))
                    temporary.replace(target)
                    self.installation = InstallationManager(self.storage_dir)
                    result["applied"].append("installation")
                if "flame.json" in files:
                    target = self.storage_dir / "flame.json"
                    temporary = target.with_suffix(".restoring")
                    temporary.write_bytes(archive.read("flame.json"))
                    temporary.replace(target)
                    self.flame.doc = self.flame._load()
                    result["applied"].append("flame")
            if "settings" in parts and "settings.json" in files:
                settings = json.loads(archive.read("settings.json"))
                self.runtime.update_settings({key: settings[key] for key in ("arm_mode", "sensitivity", "notifications_enabled") if key in settings}, actor)
                result["applied"].append("settings")
        if "database" in parts:
            backup.stage_database_restore(path, self.storage_dir)
            result["staged"].append("database")
        self.runtime.store.audit(actor, "backup_restored", result)
        return result

    # ---- rules, explanations, insights, reports -------------------------------------

    def _zone_catalog(self) -> list[dict[str, Any]]:
        return [{"id": zone["id"], "name": zone.get("name", zone["id"]), "type": zone["type"], "camera_id": camera["id"],
                 "camera_name": camera.get("name", camera["id"])}
                for camera in self.config.get("cameras", []) for zone in camera.get("zones", []) if zone["type"] != "ignore"]

    def rules_view(self) -> dict[str, Any]:
        from .config import ARM_MODES, CATEGORIES, DAYS, RULE_TYPES
        from .models import SEVERITIES
        from .rule_lab import scenario_names
        from .rules import TYPE_LABELS, describe_rule, templates

        zone_names = {zone["id"]: zone["name"] for zone in self._zone_catalog()}
        rules = self.site.config.get("rules", [])
        rule_names = {rule["id"]: rule.get("name") or rule["id"].replace("_", " ") for rule in rules}
        return {
            "rules": [rule | {"display_name": rule_names[rule["id"]], "plain": describe_rule(rule, zone_names, rule_names)} for rule in rules],
            "templates": templates(),
            "catalog": {
                "types": {kind: TYPE_LABELS[kind] for kind in sorted(RULE_TYPES)}, "classes": list(CATEGORIES), "modes": [m for m in ARM_MODES if m != "disarmed"],
                "days": list(DAYS), "severities": list(SEVERITIES), "zones": self._zone_catalog(),
                "zone_types": sorted({zone["type"] for zone in self._zone_catalog()}), "scenarios": scenario_names(),
                "notify_thresholds": self.runtime.policy.config.notify_min_severity,
            },
        }

    def check_rule(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .rule_lab import config_with_rule
        from .rules import clean_rule, describe_rule

        rule = clean_rule(payload) | {"id": payload.get("id") or "draft_rule"}
        rule.setdefault("enabled", True)
        zone_names = {zone["id"]: zone["name"] for zone in self._zone_catalog()}
        rule_names = {item["id"]: item.get("name") or item["id"] for item in self.site.config.get("rules", [])}
        try:
            config_with_rule(self.site.config, rule)
            errors: list[str] = []
        except ValueError as error:
            errors = [line.strip("- ").strip() for line in str(error).splitlines()[1:]] or [str(error)]
        try:
            plain = describe_rule(rule, zone_names, rule_names)
        except (KeyError, TypeError, ValueError):
            plain = None
        return {"valid": not errors, "errors": errors, "plain": plain, "rule": rule}

    def test_rule(self, body: dict[str, Any]) -> dict[str, Any]:
        from datetime import datetime

        from .rule_lab import test_rule
        from .rules import clean_rule

        rule = clean_rule(body["rule"]) | {"id": body["rule"].get("id") or "draft_rule"}
        start = datetime.fromisoformat(body["start"]) if body.get("start") else None
        visits = None
        if body["source"] == "history":
            now = int(time.time() * 1000)
            visits = self.runtime.store.list_visits(since_ms=now - int(body.get("days", 7) * 86_400_000), until_ms=now)
        config = self.site.config | {"_config_dir": self.config.get("_config_dir", ".")}
        return test_rule(config, rule, source=body["source"], arm_mode=body.get("arm_mode", "away"), start=start,
                         steps=body.get("steps"), label=body.get("label", "person"), scenario=body.get("scenario"), visits=visits)

    def explain(self, incident_id: str) -> dict[str, Any]:
        from .explain import explain_incident

        incident = self.runtime.store.get_incident(incident_id)
        if incident is None:
            raise KeyError(incident_id)
        events = self.runtime.store.list_events(incident_id=incident_id, limit=1000)
        known = {event["event_id"] for event in events}
        subjects = set(incident.get("site_track_ids", []))
        # The same subject's earlier observations (e.g. first sighting on another camera) belong to the story too.
        events += [event for event in self.runtime.store.list_events(since_ms=incident["opened_ms"] - 600_000, until_ms=incident["updated_ms"], limit=1000)
                   if event["site_track_id"] in subjects and event["event_id"] not in known and not event.get("incident_id")]
        explanation = explain_incident(incident, events, self.config, notify_thresholds=self.runtime.policy.config.notify_min_severity)
        inspections = self.runtime.store.list_drone_inspections(incident_id=incident_id, limit=20)
        if inspections:
            # Drone observations are reported beside, never merged into, what the fixed cameras saw.
            explanation["drone_inspections"] = [{
                key: inspection.get(key) for key in ("request_id", "drone_id", "status", "target_label", "reason", "requested_by", "decided_by",
                                                     "decided_ms", "started_ms", "reached_target_ms", "ended_ms", "mode", "note")
            } | {"observations": {key: inspection.get("observations", {}).get(key) for key in ("text", "max_visible", "frames", "tracks", "observations", "pose_sources")}}
                for inspection in inspections]
            explanation["evidence"] += [
                {"type": "drone_snapshot", "path": row["snapshot_path"], "camera": "Drone camera", "time_ms": row["first_ms"], "request_id": inspection["request_id"]}
                for inspection in inspections for row in inspection.get("observations", {}).get("observations", []) if row.get("snapshot_path")
            ]
        return explanation

    def export_incident(self, incident_id: str, actor: str, *, include_media: bool) -> bytes:
        from .reports import export_incident

        incident = self.runtime.store.get_incident(incident_id)
        if incident is None:
            raise KeyError(incident_id)
        events = self.runtime.store.list_events(incident_id=incident_id, limit=1000)
        media = list(incident.get("clip_paths", [])) + [event["snapshot_path"] for event in events if event.get("snapshot_path")]
        data = export_incident(incident, events, self.explain(incident_id), media_root=self.runtime.media_root,
                               recorded_hashes=self.runtime.store.evidence_hashes(media), site_name=self.config["site"].get("name", self.config["site"]["id"]),
                               actor=actor, include_media=include_media)
        self.runtime.store.audit(actor, "incident_exported", {"incident_id": incident_id, "include_media": include_media, "bytes": len(data)})
        return data

    def insights(self, days: float | None = None) -> dict[str, Any]:
        from .insights import ActivitySettings, compute_insights
        from .reports import incident_rules

        settings = ActivitySettings.from_dict(self.config.get("activity"))
        if days:
            settings.window_days = max(1.0, min(float(days), settings.retention_days))
        now = int(time.time() * 1000)
        since = now - int(settings.window_days * 86_400_000)
        store = self.runtime.store
        links = incident_rules(store, since)
        reviewed = [incident | {"rule_ids": sorted(links.get(incident["incident_id"], {}).get("rule_ids", []))}
                    for incident in store.search_incidents(since_ms=since, limit=1000) if incident.get("resolution")]
        scene = [event for event in store.list_events(since_ms=since, limit=1000) if event["event_type"] == "scene_condition"]
        return compute_insights(store.list_visits(since_ms=since), store.list_trips(since_ms=since), config=self.config, settings=settings,
                                now_ms=now, scene_events=scene, reviewed_incidents=reviewed)

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
                         "recovery": self.db_recovery, "recoveries": backup.list_recoveries(self.storage_dir, limit=5)},
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
            lines = database_recovery_lines(report)
            staged = report["action"] == "staged_restore"
            runtime._health_event("", "system_health", "info" if staged else "medium", lines[0], now_ms)
            # Every recovery is reported to the owner, including one they requested: history was replaced.
            runtime.health.announce("Event history restored from a backup" if staged else "Sentinel repaired its event database",
                                    lines + ["Monitoring is running."], "low" if staged else "medium", now_ms, "database", ())
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
        url = settings.get("url") if coverage in ("full", "not_running", "no_cameras") else settings.get("fail_url") or None
        if not url:
            return

        def ping() -> None:
            try:
                with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=10) as response:
                    response.read(64)
            except Exception as error:
                log.warning("heartbeat ping failed: %s", type(error).__name__)

        threading.Thread(target=ping, name="sentinel-heartbeat", daemon=True).start()

    def _drone_health(self, manager: Any) -> None:
        """Losing the telemetry link is a component outage; it is pushed to
        people only when it happens with the aircraft in the air."""
        if manager.adapter.kind == "simulated":
            return
        telemetry = manager.adapter.telemetry()
        now = int(time.time() * 1000)
        if telemetry.link_ok:
            self.runtime.health.set_component("drone_link", "ok", "receiving telemetry", now)
        else:
            airborne = manager.flight_state in ("link_lost",) and manager._flight is not None
            self.runtime.health.set_component("drone_link", "degraded", "no telemetry from the drone" + (" during a flight" if airborne else ""),
                                              now, notify=airborne)

    def _drone_loop(self) -> None:
        last = time.monotonic()
        published = 0.0
        while not self._stop.wait(0.5):
            now = time.monotonic()
            try:
                with self._drone_lock:
                    for manager in self.drones.values():
                        manager.step(now - last)
                    pending = self._drone_rebuild_pending
                    primary = self.runtime.drone
                if pending is not None:
                    self._build_drones(pending)
                if primary is not None and now - published >= 2.0:
                    published = now
                    self.runtime.bus.publish("drone", primary.status())
                    self._drone_health(primary)
                self.sensors.step(int(time.time() * 1000))
            except Exception:
                log.exception("drone safety loop failed")
            last = now
