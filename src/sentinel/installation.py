"""Installation journey: save-and-resume progress, the property plan, and an
honest readiness assessment.

Progress lives in `<storage>/installation.json` (path, property answers, plan,
step marks, completion). It is separate from the site config so that planning
edits never restart cameras or reset tracking.

Readiness is computed from live data every time it is asked for, never stored
as a claim. It keeps four things apart: installation steps completed, devices
connected, video available, and AI monitoring verified. Sentinel never says a
property is protected; the strongest statement it makes is that AI monitoring
is verified on N of M cameras. Standard library only."""

from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path
from typing import Any

from .planner import INSTALL_PATHS, PROPERTY_TYPES, PlanError, clean_plan

STEPS: list[tuple[str, str]] = [
    ("type", "Installation type"),
    ("property", "Property"),
    ("plan", "Plan or discover"),
    ("hardware", "Register and connect hardware"),
    ("verify", "Verify devices"),
    ("zones", "Zones and privacy"),
    ("rules", "AI monitoring and rules"),
    ("alerts", "Alerts and notifications"),
    ("health", "System health check"),
    ("finish", "Finish"),
]
STEP_IDS = [step for step, _ in STEPS]
PATH_LABELS = {
    "existing_cameras": "Connect existing cameras",
    "new_cameras": "Install new cameras",
    "drone_first": "Drone-first installation",
    "complete_package": "Complete Sentinel package",
}
PROPERTY_FIELDS = {"property_type", "notes", "indoor", "outdoor", "occupancy"}
QUESTION_FIELDS = {"property_type", "exterior_doors", "garage_doors", "driveway", "gates", "yard_sides", "street_frontages",
                   "loading_areas", "indoor_areas", "wired", "want_drone", "want_sensors"}


class InstallationError(ValueError):
    pass


def _empty() -> dict[str, Any]:
    now = int(time.time() * 1000)
    return {"version": 1, "path": "", "property": {}, "questionnaire": {}, "recommendation": None, "plan": {"items": [], "updated_ms": 0},
            "steps": {}, "current_step": "type", "started_ms": now, "updated_ms": now, "completed_ms": None, "completed_by": "",
            "completed_with_issues": [], "notification_test": None}


class InstallationManager:
    def __init__(self, storage_dir: str | Path) -> None:
        self.storage_dir = Path(storage_dir)
        self.path = self.storage_dir / "installation.json"
        self.recovered: str = ""
        self._lock = threading.RLock()
        self._state = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty()
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("version") != 1:
                raise ValueError("unknown installation file format")
            return _empty() | state
        except (OSError, ValueError) as error:
            # Keep the damaged file for inspection rather than overwriting it.
            recovery = self.storage_dir / "recovery"
            recovery.mkdir(parents=True, exist_ok=True)
            target = recovery / f"installation.{int(time.time() * 1000)}.damaged.json"
            self.path.replace(target)
            self.recovered = f"installation progress was unreadable ({str(error)[:120]}); it was moved to recovery/{target.name}"
            return _empty()

    def _save(self) -> None:
        self._state["updated_ms"] = int(time.time() * 1000)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._state, indent=1), encoding="utf-8")
        temporary.replace(self.path)

    @property
    def state(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            state = copy.deepcopy(self._state)
            if "path" in changes:
                if changes["path"] not in INSTALL_PATHS:
                    raise InstallationError(f"path must be one of {list(INSTALL_PATHS)}")
                state["path"] = changes["path"]
                state["steps"].setdefault("type", {"status": "done", "ms": int(time.time() * 1000)})
            if "property" in changes:
                info = {key: value for key, value in dict(changes["property"]).items() if key in PROPERTY_FIELDS}
                if info.get("property_type") and info["property_type"] not in PROPERTY_TYPES:
                    raise InstallationError("unknown property type")
                info["notes"] = str(info.get("notes", ""))[:500]
                state["property"] = state["property"] | info
            if "current_step" in changes:
                if changes["current_step"] not in STEP_IDS:
                    raise InstallationError("unknown step")
                state["current_step"] = changes["current_step"]
            for step, status in (changes.get("steps") or {}).items():
                if step not in STEP_IDS or status not in ("done", "skipped", "pending"):
                    raise InstallationError(f"invalid step mark {step}={status}")
                if step == "finish":
                    raise InstallationError("use complete to finish the installation")
                if status == "pending":
                    state["steps"].pop(step, None)
                else:
                    state["steps"][step] = {"status": status, "ms": int(time.time() * 1000)}
            self._state = state
            self._save()
            return self.state

    def set_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        try:
            cleaned = clean_plan(plan)
        except PlanError as error:
            raise InstallationError(str(error)) from None
        with self._lock:
            self._state["plan"] = cleaned
            self._save()
            return copy.deepcopy(cleaned)

    def set_recommendation(self, answers: dict[str, Any], recommendation: dict[str, Any]) -> None:
        with self._lock:
            self._state["questionnaire"] = {key: value for key, value in answers.items() if key in QUESTION_FIELDS}
            self._state["recommendation"] = recommendation
            self._save()

    def record_notification_test(self, outcome: dict[str, Any]) -> None:
        with self._lock:
            self._state["notification_test"] = outcome
            self._save()

    def unlink_device(self, device_id: str) -> int:
        """Plan items keep their place when the device they referred to is removed."""
        changed = 0
        with self._lock:
            for item in self._state["plan"].get("items", []):
                for key in ("device_id", "camera_id"):
                    if item.get(key) == device_id:
                        item.pop(key)
                        if item["kind"] == "camera":
                            item["status"] = "proposed"
                        changed += 1
            if changed:
                self._save()
        return changed

    def complete(self, actor: str, open_issues: list[str]) -> dict[str, Any]:
        with self._lock:
            self._state["completed_ms"] = int(time.time() * 1000)
            self._state["completed_by"] = actor[:64]
            self._state["completed_with_issues"] = open_issues[:50]
            self._state["steps"]["finish"] = {"status": "done", "ms": self._state["completed_ms"]}
            self._save()
            return self.state

    def reopen(self) -> dict[str, Any]:
        with self._lock:
            self._state["completed_ms"] = None
            self._state["steps"].pop("finish", None)
            self._save()
            return self.state

    def reset(self, keep_plan: bool = True) -> dict[str, Any]:
        """Start the journey again. The site configuration and devices are untouched."""
        with self._lock:
            plan = self._state["plan"] if keep_plan else {"items": [], "updated_ms": 0}
            self._state = _empty() | {"plan": plan}
            self._save()
            return self.state


# ---- readiness ---------------------------------------------------------------------------


def _check(checks: list, check_id: str, step: str, status: str, title: str, detail: str = "", *, blocking: bool = False, device: str = "") -> None:
    checks.append({"id": check_id, "step": step, "status": status, "title": title, "detail": detail, "blocking": blocking and status == "fail",
                   "device": device})


def readiness(*, state: dict[str, Any], config: dict[str, Any], devices: list[dict[str, Any]], health: dict[str, Any],
              coverage_analysis: dict[str, Any] | None = None) -> dict[str, Any]:
    """Live readiness. `devices` are entries from devices.py; `health` carries
    coverage, storage, database and backup state from the service."""
    path = state.get("path", "")
    checks: list[dict[str, Any]] = []
    cameras = [d for d in devices if d["category"] == "camera"]
    drones = [d for d in devices if d["category"] == "drone"]
    sensors = [d for d in devices if d["category"].endswith("_sensor")]
    real_devices = [d for d in devices if d["category"] != "hub"]

    # 1-2. type and property
    _check(checks, "path", "type", "pass" if path else "fail", "Installation type chosen" if path else "Choose an installation type", blocking=True)
    site = config.get("site", {})
    site_map = site.get("map", {})
    named = bool(site.get("name"))
    _check(checks, "property_name", "property", "pass" if named else "warn", "Property named" if named else "Give the property a name")
    needs_location = path in ("drone_first", "complete_package") or bool(drones)
    if needs_location:
        located = bool(site_map.get("origin_geo"))
        _check(checks, "property_location", "property", "pass" if located else "fail",
               "Property location set" if located else "Set the property location (needed for the drone geofence)", blocking=bool(drones))

    # 3. plan
    items = state.get("plan", {}).get("items", [])
    if path in ("new_cameras", "complete_package"):
        planned = sum(1 for item in items if item["kind"] == "camera")
        _check(checks, "plan_cameras", "plan", "pass" if planned else "warn",
               f"{planned} camera(s) placed on the plan" if planned else "No cameras placed on the plan yet",
               "Place cameras on the property map to check coverage before installing.")
    if path in ("drone_first", "complete_package"):
        docks = sum(1 for item in items if item["kind"] == "drone_dock")
        _check(checks, "plan_dock", "plan", "pass" if docks else "warn", "Drone dock location planned" if docks else "No drone dock location on the plan")
    if coverage_analysis and coverage_analysis.get("gaps"):
        _check(checks, "plan_gaps", "plan", "warn", f"{len(coverage_analysis['gaps'])} planned priority spot(s) look uncovered",
               "; ".join(coverage_analysis["gaps"][:4]))

    # 4. hardware
    if path == "drone_first":
        monitoring_capable = drones or sensors or cameras
        need = "a drone"
    elif path == "complete_package":
        monitoring_capable = cameras or drones
        need = "a camera or a drone"
    else:
        monitoring_capable = cameras
        need = "a camera"
    _check(checks, "hardware_present", "hardware", "pass" if monitoring_capable else "fail",
           f"{len(real_devices)} device(s) registered" if monitoring_capable else f"Register at least {need}", blocking=True)
    for device in real_devices:
        if device.get("support") == "planned":
            _check(checks, f"planned_{device['id']}", "hardware", "warn", f"{device['name']}: planned hardware",
                   "Registered for planning only; Sentinel cannot connect to it in this build.", device=device["id"])

    simulated_names = [device["name"] for device in real_devices if device.get("simulated")]
    if simulated_names:
        _check(checks, "simulated", "hardware", "warn", f"{len(simulated_names)} simulated device(s): {', '.join(simulated_names[:6])}"
               + ("…" if len(simulated_names) > 6 else ""), "Simulated devices exercise the software; they watch nothing real.")

    # 5. verify: separate levels per device
    for camera in cameras:
        level, name = camera["level"], camera["name"]
        if camera.get("state") == "not_running":
            _check(checks, f"camera_{camera['id']}", "verify", "pending", f"{name}: not checked", camera.get("detail", ""), device=camera["id"])
        elif level == "ai_verified":
            _check(checks, f"camera_{camera['id']}", "verify", "pass", f"{name}: AI monitoring verified", device=camera["id"])
        elif level == "video":
            status = "warn" if camera.get("state") in ("unverified", "starting") else "fail"
            _check(checks, f"camera_{camera['id']}", "verify", status, f"{name}: video available, AI monitoring not verified",
                   camera.get("detail", ""), device=camera["id"])
        elif level == "connected":
            _check(checks, f"camera_{camera['id']}", "verify", "fail", f"{name}: connected but no video", camera.get("detail", ""), device=camera["id"])
        else:
            _check(checks, f"camera_{camera['id']}", "verify", "fail" if camera.get("state") not in ("starting",) else "pending",
                   f"{name}: not connected", camera.get("detail", ""), device=camera["id"])
    for drone in drones:
        if drone.get("state") == "ready":
            _check(checks, f"drone_{drone['id']}", "verify", "pass", f"{drone['name']}: telemetry connected, ready for approved inspections",
                   "Drone video is not ingested in this build.", device=drone["id"])
        elif drone["level"] == "connected":
            _check(checks, f"drone_{drone['id']}", "verify", "warn", f"{drone['name']}: connected, not ready", drone.get("detail", ""), device=drone["id"])
        else:
            _check(checks, f"drone_{drone['id']}", "verify", "fail", f"{drone['name']}: no telemetry", drone.get("detail", ""), device=drone["id"])
    for sensor in sensors:
        if sensor["level"] == "connected":
            _check(checks, f"sensor_{sensor['id']}", "verify", "pass" if not sensor.get("warnings") else "warn", f"{sensor['name']}: reporting",
                   "; ".join(sensor.get("warnings", [])) or "Sensor reports are logged; sensors do not raise alerts yet.", device=sensor["id"])
        else:
            _check(checks, f"sensor_{sensor['id']}", "verify", "warn", f"{sensor['name']}: no reports", sensor.get("detail", ""), device=sensor["id"])
    for recorder in (d for d in devices if d["category"] == "recorder"):
        if recorder.get("state") == "no_channels":
            _check(checks, f"recorder_{recorder['id']}", "verify", "warn", f"{recorder['name']}: no channels added", recorder.get("detail", ""), device=recorder["id"])

    # 6. zones and privacy
    for camera in cameras:
        if not camera.get("zones"):
            _check(checks, f"zones_{camera['id']}", "zones", "fail", f"{camera['name']}: no zones",
                   "Without zones Sentinel cannot report activity on this camera. Draw at least one zone.", device=camera["id"])
    privacy_cameras = {entry["camera"] for entry in (coverage_analysis or {}).get("privacy", [])}
    for camera in cameras:
        if camera["name"] in privacy_cameras and not camera.get("privacy_masks"):
            _check(checks, f"mask_{camera['id']}", "zones", "warn", f"{camera['name']}: may see a public or privacy-sensitive area",
                   "The plan shows its view reaching such an area and it has no privacy mask.", device=camera["id"])
    if drones:
        points = len(config.get("drone", {}).get("inspection_points", [])) + sum(
            1 for camera in config.get("cameras", []) for zone in camera.get("zones", []) if zone.get("map_polygon"))
        _check(checks, "inspection_points", "zones", "pass" if points else "warn",
               f"{points} inspection location(s) available" if points else "No inspection locations for the drone",
               "" if points else "Add observation points in the drone setup or zone footprints on the property map.")

    # 7. rules
    rules = [rule for rule in config.get("rules", []) if rule.get("enabled", True)]
    if cameras:
        _check(checks, "rules", "rules", "pass" if rules else "warn", f"{len(rules)} security rule(s) on" if rules else "No security rule is turned on",
               "" if rules else "Activity is logged but nothing will raise an incident. Add a rule in the rule builder.")
    else:
        _check(checks, "rules", "rules", "pass", "Rules need camera zones", "Add cameras later to use security rules; drone inspections are requested by an operator.")

    # 8. alerts
    channels = [c for c in config.get("alerts", {}).get("channels", []) if c.get("enabled", True) and c.get("type") in ("ntfy", "mqtt")]
    _check(checks, "alert_channel", "alerts", "pass" if channels else "warn",
           f"{len(channels)} notification channel(s) on" if channels else "No push notification channel",
           "" if channels else "Alerts will only appear in the dashboard.")
    test = state.get("notification_test")
    if channels:
        if not test:
            _check(checks, "alert_test", "alerts", "warn", "Test notification not sent", "Send one and confirm it arrives on the phone.")
        elif all(str(outcome).startswith("sent") for name, outcome in test.get("outcomes", {}).items() if name != "log"):
            _check(checks, "alert_test", "alerts", "pass", "Test notification handed to every channel",
                   "Confirm that it arrived on the phone: Sentinel can only see that the push service accepted it.")
        else:
            _check(checks, "alert_test", "alerts", "fail", "Test notification failed", "; ".join(f"{k}: {v}" for k, v in test.get("outcomes", {}).items() if k != "log"))

    # 9. health
    storage = health.get("storage", {})
    database = health.get("database", {})
    _check(checks, "storage", "health", "fail" if storage.get("low") else "pass", "Disk space low: recording may pause" if storage.get("low") else "Enough disk space")
    _check(checks, "database", "health", "pass" if database.get("ok", True) else "fail", "Event database saving normally" if database.get("ok", True)
           else "Event database writes failing", database.get("last_error", ""))
    backups = config.get("storage", {}).get("backups", {}).get("enabled", True)
    _check(checks, "backups", "health", "pass" if backups else "warn", "Daily backups on" if backups else "Automatic backups are off")
    for name, component in (health.get("coverage", {}).get("components") or {}).items():
        if component.get("state") != "ok":
            _check(checks, f"component_{name}", "health", "fail", f"{component.get('label', name)}: {component.get('detail', '')}")

    # Levels and the plain statement.
    running = [c for c in cameras if c.get("state") != "not_running"]
    verified = [c for c in cameras if c["level"] == "ai_verified"]
    video = [c for c in cameras if c["level"] in ("video", "ai_verified")]
    connected = [d for d in real_devices if d.get("level") in ("connected", "video", "ai_verified")]
    reporting_sensors = [s for s in sensors if s["level"] == "connected"]
    ready_drones = [d for d in drones if d.get("state") == "ready"]
    simulated = any(d.get("simulated") for d in real_devices)
    if cameras and verified:
        state_name = "monitoring_verified" if len(verified) == len(cameras) else "partial"
        text = f"AI monitoring verified on {len(verified)} of {len(cameras)} camera(s)."
    elif cameras and not running:
        state_name, text = "unknown", "Camera processing is not running in this session, so monitoring cannot be checked."
    elif cameras:
        state_name, text = "not_monitoring", "Not monitoring yet: no camera has passed the AI monitoring check."
    else:
        state_name, text = "no_cameras", "No fixed cameras: there is no continuous video monitoring."
    extras = []
    if reporting_sensors:
        extras.append(f"{len(reporting_sensors)} sensor(s) reporting (logged only; sensors do not raise alerts yet).")
    if drones:
        extras.append(f"{len(ready_drones)} of {len(drones)} drone(s) ready for operator-approved inspections; flights need a person's approval.")
    if simulated:
        extras.append("Some devices are simulated: they demonstrate the system but watch nothing real.")
    steps_view = {}
    marks = state.get("steps", {})
    for step in STEP_IDS:
        mine = [c for c in checks if c["step"] == step]
        steps_view[step] = {
            "status": "done" if step == "finish" and state.get("completed_ms") else marks.get(step, {}).get("status", "pending"),
            "fails": sum(1 for c in mine if c["status"] == "fail"), "warnings": sum(1 for c in mine if c["status"] == "warn"),
        }
    open_issues = [c["title"] for c in checks if c["status"] in ("fail", "warn")]
    return {
        "levels": {
            "installation_completed": bool(state.get("completed_ms")),
            "steps_done": sum(1 for step in STEP_IDS if steps_view[step]["status"] in ("done", "skipped")), "steps_total": len(STEP_IDS),
            "devices_connected": len(connected), "devices_total": len(real_devices),
            "cameras_video": len(video), "cameras_ai_verified": len(verified), "cameras_total": len(cameras),
            "sensors_reporting": len(reporting_sensors), "drones_ready": len(ready_drones),
        },
        "statement": {"state": state_name, "text": text, "extras": extras},
        "checks": checks, "steps": steps_view,
        "blocking": [c["title"] for c in checks if c["blocking"]],
        "open_issues": open_issues,
        "can_complete": not any(c["blocking"] for c in checks),
    }
