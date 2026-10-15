"""Flame Mode: the agricultural configuration of the Sentinel drone.

`FlameService` sits beside the security services and shares their store,
device registry, drone managers, notification channels and audit log. It adds:
- removable accessories: live state (simulated), compatibility, records;
- the drone's configuration (security or agricultural monitoring), switched only
  on the ground and only when the accessory is attached, commissioned and
  inspected; in agricultural configuration the drone takes no security
  inspection requests, and in security configuration no burn-watch requests;
- fields, burn operations, the monitoring loop, notifications and reports.

Fields, configurations and settings live in `<storage>/flame.json` (planning
never restarts cameras); records and observations live in SQLite.

Sentinel never controls ignition or release hardware and never states that a
burn is approved, legal or safe to light. Standard library only."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import sqlite3
import threading
import time
import uuid
import zlib
from datetime import date
from pathlib import Path
from typing import Any

from ..drone import accessories as acc
from ..drone.base import FlightCommandRejected, distance_m, offset_to_geo
from ..models import Notification
from . import fields as fieldlib
from . import operations as ops
from .reports import build_report
from .thermal import DEFAULT_THERMAL, REAL_THERMAL_STATUS, THERMAL_CAMERAS, SimulatedFireField, ThermalObserver

log = logging.getLogger("sentinel.flame")

DEFAULT_SETTINGS = {
    "hotspot_threshold_c": 150.0,  # heat above this is a hotspot (flaming or strongly smouldering residue)
    "residual_threshold_c": 60.0,  # post-burn inspection: remaining heat above this is reported
    "notify_cooldown_s": 300.0,  # one notification per place and kind within this time
    "watch_hover_s": 180.0,  # time on station for a burn-watch flight
    "watch_camera_pitch_deg": -50.0,  # the pilot tilts the thermal camera toward the field edge; assumed when the gimbal reports no angle
    "coverage_window_s": 600.0,
    "inspect_every_days": 30.0,
    "inspect_every_operations": 5,
    "simulation_speed": 1.0,
}
WATCH_SIDES = {"north": 0, "north-east": 45, "east": 90, "south-east": 135, "south": 180, "south-west": 225, "west": 270, "north-west": 315}
SEVERITY_PRIORITY = {"info": 2, "warning": 4, "critical": 5}
NOTIFY_DISCLAIMER = ("Sentinel reports thermal observations and crew reports; it cannot judge fire containment. "
                     "If a fire has escaped, call 911.")


class FlameError(ValueError):
    """A request the person must correct."""


class AccessorySim:
    """Simulated accessory reports: attachment, identity, latch and heat-shield
    temperatures. Faults can be injected; overheating follows exposure to
    simulated flames below the aircraft."""

    REPORT_EVERY_S = 2.0

    def __init__(self, ambient_c: float = 15.0) -> None:
        self.attached = True
        self.latch_closed = True
        self.identity_ok = True
        self.silent = False
        self.forced: set[str] = set()
        self.ambient_c = ambient_c
        self.bay_c = ambient_c + 8
        self.shield_c = ambient_c
        self.last_report_ms = int(time.time() * 1000)

    def step(self, dt_s: float, now_ms: int, exposure_c: float) -> None:
        if self.silent:
            return
        target_shield = self.ambient_c + exposure_c
        self.shield_c += (target_shield - self.shield_c) * min(1.0, dt_s / 20.0)
        self.bay_c += ((self.ambient_c + 8 + 0.3 * exposure_c) - self.bay_c) * min(1.0, dt_s / 60.0)
        self.last_report_ms = now_ms

    def apply(self, action: str) -> None:
        if action == "attach":
            self.attached, self.latch_closed, self.identity_ok = True, True, True
        elif action == "detach":
            self.attached = False
        elif action == "latch_open":
            self.latch_closed = False
        elif action == "identity_mismatch":
            self.identity_ok = False
        elif action == "overheat":
            self.forced.add("overheat")
        elif action == "silent":
            self.silent = True
        elif action == "clear":
            self.latch_closed, self.identity_ok, self.silent = True, True, False
            self.forced.clear()
            self.shield_c = self.ambient_c
        else:
            raise FlameError(f"unknown simulated accessory action {action!r}")

    def view(self, profile: dict[str, Any], now_ms: int) -> dict[str, Any]:
        age = round((now_ms - self.last_report_ms) / 1000, 1)
        reporting = not self.silent and age <= 5 * self.REPORT_EVERY_S
        shield = max(self.shield_c, (profile.get("max_temperature_c") or 60) + 5) if "overheat" in self.forced else self.shield_c
        faults = []
        if not reporting:
            faults.append("silent")
        else:
            if not self.attached:
                faults.append("detached")
            elif not self.latch_closed:
                faults.append("latch_open")
            if self.attached and not self.identity_ok:
                faults.append("identity_mismatch")
            if profile.get("max_temperature_c") and shield > profile["max_temperature_c"]:
                faults.append("overheat")
        return {"reporting": reporting, "last_report_age_s": age,
                "health": {"attached": self.attached, "latch_closed": self.latch_closed, "identity_ok": self.identity_ok,
                           "bay_temperature_c": round(self.bay_c, 1), "shield_temperature_c": round(shield, 1)},
                "faults": faults}


class FlameService:
    def __init__(self, service: Any, storage_dir: Path | None = None) -> None:
        self.service = service
        self.store = service.runtime.store
        self.path = Path(storage_dir or service.storage_dir) / "flame.json"
        self._lock = threading.RLock()
        self.doc = self._load()
        self.sims: dict[str, AccessorySim] = {}
        self.fires: dict[str, SimulatedFireField] = {}
        self.observer = ThermalObserver(THERMAL_CAMERAS[DEFAULT_THERMAL], threshold_c=self.settings["hotspot_threshold_c"])
        self.runtime: dict[str, dict[str, Any]] = {}  # per operation: coverage, notification keys, last states
        self.record_failures = 0
        self._accessory_faults: dict[str, set[str]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_step = time.monotonic()
        self.operations: dict[str, dict[str, Any]] = {op["operation_id"]: op for op in self.store.list_burn_operations(limit=200)
                                                      if op["status"] in ops.OPEN_STATUSES}
        self._restore()
        self.apply_hooks()

    # ---- persistence -----------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or any(not isinstance(doc.get(key, {}), dict) for key in ("fields", "configurations", "settings")):
                raise ValueError("Flame Mode settings must contain objects")
        except FileNotFoundError:
            doc = {}
        except ValueError as error:
            # Keep the damaged bytes before any later save writes new settings.
            recovery = self.path.parent / "recovery"
            recovery.mkdir(parents=True, exist_ok=True)
            saved = recovery / f"flame.{uuid.uuid4().hex}.damaged.json"
            digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
            self.path.replace(saved)
            saved.with_suffix(".sha256").write_text(digest, encoding="ascii")
            log.error("Flame Mode settings damaged (%s); original preserved in recovery/%s", error, saved.name)
            self.service.runtime.health.set_component("flame_config", "degraded", "Flame Mode settings were damaged; the original is preserved in recovery. Check fields and configuration.",
                                                      int(time.time() * 1000))
            doc = {}
        doc.setdefault("enabled", False)
        doc.setdefault("fields", {})
        doc.setdefault("configurations", {})
        doc["settings"] = DEFAULT_SETTINGS | doc.get("settings", {})
        return doc

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.doc, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    @property
    def settings(self) -> dict[str, Any]:
        return self.doc["settings"]

    def _restore(self) -> None:
        """After a restart an operation that was being monitored is kept open,
        but its watch is marked interrupted: nobody was watching meanwhile."""
        for operation in self.operations.values():
            if operation["status"] in ops.ACTIVE_STATUSES:
                self._observe(operation, "monitoring_interrupted", "warning",
                              "Sentinel restarted while this operation was open: thermal watch was interrupted until now.", notify=False)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="sentinel-flame", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            now = time.monotonic()
            dt, self._last_step = now - self._last_step, now
            try:
                self.step(min(dt, 5.0))
            except Exception:  # the watch loop must keep running
                log.exception("flame monitoring step failed")

    # ---- context ---------------------------------------------------------------------

    def _config(self) -> dict[str, Any]:
        # Site saves hold the editor lock while applying configuration, which
        # takes the drone fleet lock. Read the service's applied snapshot here:
        # readiness/hooks run under drone locks and must never take the editor
        # lock in the opposite order. apply_config replaces this snapshot before
        # rebuilding managers and attaching their FLAME hooks.
        return self.service.config

    def _origin(self) -> tuple[float, float] | None:
        origin = self._config().get("site", {}).get("map", {}).get("origin_geo")
        return tuple(origin) if origin else None

    def _audit(self, actor: str, action: str, body: dict[str, Any]) -> None:
        self.store.audit(actor, action, body)

    def _publish(self, kind: str, body: dict[str, Any]) -> None:
        try:
            self.service.runtime.bus.publish("flame", {"kind": kind} | body)
        except Exception:
            log.exception("flame bus publish failed")

    def _drone_devices(self) -> dict[str, dict[str, Any]]:
        return {d["id"]: d for d in self._config().get("devices", []) if d.get("category") == "drone"}

    def _manager(self, drone_id: str) -> Any:
        return self.service.drone_manager(drone_id)

    def enabled(self) -> bool:
        return bool(self.doc["enabled"]) or bool(self.accessory_devices())

    def set_enabled(self, enabled: bool, actor: str) -> dict[str, Any]:
        with self._lock:
            self.doc["enabled"] = bool(enabled)
            self._save()
        self._audit(actor, "flame_mode_enabled" if enabled else "flame_mode_disabled", {})
        return self.overview()

    # ---- accessories -----------------------------------------------------------------

    def accessory_devices(self) -> list[dict[str, Any]]:
        return [d for d in self._config().get("devices", []) if d.get("category") == "accessory"]

    def accessory_for_drone(self, drone_id: str) -> dict[str, Any] | None:
        return next((d for d in self.accessory_devices() if d.get("drone_id") == drone_id and d.get("enabled", True)), None)

    def _sim(self, device: dict[str, Any]) -> AccessorySim | None:
        profile = acc.profile_for(device)
        if not device.get("simulated") or not profile or profile["support"] != "simulated":
            return None
        if device["id"] not in self.sims:
            self.sims[device["id"]] = AccessorySim()
        return self.sims[device["id"]]

    def _records(self, accessory_id: str) -> list[dict[str, Any]]:
        return self.store.list_accessory_records(accessory_id=accessory_id, limit=200)

    def accessory_view(self, device: dict[str, Any], now_ms: int | None = None) -> dict[str, Any]:
        now_ms = now_ms or ops.now_ms()
        profile = acc.profile_for(device) or {}
        drone = self._drone_devices().get(device.get("drone_id", ""), {})
        from ..drone import catalog

        model = catalog.model_for(drone)
        records = self._records(device["id"])
        commissioning = next((r for r in records if r["kind"] == "commissioning"), None)
        measured = (commissioning or {}).get("measured_mass_g")
        compatibility = acc.evaluate(profile, model, measured_mass_g=measured) if profile else None
        view: dict[str, Any] = {
            "id": device["id"], "name": device.get("name", device["id"]), "profile": profile.get("id", ""), "profile_name": profile.get("name", ""),
            "support": profile.get("support", "planned"), "simulated": bool(device.get("simulated")), "drone_id": device.get("drone_id", ""),
            "drone_model": model["id"], "drone_model_name": model["name"], "serial": device.get("serial", ""), "kind": profile.get("kind", ""),
            "applications": profile.get("applications", []), "capabilities": profile.get("health", []),
            "compatibility": compatibility, "commissioned": commissioning is not None,
            "commissioning": {k: commissioning.get(k) for k in ("recorded_ms", "by", "measured_mass_g", "cg_checked", "hover_throttle_pct", "note")} if commissioning else None,
            "mass_g": measured if measured is not None else (profile.get("mass") or {}).get("value_g"),
            "mass_basis": "measured" if measured is not None else (profile.get("mass") or {}).get("basis", ""),
        }
        sim = self._sim(device)
        if sim is not None:
            live = sim.view(profile, now_ms)
            view |= live
            view["attachment"] = "attached" if live["reporting"] and live["health"]["attached"] else "detached" if live["reporting"] else "unknown"
            view["attachment_detail"] = {"attached": "attached (simulated report)", "detached": "the simulated kit reports it is not attached",
                                         "unknown": "no reports from the accessory"}[view["attachment"]]
            view["state"] = "fault" if live["faults"] else "ok"
        else:
            view |= {"reporting": False, "health": {}, "faults": [], "attachment": "unknown", "state": "not_connected",
                     "attachment_detail": ("planned hardware: registration only, Sentinel receives no reports from it"
                                           if profile.get("support") == "planned" else "Sentinel receives no reports from this accessory")}
        view |= self._inspection_status(records, now_ms)
        return view

    def _inspection_status(self, records: list[dict[str, Any]], now_ms: int) -> dict[str, Any]:
        inspections = [r for r in records if r["kind"] == "inspection"]
        last = inspections[0]["recorded_ms"] if inspections else None
        faults_since = [r for r in records if r["kind"] == "fault" and (last is None or r["recorded_ms"] > last)]
        ops_since = sum(1 for op in self.store.list_burn_operations(limit=200)
                        if op.get("timeline", {}).get("monitoring_started_ms", 0) > (last or 0))
        reasons = []
        if last is None:
            reasons.append("no inspection recorded")
        elif now_ms - last > self.settings["inspect_every_days"] * 86_400_000:
            reasons.append(f"last inspection more than {self.settings['inspect_every_days']:g} days ago")
        if faults_since:
            reasons.append(f"{len(faults_since)} fault(s) since the last inspection")
        if last is not None and ops_since >= self.settings["inspect_every_operations"]:
            reasons.append(f"{ops_since} operations since the last inspection")
        return {"last_inspection_ms": last, "inspection_due": bool(reasons), "inspection_reasons": reasons,
                "records": records[:30]}

    def accessories(self) -> list[dict[str, Any]]:
        now = ops.now_ms()
        return [self.accessory_view(device, now) for device in self.accessory_devices()]

    def accessory(self, accessory_id: str) -> dict[str, Any]:
        device = next((d for d in self.accessory_devices() if d["id"] == accessory_id), None)
        if device is None:
            raise KeyError(accessory_id)
        return self.accessory_view(device)

    def add_accessory_record(self, accessory_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        kind = payload.get("kind")
        if kind not in ("inspection", "maintenance", "repair", "commissioning", "note"):
            raise FlameError("kind must be inspection, maintenance, repair, commissioning or note")
        with self._lock:
            view = self.accessory(accessory_id)
            record: dict[str, Any] = {"record_id": ops.new_id("accrec"), "accessory_id": accessory_id, "kind": kind, "recorded_ms": ops.now_ms(),
                                      "by": actor, "note": ops.clean_text(payload.get("note"), 500), "drone_id": view["drone_id"]}
            if kind == "commissioning":
                if view["state"] == "not_connected":
                    raise FlameError("commissioning needs reports from the accessory; this one is registration only")
                if view["attachment"] != "attached" or not view["health"].get("identity_ok"):
                    raise FlameError("commissioning needs the accessory attached and identified")
                if view["compatibility"] and view["compatibility"]["verdict"] == "not_compatible":
                    raise FlameError(view["compatibility"]["statement"])
                mass = payload.get("measured_mass_g")
                if not isinstance(mass, (int, float)) or isinstance(mass, bool) or not 1 <= mass <= 5000:
                    raise FlameError("weigh the accessory: measured_mass_g (1-5000 g) is required for commissioning")
                if payload.get("cg_checked") is not True:
                    raise FlameError("confirm the centre of gravity was checked with the accessory and a full battery fitted (cg_checked)")
                throttle = payload.get("hover_throttle_pct")
                if throttle is not None and (not isinstance(throttle, (int, float)) or not 10 <= throttle <= 90):
                    raise FlameError("hover_throttle_pct must be between 10 and 90")
                record |= {"measured_mass_g": float(mass), "cg_checked": True, "hover_throttle_pct": throttle,
                           "compatibility": acc.evaluate(acc.PROFILE_INDEX[view["profile"]], self._model_for_drone(view["drone_id"]),
                                                         measured_mass_g=float(mass))["verdict"]}
            self.store.add_accessory_record(record)
            self._audit(actor, f"accessory_{kind}_recorded", {"accessory": accessory_id, "record_id": record["record_id"]})
        return record

    def simulate_accessory(self, accessory_id: str, action: str, actor: str) -> dict[str, Any]:
        device = next((d for d in self.accessory_devices() if d["id"] == accessory_id), None)
        if device is None:
            raise KeyError(accessory_id)
        sim = self._sim(device)
        if sim is None:
            raise FlameError("only a simulated accessory can be changed from here")
        with self._lock:
            sim.apply(action)
            self._audit(actor, "accessory_simulation", {"accessory": accessory_id, "action": action})
            self._check_accessory_faults(ops.now_ms())
        return self.accessory(accessory_id)

    def _model_for_drone(self, drone_id: str) -> dict[str, Any]:
        from ..drone import catalog

        return catalog.model_for(self._drone_devices().get(drone_id))

    def _check_accessory_faults(self, now_ms: int) -> None:
        for device in self.accessory_devices():
            sim = self._sim(device)
            if sim is None:
                continue
            view = sim.view(acc.profile_for(device) or {}, now_ms)
            faults = set(view["faults"])
            previous = self._accessory_faults.get(device["id"], set())
            for fault in sorted(faults - previous):
                kind = "detached" if fault == "detached" else "fault"
                self.store.add_accessory_record({"record_id": ops.new_id("accrec"), "accessory_id": device["id"], "kind": kind, "recorded_ms": now_ms,
                                                 "by": "system", "note": acc.FAULTS.get(fault, fault), "fault": fault, "drone_id": device.get("drone_id", "")})
                self._audit("system", "accessory_fault", {"accessory": device["id"], "fault": fault})
                for operation in self._active_for_drone(device.get("drone_id", "")):
                    self._observe(operation, "accessory_fault", "critical" if fault in ("detached", "latch_open", "overheat") else "warning",
                                  f"{device.get('name', device['id'])}: {acc.FAULTS.get(fault, fault)}.",
                                  extra={"fault": fault}, key=f"accessory:{fault}",
                                  title=f"Drone accessory fault: {acc.FAULTS.get(fault, fault)}")
            for fault in sorted(previous - faults):
                if fault == "detached":
                    self.store.add_accessory_record({"record_id": ops.new_id("accrec"), "accessory_id": device["id"], "kind": "attached",
                                                     "recorded_ms": now_ms, "by": "system", "note": "attachment reported", "drone_id": device.get("drone_id", "")})
            self._accessory_faults[device["id"]] = faults

    # ---- drone configuration --------------------------------------------------------------

    def configuration(self, drone_id: str) -> str:
        return self.doc["configurations"].get(drone_id, {}).get("configuration", "security")

    def configuration_view(self, drone_id: str) -> dict[str, Any]:
        record = self.doc["configurations"].get(drone_id, {})
        current = record.get("configuration", "security")
        target = "security" if current == "agricultural_monitoring" else "agricultural_monitoring"
        return {"drone_id": drone_id, "configuration": current, "label": acc.CONFIGURATIONS[current], "since_ms": record.get("since_ms"),
                "by": record.get("by", ""), "switch_to": target, "switch_blockers": self._switch_blockers(drone_id, target),
                "effects": self._configuration_effects(current)}

    @staticmethod
    def _configuration_effects(configuration: str) -> list[str]:
        if configuration == "agricultural_monitoring":
            return ["Takes burn-watch requests only: security inspection requests (including automatic ones from incidents) are refused",
                    "Readiness includes the accessory's attachment and faults",
                    "Route endurance uses the take-off mass with the accessory"]
        return ["Takes security inspection requests as usual", "Burn-watch requests are refused until it is switched to agricultural monitoring"]

    def _switch_blockers(self, drone_id: str, target: str) -> list[dict[str, str]]:
        manager = self._manager(drone_id)
        if manager is None:
            return [{"id": "drone_not_operable", "detail": "Sentinel has no connection to this drone (planned hardware or drone features off)"}]
        telemetry = manager.adapter.telemetry().as_dict()
        open_requests = sum(1 for r in manager.requests.values() if r.status == "pending_approval" and r.source != "flame")
        device = self.accessory_for_drone(drone_id)
        return acc.switch_blockers(target, telemetry=telemetry, mission_open=manager._mission is not None, open_requests=open_requests,
                                   accessory=self.accessory_view(device) if device else None,
                                   operation_active=bool(self._active_for_drone(drone_id)))

    def set_configuration(self, drone_id: str, target: str, actor: str) -> dict[str, Any]:
        with self._lock:
            if target == self.configuration(drone_id):
                return self.configuration_view(drone_id)
            blockers = self._switch_blockers(drone_id, target)
            if blockers:
                raise FlightCommandRejected("cannot switch: " + "; ".join(b["detail"] for b in blockers))
            self.doc["configurations"][drone_id] = {"configuration": target, "since_ms": ops.now_ms(), "by": actor}
            self._save()
            record = {"record_id": ops.new_id("cfg"), "kind": "configuration", "recorded_ms": ops.now_ms(), "drone_id": drone_id,
                      "configuration": target, "by": actor}
            self.store.add_ag_record(record)
            self._audit(actor, "drone_configuration_changed", {"drone": drone_id, "configuration": target})
            self.apply_hooks()
            self._publish("configuration", {"drone_id": drone_id, "configuration": target})
        return self.configuration_view(drone_id)

    def apply_hooks(self) -> None:
        """Attach the configuration guard, readiness checks and the payload-
        adjusted model to every drone manager. Managers are rebuilt when drone
        settings change, so this runs on every monitoring step."""
        for drone_id, manager in list(getattr(self.service, "drones", {}).items()):
            if getattr(manager, "_flame_hooked", None) is not self:
                manager._flame_base_model = manager.model
                manager.request_guard = lambda source, _id=drone_id: self._request_guard(_id, source)
                manager.extra_checks = lambda _id=drone_id: self._readiness_checks(_id)
                manager._flame_hooked = self
            device = self.accessory_for_drone(drone_id)
            base = manager._flame_base_model
            if device is not None:
                view = self.accessory_view(device)
                fitted = view["attachment"] == "attached" and view.get("mass_g")
                manager.model = acc.model_with_payload(base, float(view["mass_g"]), view["name"]) if fitted else base
            else:
                manager.model = base

    def _request_guard(self, drone_id: str, source: str) -> str:
        configuration = self.configuration(drone_id)
        if configuration == "agricultural_monitoring" and source != "flame":
            return "the drone is in its agricultural monitoring configuration: switch it back to security on the Flame tab first"
        if configuration == "security" and source == "flame":
            return "the drone is in its security configuration: switch it to agricultural monitoring on the Flame tab first"
        return ""

    def _readiness_checks(self, drone_id: str) -> list[dict[str, Any]]:
        configuration = self.configuration(drone_id)
        checks = [{"id": "configuration", "label": "Configuration", "state": "pass", "detail": acc.CONFIGURATIONS[configuration], "blocker": ""}]
        device = self.accessory_for_drone(drone_id)
        if configuration == "agricultural_monitoring":
            view = self.accessory_view(device) if device else None
            if view is None or view["attachment"] != "attached":
                checks.append({"id": "accessory", "label": "Agricultural accessory", "state": "fail",
                               "detail": (view or {}).get("attachment_detail", "no accessory registered"), "blocker": "accessory_not_attached"})
            elif view["faults"]:
                checks.append({"id": "accessory", "label": "Agricultural accessory", "state": "fail",
                               "detail": "; ".join(acc.FAULTS.get(f, f) for f in view["faults"]), "blocker": "accessory_fault"})
            else:
                checks.append({"id": "accessory", "label": "Agricultural accessory", "state": "warn" if view["inspection_due"] else "pass",
                               "detail": f"{view['name']} attached" + (f"; inspection due ({', '.join(view['inspection_reasons'])})" if view["inspection_due"] else ""),
                               "blocker": ""})
        elif device is not None:
            view = self.accessory_view(device)
            if view["attachment"] == "attached":
                checks.append({"id": "accessory", "label": "Agricultural accessory", "state": "warn",
                               "detail": f"{view['name']} still fitted in the security configuration: heavier aircraft, shorter flights", "blocker": ""})
        return checks

    # ---- qualifications and authorizations ----------------------------------------------

    def ag_records(self, kind: str) -> list[dict[str, Any]]:
        rows = self.store.list_ag_records(kind=kind, limit=500)
        withdrawn = {r["withdraws"] for r in rows if r.get("withdraws")}
        return [r for r in rows if not r.get("withdraws") and r["record_id"] not in withdrawn]

    def add_qualification(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        record = ops.qualification(payload, actor)
        self.store.add_ag_record(record)
        self._audit(actor, "qualification_recorded", {"record_id": record["record_id"], "qualification": record["qualification"]})
        return record

    def add_authorization(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        record = ops.authorization(payload, actor)
        if record["field_id"] and record["field_id"] not in self.doc["fields"]:
            raise FlameError("unknown field")
        self.store.add_ag_record(record)
        self._audit(actor, "authorization_recorded", {"record_id": record["record_id"], "authorization": record["authorization"]})
        return record

    def withdraw_record(self, record_id: str, actor: str) -> None:
        rows = self.store.list_ag_records(limit=2000)
        target = next((r for r in rows if r["record_id"] == record_id and r["kind"] in ("qualification", "authorization")), None)
        if target is None:
            raise KeyError(record_id)
        self.store.add_ag_record({"record_id": ops.new_id("wd"), "kind": target["kind"], "recorded_ms": ops.now_ms(), "withdraws": record_id, "by": actor})
        self._audit(actor, f"{target['kind']}_withdrawn", {"record_id": record_id})

    # ---- fields ------------------------------------------------------------------------

    def fields(self) -> list[dict[str, Any]]:
        origin = self._origin()
        return [self.field_view(field, origin) for field in self.doc["fields"].values()]

    def field_view(self, field: dict[str, Any], origin: tuple[float, float] | None = None) -> dict[str, Any]:
        view = fieldlib.view(field, origin if origin is not None else self._origin())
        view["geofence"] = self._field_in_geofence(field)
        return view

    def field(self, field_id: str) -> dict[str, Any]:
        field = self.doc["fields"].get(field_id)
        if field is None:
            raise KeyError(field_id)
        return self.field_view(field)

    def save_field(self, field_id: str | None, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        with self._lock:
            existing = self.doc["fields"].get(field_id) if field_id else None
            if field_id and existing is None:
                raise KeyError(field_id)
            if existing and any(op["field_id"] == field_id and op["status"] in ops.ACTIVE_STATUSES for op in self.operations.values()):
                raise FlameError("this field is being monitored; change it after the operation")
            try:
                field = fieldlib.clean_field(payload, site_origin=self._origin(), existing=existing, taken=set(self.doc["fields"]), actor=actor)
            except fieldlib.FieldError as error:
                raise FlameError(str(error)) from None
            self.doc["fields"][field["id"]] = field
            self.doc["enabled"] = True
            self._save()
            self._audit(actor, "field_saved", {"field": field["id"], "corners": len(field["boundary"])})
        return self.field_view(field)

    def delete_field(self, field_id: str, actor: str) -> None:
        with self._lock:
            if field_id not in self.doc["fields"]:
                raise KeyError(field_id)
            if any(op["field_id"] == field_id for op in self.operations.values()):
                raise FlameError("this field has an open operation; close it first (closed operations keep their copy of the field)")
            del self.doc["fields"][field_id]
            self._save()
        self._audit(actor, "field_deleted", {"field": field_id})

    def drone_position(self, drone_id: str | None = None) -> dict[str, Any]:
        """Where the drone is now, to mark a field corner by walking or flying to it."""
        manager = self._manager(drone_id or "")
        if manager is None:
            raise FlameError("no operable drone")
        telemetry = manager.adapter.telemetry()
        if telemetry.lat is None or not telemetry.link_ok or telemetry.gps_fix < 3:
            raise FlameError("the drone has no current 3D GPS position")
        return {"lat": round(telemetry.lat, 7), "lon": round(telemetry.lon, 7), "hdop": telemetry.hdop, "satellites": telemetry.satellites,
                "note": "GPS position of the drone (typically ±2-5 m); walk or fly it to each corner"}

    def _field_in_geofence(self, field: dict[str, Any]) -> dict[str, Any]:
        drone_id = getattr(self.service, "primary_drone", "")
        manager = self._manager(drone_id) if drone_id else None
        if manager is None:
            return {"state": "missing", "detail": "no operable drone"}
        boundary = field["boundary"]
        outside = [i + 1 for i, (lat, lon) in enumerate(boundary) if manager.geofence.violations(lat, lon)]
        origin = tuple(boundary[0])
        points = fieldlib.local(boundary, origin)
        cx = sum(p[0] for p in points) / len(points)
        cy = sum(p[1] for p in points) / len(points)
        strip_out = 0
        for x, y in points:
            length = math.hypot(x - cx, y - cy) or 1.0
            scale = (length + field["watch_buffer_m"]) / length
            lat, lon = offset_to_geo(origin, cx + (x - cx) * scale, cy + (y - cy) * scale)
            strip_out += bool(manager.geofence.violations(lat, lon))
        if outside:
            return {"state": "fail", "detail": f"corner(s) {', '.join(map(str, outside[:6]))} outside the drone's geofence: extend the geofence in the drone setup"}
        if strip_out:
            return {"state": "warn", "detail": "the watch strip reaches beyond the drone's geofence: the drone cannot watch all of it"}
        return {"state": "pass", "detail": "field and watch strip inside the drone's geofence"}

    # ---- operations --------------------------------------------------------------------

    def _active_for_drone(self, drone_id: str) -> list[dict[str, Any]]:
        return [op for op in self.operations.values() if op["drone_id"] == drone_id and op["status"] in ops.ACTIVE_STATUSES]

    def _operation(self, operation_id: str) -> dict[str, Any]:
        operation = self.operations.get(operation_id)
        if operation is None:
            rows = [op for op in self.store.list_burn_operations(limit=500) if op["operation_id"] == operation_id]
            if not rows:
                raise KeyError(operation_id)
            return rows[0]
        return operation

    def _persist(self, operation: dict[str, Any]) -> None:
        self.store.upsert_burn_operation(operation)
        if operation["status"] == "closed":
            self.operations.pop(operation["operation_id"], None)
            self.fires.pop(operation["operation_id"], None)
            self.runtime.pop(operation["operation_id"], None)
        self._publish("operation", {"operation_id": operation["operation_id"], "status": operation["status"]})

    def _editable(self, operation_id: str, statuses: tuple[str, ...] = ops.OPEN_STATUSES) -> dict[str, Any]:
        operation = self._operation(operation_id)
        if operation["status"] not in statuses:
            raise FlameError(f"the operation is {ops.STATUSES[operation['status']].lower()}")
        return operation

    def create_operation(self, field_id: str, actor: str, *, drone_id: str = "", name: str = "") -> dict[str, Any]:
        with self._lock:
            field = self.doc["fields"].get(field_id)
            if field is None:
                raise KeyError(field_id)
            if any(op["field_id"] == field_id and op["status"] != "closed" for op in self.operations.values()):
                raise FlameError("this field already has an open operation")
            drone_id = drone_id or getattr(self.service, "primary_drone", "")
            manager = self._manager(drone_id) if drone_id else None
            simulated = bool(manager is not None and not manager.adapter.is_physical)
            operation = ops.new_operation(field, drone_id, actor, simulated=simulated, name=name)
            operation["field_snapshot"] = copy.deepcopy(field)
            self.operations[operation["operation_id"]] = operation
            self._persist(operation)
            self._audit(actor, "burn_operation_created", {"operation_id": operation["operation_id"], "field": field_id})
        return self.operation_view(operation["operation_id"])

    def operations_list(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = {op["operation_id"]: op for op in self.store.list_burn_operations(limit=limit)}
        rows |= self.operations
        return [{k: op.get(k) for k in ("operation_id", "name", "field_id", "field_name", "status", "created_ms", "drone_id", "simulated", "counts", "timeline")}
                for op in sorted(rows.values(), key=lambda o: -o["created_ms"])][:limit]

    def _auto_checks(self, operation: dict[str, Any]) -> dict[str, dict[str, Any]]:
        today = date.today().isoformat()
        field = operation.get("field_snapshot") or self.doc["fields"].get(operation["field_id"])
        field_view = self.field_view(field)
        latest = self._latest_weather(operation)
        limits = operation["prescription"]
        weather = ops.assess_weather(latest, limits)
        if latest and ops.now_ms() - latest["recorded_ms"] > ops.WEATHER_TTL_S * 1000:
            weather = {"state": "missing", "detail": "last weather observation is more than an hour old"}
        drone_id = operation["drone_id"]
        manager = self._manager(drone_id) if drone_id else None
        if manager is None:
            drone_ready = {"state": "missing", "detail": "no operable drone for this operation"}
        else:
            readiness = manager.readiness()
            failing = [c["label"] for c in readiness["checks"] if c["state"] == "fail"]
            drone_ready = {"state": "pass" if not failing else "fail", "detail": "all readiness checks pass" if not failing else "failing: " + ", ".join(failing)}
        configuration = self.configuration(drone_id) if drone_id else "security"
        device = self.accessory_for_drone(drone_id) if drone_id else None
        view = self.accessory_view(device) if device else None
        if configuration != "agricultural_monitoring":
            config_state = {"state": "missing", "detail": "the drone is in its security configuration"}
        elif not view or view["attachment"] != "attached" or view["faults"]:
            config_state = {"state": "fail", "detail": (view or {}).get("attachment_detail", "no accessory") if not view or view["attachment"] != "attached"
                            else "accessory fault: " + ", ".join(view["faults"])}
        else:
            config_state = {"state": "pass" if not view["inspection_due"] else "warn",
                            "detail": f"agricultural configuration, {view['name']} attached" + ("; accessory inspection due" if view["inspection_due"] else "")}
        return {
            "authorization": ops.authorization_state(self.ag_records("authorization"), operation["field_id"], today),
            "prescription": {"state": "pass" if limits.get("source") == "burn_boss" else "missing",
                             "detail": f"set by {limits.get('set_by')}" if limits.get("source") == "burn_boss" else "example limits shown; the burn boss has not set them"},
            "weather": weather,
            "smoke": ops.smoke_toward(field_view, (latest or {}).get("wind_from_deg"), limits),
            "crew_roles": ops.crew_roles(operation),
            "equipment": ops.equipment_state(operation),
            "pilot": ops.pilot_state(operation, self.ag_records("qualification"), today),
            "drone_configuration": config_state,
            "drone_ready": drone_ready,
            "field_in_geofence": field_view["geofence"],
        }

    def _latest_weather(self, operation: dict[str, Any]) -> dict[str, Any] | None:
        rows = self.store.list_burn_observations(operation["operation_id"], kind="weather", limit=50)
        return rows[-1]["weather"] | {"recorded_ms": rows[-1]["recorded_ms"]} if rows else None

    def operation_view(self, operation_id: str, *, observations: int = 200) -> dict[str, Any]:
        operation = self._operation(operation_id)
        field = operation.get("field_snapshot") or self.doc["fields"].get(operation["field_id"])
        view = copy.deepcopy(operation)
        view["status_label"] = ops.STATUSES[operation["status"]]
        view["field"] = self.field_view(field)
        view["readiness"] = ops.readiness(operation, self._auto_checks(operation)) if operation["status"] != "closed" else None
        rows = self.store.list_burn_observations(operation_id, limit=5000)
        view["observations"] = rows[-observations:]
        view["hotspots"] = self._hotspot_map(rows)
        state = self.runtime.get(operation_id, {})
        view["coverage"] = self._coverage(operation_id)
        view["thermal"] = state.get("thermal", {"state": "idle", "detail": "not monitoring"})
        view["simulation"] = self.fires[operation_id].stats() if operation_id in self.fires else None
        view["disclaimer"] = ops.DISCLAIMER
        view["watch_requests"] = self._watch_requests(operation)
        return view

    def _watch_requests(self, operation: dict[str, Any]) -> list[dict[str, Any]]:
        manager = self._manager(operation["drone_id"]) if operation["drone_id"] else None
        if manager is None:
            return []
        return [r.as_dict() for r in sorted(manager.requests.values(), key=lambda r: -r.created_ms)
                if r.source == "flame" and operation["operation_id"] in r.reason][:10]

    def _hotspot_map(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Latest hotspot per place, for the map (positions only)."""
        latest: dict[str, dict[str, Any]] = {}
        origin = self._origin()
        from ..drone.base import geo_to_map_point

        for row in rows:
            if row["kind"] in ("hotspot", "heat_outside_boundary", "heat_at_boundary", "residual_heat", "heat_near_sensitive_place"):
                spot = row.get("hotspot") or {}
                if not spot:
                    continue
                key = row.get("place_key", row["observation_id"])
                entry = {"lat": spot["lat"], "lon": spot["lon"], "zone": row.get("zone", ""), "max_c": spot.get("max_c"), "kind": row["kind"],
                         "recorded_ms": row["recorded_ms"], "uncertainty_m": spot.get("uncertainty_m")}
                if origin:
                    entry["map"] = [round(v, 1) for v in geo_to_map_point(origin, spot["lat"], spot["lon"])]
                latest[key] = entry
        return list(latest.values())[-300:]

    def update_operation(self, operation_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        """Checklist items, prescription, crew and equipment (planning and during the burn)."""
        with self._lock:
            operation = self._editable(operation_id)
            if "checklist" in payload:
                for item_id, value in dict(payload["checklist"]).items():
                    item = ops.CHECKLIST_INDEX.get(item_id)
                    if item is None or item.get("auto"):
                        raise FlameError(f"{item_id} is not a checklist item a person confirms")
                    status = (value or {}).get("status", "")
                    if status not in ("done", "not_applicable", ""):
                        raise FlameError("checklist status must be done, not_applicable or empty")
                    note = ops.clean_text((value or {}).get("note"), 200)
                    if status == "not_applicable" and not note:
                        raise FlameError(f"say why '{item['label']}' does not apply (note)")
                    operation["checklist"][item_id] = {"status": status, "note": note, "by": actor, "ms": ops.now_ms()} if status else {}
            if "prescription" in payload:
                ops.set_prescription(operation, dict(payload["prescription"]), actor)
            if "crew" in payload:
                crew = []
                for member in list(payload["crew"])[:30]:
                    name, role = ops.clean_text(member.get("name"), 60), member.get("role")
                    if not name or role not in ops.ROLES:
                        raise FlameError(f"each crew member needs a name and a role from {list(ops.ROLES)}")
                    crew.append({"name": name, "role": role})
                operation["crew"] = crew
            if "equipment" in payload:
                known = {item["id"]: item for item in operation["equipment"]}
                for entry in list(payload["equipment"])[:40]:
                    item_id = ops.clean_text(entry.get("id"), 30) or ops.new_id("eq")
                    item = known.get(item_id) or {"id": item_id, "label": ops.clean_text(entry.get("label"), 80) or "Equipment"}
                    if "checked" in entry:
                        item["checked"] = bool(entry["checked"])
                        item |= {"by": actor, "ms": ops.now_ms()}
                    if entry.get("note") is not None:
                        item["note"] = ops.clean_text(entry["note"], 120)
                    if item_id not in known:
                        operation["equipment"].append(item)
                        known[item_id] = item
            self._persist(operation)
            self._audit(actor, "burn_operation_updated", {"operation_id": operation_id, "parts": sorted(payload)})
        return self.operation_view(operation_id)

    def add_weather(self, operation_id: str, payload: dict[str, Any], actor: str, *, source: str = "observed") -> dict[str, Any]:
        with self._lock:
            operation = self._editable(operation_id)
            entry = ops.weather_entry(payload, actor, source=source)
            assessment = ops.assess_weather(entry, operation["prescription"])
            field = self.field_view(operation.get("field_snapshot") or self.doc["fields"][operation["field_id"]])
            smoke = ops.smoke_toward(field, entry.get("wind_from_deg"), operation["prescription"])
            severity = "warning" if assessment["state"] == "fail" or smoke["state"] == "fail" else "info"
            text = f"Weather ({source}): wind {entry['wind_mps']:g} m/s" + (f" from {entry['wind_from_deg']:.0f}°" if "wind_from_deg" in entry else "") \
                + (f", gusts {entry['gust_mps']:g}" if "gust_mps" in entry else "") + (f", {entry['humidity_pct']:g}% humidity" if "humidity_pct" in entry else "") \
                + (f", {entry['temperature_c']:g} °C" if "temperature_c" in entry else "") + f". {assessment['detail'].capitalize()}." \
                + (f" {smoke['detail'].capitalize()}." if smoke["state"] == "fail" else "")
            notify = severity == "warning" and operation["status"] in ops.ACTIVE_STATUSES
            row = self._observe(operation, "weather", severity, text, extra={"weather": entry, "assessment": assessment["state"], "smoke": smoke["places"]},
                                notify=notify, key="weather", title=f"Weather outside your limits at {operation['field_name']}")
            fire = self.fires.get(operation_id)
            if fire is not None and "wind_from_deg" in entry:
                fire.wind_from_deg, fire.wind_mps = entry["wind_from_deg"], entry["wind_mps"]
        return row

    def add_observation(self, operation_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        """A crew member's note, optionally with a position."""
        with self._lock:
            operation = self._editable(operation_id)
            text = ops.clean_text(payload.get("text"), 500)
            if not text:
                raise FlameError("write what was observed")
            extra: dict[str, Any] = {}
            if payload.get("lat") is not None and payload.get("lon") is not None:
                extra["position"] = fieldlib.corner_from_point(payload["lat"], payload["lon"])
            return self._observe(operation, "crew_observation", "info", f"{actor}: {text}", extra=extra, notify=False)

    def report_ignition(self, operation_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        """Records that the crew lit the field (Sentinel does not ignite anything).
        In a simulated operation the simulated fire starts where reported."""
        with self._lock:
            operation = self._editable(operation_id, ("monitoring",))
            side, where = payload.get("side"), ""
            fire = self.fires.get(operation_id)
            if side is not None:
                if side not in WATCH_SIDES:
                    raise FlameError(f"side must be one of {list(WATCH_SIDES)}")
                where = f"along the {side} edge"
                if fire is not None:
                    fire.ignite_edge(WATCH_SIDES[side])
            elif payload.get("lat") is not None and payload.get("lon") is not None:
                point = fieldlib.corner_from_point(payload["lat"], payload["lon"])
                where = f"at {point[0]:.5f}, {point[1]:.5f}"
                if fire is not None:
                    fire.ignite_point(*point)
            else:
                raise FlameError("say where the crew lit the field: side or lat/lon")
            operation["timeline"].setdefault("ignition_reported_ms", ops.now_ms())
            self._persist(operation)
            return self._observe(operation, "ignition_reported", "info", f"{actor} reported that the crew lit the field {where}.",
                                 extra={"side": side} if side else {}, notify=False)

    def start_monitoring(self, operation_id: str, actor: str, *, acknowledge_incomplete: bool = False) -> dict[str, Any]:
        with self._lock:
            operation = self._editable(operation_id, ("planning", "stood_down"))
            readiness = ops.readiness(operation, self._auto_checks(operation))
            if readiness["incomplete"] and not acknowledge_incomplete:
                labels = [ops.CHECKLIST_INDEX[i]["label"] for i in readiness["incomplete"]]
                raise FlameError(f"{len(labels)} checklist item(s) open: " + "; ".join(labels[:6])
                                 + ". Complete them, or acknowledge them to start monitoring anyway (recorded in the report).")
            operation["status"] = "monitoring"
            operation["timeline"]["monitoring_started_ms"] = operation["timeline"].get("monitoring_started_ms") or ops.now_ms()
            operation["acknowledged_incomplete"] = sorted(set(operation["acknowledged_incomplete"]) | set(readiness["incomplete"]))
            operation.setdefault("readiness_at_start", readiness)
            if operation["simulated"] and operation_id not in self.fires:
                self._start_simulation(operation)
            self._persist(operation)
            self._observe(operation, "monitoring_started", "info",
                          f"{actor} started monitoring with {readiness['done']} of {readiness['total']} checklist items recorded"
                          + (f" ({len(readiness['incomplete'])} acknowledged as open)" if readiness["incomplete"] else "") + ".", notify=False)
            self._audit(actor, "burn_monitoring_started", {"operation_id": operation_id, "open_items": readiness["incomplete"]})
        return self.operation_view(operation_id)

    def _start_simulation(self, operation: dict[str, Any]) -> None:
        field = operation.get("field_snapshot") or self.doc["fields"][operation["field_id"]]
        weather = self._latest_weather(operation) or {}
        self.fires[operation["operation_id"]] = SimulatedFireField(
            field["boundary"], fireguard_m=field.get("fireguard_m", 0.0), margin_m=field["watch_buffer_m"] + 20,
            wind_from_deg=weather.get("wind_from_deg", 270.0), wind_mps=weather.get("wind_mps", 3.0),
            ambient_c=weather.get("temperature_c", 15.0), seed=zlib.crc32(operation["operation_id"].encode()))

    def request_watch(self, operation_id: str, side: str, actor: str) -> dict[str, Any]:
        """Ask for a burn-watch flight over one side of the field. Like any
        inspection request, an operator must approve it after preflight checks."""
        with self._lock:
            operation = self._editable(operation_id, ("planning", "monitoring", "post_burn"))
            manager = self._manager(operation["drone_id"]) if operation["drone_id"] else None
            if manager is None:
                raise FlameError("no operable drone for this operation")
            field = operation.get("field_snapshot") or self.doc["fields"][operation["field_id"]]
            if side == "centre":
                lat, lon = self.field_view(field)["centre"]
            elif side in WATCH_SIDES:
                lat, lon = self._edge_point(field, WATCH_SIDES[side])
            else:
                raise FlameError(f"side must be centre or one of {list(WATCH_SIDES)}")
            request = manager.request_inspection(requested_by=actor, lat=lat, lon=lon, label=f"{field['name']}: {side} (burn watch)", source="flame",
                                                 reason=f"Burn watch for operation {operation_id}", **self._hover_argument(manager))
            self._audit(actor, "burn_watch_requested", {"operation_id": operation_id, "request_id": request.request_id, "side": side})
        return request.as_dict()

    def _hover_argument(self, manager: Any) -> dict[str, Any]:
        import inspect

        accepts = "hover_s" in inspect.signature(manager.request_inspection).parameters
        return {"hover_s": float(self.settings["watch_hover_s"])} if accepts else {}

    @staticmethod
    def _edge_point(field: dict[str, Any], side_deg: float) -> tuple[float, float]:
        """Where a ray from the centre in the side's direction crosses the boundary."""
        boundary = field["boundary"]
        origin = tuple(boundary[0])
        points = fieldlib.local(boundary, origin)
        cx = sum(p[0] for p in points) / len(points)
        cy = sum(p[1] for p in points) / len(points)
        dx, dy = math.sin(math.radians(side_deg)), math.cos(math.radians(side_deg))
        best = None
        for i, (x1, y1) in enumerate(points):
            x2, y2 = points[(i + 1) % len(points)]
            ex, ey = x2 - x1, y2 - y1
            denominator = dx * ey - dy * ex
            if abs(denominator) < 1e-9:
                continue
            t = ((x1 - cx) * ey - (y1 - cy) * ex) / denominator
            s = ((x1 - cx) * dy - (y1 - cy) * dx) / denominator
            if t > 0 and 0 <= s <= 1 and (best is None or t < best):
                best = t
        best = best or 0.0
        return offset_to_geo(origin, cx + dx * best, cy + dy * best)

    def start_post_burn(self, operation_id: str, actor: str) -> dict[str, Any]:
        with self._lock:
            operation = self._editable(operation_id, ("monitoring", "stood_down"))
            operation["status"] = "post_burn"
            operation["timeline"]["post_burn_started_ms"] = ops.now_ms()
            self._persist(operation)
            self._observe(operation, "post_burn_started", "info",
                          f"{actor} started the post-burn inspection: remaining heat above {self.settings['residual_threshold_c']:g} °C is reported.", notify=False)
        return self.operation_view(operation_id)

    def stand_down(self, operation_id: str, reason: str, actor: str) -> dict[str, Any]:
        """Emergency stop of the operation: everyone on the notification
        channels is told. Sentinel does nothing physical."""
        with self._lock:
            operation = self._editable(operation_id, ("planning", "monitoring", "post_burn"))
            reason = ops.clean_text(reason, 300) or "no reason given"
            operation["status"] = "stood_down"
            operation["stand_down"] = {"by": actor, "ms": ops.now_ms(), "reason": reason}
            self._persist(operation)
            self._observe(operation, "stood_down", "critical", f"{actor} stood down the operation: {reason}", key="stand_down",
                          title=f"Burn operation stood down: {operation['field_name']}")
        return self.operation_view(operation_id)

    def emergency_alert(self, operation_id: str, text: str, actor: str) -> dict[str, Any]:
        """A crew member's emergency message to everyone on the notification channels."""
        with self._lock:
            operation = self._editable(operation_id)
            text = ops.clean_text(text, 300)
            if not text:
                raise FlameError("write the message")
            return self._observe(operation, "emergency_alert", "critical", f"{actor}: {text}", key=f"alert:{ops.now_ms()}",
                                 title=f"Emergency at {operation['field_name']}")

    def close_operation(self, operation_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        with self._lock:
            operation = self._editable(operation_id)
            result = payload.get("result")
            if operation["status"] in ("post_burn", "stood_down", "monitoring"):
                if result not in ("no_heat_observed", "heat_remains", "not_inspected"):
                    raise FlameError("record the post-burn result: no_heat_observed, heat_remains or not_inspected")
            operation["post_burn"] = {"result": result or "not_burned", "by": actor, "ms": ops.now_ms(), "note": ops.clean_text(payload.get("note"), 500)}
            operation["status"] = "closed"
            operation["timeline"]["closed_ms"] = ops.now_ms()
            text = {"no_heat_observed": "no remaining heat was observed in the areas the thermal camera covered",
                    "heat_remains": "heat remains: the crew keeps watching on the ground",
                    "not_inspected": "no post-burn inspection was done",
                    "not_burned": "the field was not burned"}[operation["post_burn"]["result"]]
            self._observe(operation, "closed", "info", f"{actor} closed the operation: {text}.", notify=False)
            report = build_report(self, operation)
            operation["report_fingerprint"] = report["fingerprint"]
            self._persist(operation)
            self._audit(actor, "burn_operation_closed", {"operation_id": operation_id, "result": operation["post_burn"]["result"],
                                                         "fingerprint": report["fingerprint"]})
        return self.operation_view(operation_id)

    def report(self, operation_id: str) -> dict[str, Any]:
        return build_report(self, self._operation(operation_id))

    # ---- simulation controls --------------------------------------------------------------

    def simulate(self, operation_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        with self._lock:
            operation = self._operation(operation_id)
            fire = self.fires.get(operation_id)
            if not operation["simulated"] or fire is None:
                raise FlameError("simulation controls exist only for a simulated operation that is being monitored")
            action = payload.get("action")
            state = self.runtime.setdefault(operation_id, {})
            if action == "spot_fire":
                side = payload.get("side", "north-east")
                if side not in WATCH_SIDES:
                    raise FlameError(f"side must be one of {list(WATCH_SIDES)}")
                distance = float(payload.get("distance_m", 25))
                field = operation.get("field_snapshot") or self.doc["fields"][operation["field_id"]]
                lat, lon = self._edge_point(field, WATCH_SIDES[side])
                origin = tuple(field["boundary"][0])
                from ..drone.base import geo_to_offset

                x, y = geo_to_offset(origin, lat, lon)
                points = fieldlib.local(field["boundary"], origin)
                cx = sum(p[0] for p in points) / len(points)
                cy = sum(p[1] for p in points) / len(points)
                length = math.hypot(x - cx, y - cy) or 1.0
                spot = offset_to_geo(origin, x + (x - cx) / length * distance, y + (y - cy) / length * distance)
                fire.ignite_point(*spot, radius_m=4.0)
                detail = f"simulated spot fire {distance:g} m outside the {side} edge"
            elif action == "thermal_fault":
                state["thermal_fault"] = bool(payload.get("on", True))
                detail = f"simulated thermal camera fault {'on' if state['thermal_fault'] else 'off'}"
            elif action == "wind":
                fire.wind_from_deg = float(payload.get("from_deg", fire.wind_from_deg)) % 360
                fire.wind_mps = max(0.0, min(30.0, float(payload.get("mps", fire.wind_mps))))
                detail = f"simulated wind from {fire.wind_from_deg:.0f}° at {fire.wind_mps:g} m/s"
            elif action == "advance":
                seconds = max(1.0, min(1800.0, float(payload.get("seconds", 60))))
                for _ in range(int(seconds // 5)):
                    fire.step(5.0)
                detail = f"simulated fire advanced {seconds:g} s"
            else:
                raise FlameError("action must be spot_fire, thermal_fault, wind or advance")
            self._audit(actor, "flame_simulation", {"operation_id": operation_id, "action": action})
            self._observe(operation, "simulation", "info", f"Simulation: {detail} (by {actor}).", notify=False)
        return self.operation_view(operation_id)

    # ---- monitoring loop -------------------------------------------------------------------

    def step(self, dt_s: float, now_ms: int | None = None) -> None:
        now_ms = now_ms or ops.now_ms()
        with self._lock:
            self.apply_hooks()
            self._step_accessories(dt_s, now_ms)
            self._check_accessory_faults(now_ms)
            for operation in list(self.operations.values()):
                if operation["status"] in ops.ACTIVE_STATUSES:
                    self._monitor(operation, dt_s, now_ms)

    def _step_accessories(self, dt_s: float, now_ms: int) -> None:
        for device in self.accessory_devices():
            sim = self._sim(device)
            if sim is None:
                continue
            exposure = 0.0
            for operation in self._active_for_drone(device.get("drone_id", "")):
                fire = self.fires.get(operation["operation_id"])
                manager = self._manager(operation["drone_id"])
                if fire is None or manager is None:
                    continue
                telemetry = manager.adapter.telemetry()
                if telemetry.in_air and telemetry.lat is not None:
                    x, y = fire.to_local(telemetry.lat, telemetry.lon)
                    near_m2 = fire.cell_m ** 2 * sum(1 for i in fire.burning if math.hypot(fire.center(i)[0] - x, fire.center(i)[1] - y) < 30)
                    # Rough stand-in for radiant and plume heating: grows with flaming area and falls with height squared.
                    exposure = max(exposure, min(80.0, near_m2 / max(telemetry.alt_m, 5.0) ** 2 * 10))
            sim.step(dt_s, now_ms, exposure)

    def _monitor(self, operation: dict[str, Any], dt_s: float, now_ms: int) -> None:
        operation_id = operation["operation_id"]
        state = self.runtime.setdefault(operation_id, {})
        fire = self.fires.get(operation_id)
        if fire is not None:
            speed = float(self.settings["simulation_speed"])
            fire.step(dt_s * speed)
        manager = self._manager(operation["drone_id"]) if operation["drone_id"] else None
        if manager is None:
            state["thermal"] = {"state": "no_drone", "detail": "no operable drone for this operation"}
            return
        telemetry = manager.adapter.telemetry()
        self._mirror_drone(operation, manager, telemetry, state)
        if fire is None:
            state["thermal"] = {"state": "unavailable", "detail": REAL_THERMAL_STATUS}
            return
        if state.get("thermal_fault"):
            self._thermal_state(operation, state, "fault", "simulated thermal camera fault: no thermal data", now_ms)
            return
        device = self.accessory_for_drone(operation["drone_id"])
        if self.configuration(operation["drone_id"]) != "agricultural_monitoring" or device is None:
            self._thermal_state(operation, state, "not_configured", "drone not in its agricultural configuration", now_ms)
            return
        self._simulated_camera_tilt(manager, state)
        threshold = self.settings["residual_threshold_c"] if operation["status"] == "post_burn" else self.settings["hotspot_threshold_c"]
        self.observer.threshold_c = threshold
        self.observer.camera.default_pitch_deg = float(self.settings["watch_camera_pitch_deg"])
        look = self.observer.look(fire, telemetry, now_ms)
        if not look.ok:
            self._thermal_state(operation, state, "idle", look.reason, now_ms)
            return
        self._thermal_state(operation, state, "observing", f"thermal camera covering about {look.footprint_m2:.0f} m²", now_ms)
        operation["counts"]["looks"] += 1
        seen = state.setdefault("seen_ms", {})
        for index in look.observed:
            seen[index] = now_ms
        field = operation.get("field_snapshot") or self.doc["fields"][operation["field_id"]]
        for hotspot in look.hotspots:
            self._record_hotspot(operation, field, fire, hotspot, now_ms, state)
        if now_ms - state.get("summary_ms", 0) >= 60_000:
            state["summary_ms"] = now_ms
            coverage = self._coverage(operation_id)
            self._observe(operation, "watch_summary", "info",
                          f"Thermal watch: {coverage['edge_pct']:g}% of the boundary edge and {coverage['strip_pct']:g}% of the watch strip "
                          f"observed in the last {coverage['window_min']:g} min.", extra={"coverage": coverage}, notify=False)

    def _simulated_camera_tilt(self, manager: Any, state: dict[str, Any]) -> None:
        """In the built-in simulator, stand in for the pilot tilting the camera
        toward the field edge during a burn watch, and back afterwards."""
        adapter = manager.adapter
        if adapter.kind != "simulated" or not hasattr(adapter, "gimbal_pitch_deg"):
            return
        active = manager.active_inspection()
        watching = bool(active and active.get("source") == "flame")
        if "sim_gimbal_base" not in state:
            state["sim_gimbal_base"] = adapter.gimbal_pitch_deg
        adapter.gimbal_pitch_deg = float(self.settings["watch_camera_pitch_deg"]) if watching else state["sim_gimbal_base"]

    def _thermal_state(self, operation: dict[str, Any], state: dict[str, Any], kind: str, detail: str, now_ms: int) -> None:
        previous = state.get("thermal", {}).get("state")
        state["thermal"] = {"state": kind, "detail": detail}
        if kind == "fault" and previous != "fault":
            self._observe(operation, "thermal_feed_lost", "warning", f"Thermal data lost: {detail}. Keep watching the fire line from the ground.",
                          key="thermal_fault", title=f"Thermal watch lost at {operation['field_name']}")
        if kind == "observing" and previous == "fault":
            self._observe(operation, "thermal_feed_restored", "info", "Thermal data restored.", notify=False)

    def _record_hotspot(self, operation: dict[str, Any], field: dict[str, Any], fire: SimulatedFireField, hotspot: Any, now_ms: int,
                        state: dict[str, Any]) -> None:
        # The patch's most outward cell decides: heat is outside if any of it is.
        position = ops.classify(max(fire.signed_m[i] for i in hotspot.cells), hotspot.uncertainty_m, field)
        side = fieldlib.nearest_edge_label(hotspot.local, fire.polygon)
        place_key = f"{int(hotspot.local[0] // 25)}:{int(hotspot.local[1] // 25)}"
        temperature = f"≥ {hotspot.max_c:.0f} °C (sensor maximum)" if hotspot.saturated else f"about {hotspot.max_c:.0f} °C"
        where = (f"±{hotspot.uncertainty_m:g} m" if hotspot.uncertainty_m else "position uncertainty unknown")
        extra = {"hotspot": hotspot.as_dict(), "zone": position["zone"], "distance_m": position["distance_m"], "side": side, "place_key": place_key}
        sensitive = self._near_sensitive(field, hotspot)
        if operation["status"] == "post_burn":
            self._observe(operation, "residual_heat", "warning",
                          f"Remaining heat {temperature} on the {side} side of {field['name']} ({where}), {hotspot.area_m2:.0f} m².",
                          extra=extra, key=f"residual:{place_key}", title=f"Remaining heat at {field['name']}", cooldown_only=True)
            return
        if position["zone"] == "outside":
            operation["counts"]["hotspots_outside"] += 1
            self._observe(operation, "heat_outside_boundary", "critical",
                          f"Heat {temperature} about {position['distance_m']:.0f} m outside the {side} boundary of {field['name']} ({where}), "
                          f"{hotspot.area_m2:.0f} m². Check it now.", extra=extra, key=f"outside:{place_key}",
                          title=f"Heat outside the {field['name']} boundary")
        elif position["zone"] == "edge":
            operation["counts"]["hotspots_edge"] += 1
            self._observe(operation, "heat_at_boundary", "warning",
                          f"Heat {temperature} at the {side} boundary of {field['name']} ({position['distance_m']:+.0f} m from the edge, {where}).",
                          extra=extra, key=f"edge:{place_key}", title=f"Heat at the {field['name']} boundary")
        else:
            last = state.setdefault("interior_ms", {})
            if now_ms - last.get(place_key, 0) >= 120_000:
                last[place_key] = now_ms
                self._observe(operation, "hotspot", "info", f"Burning inside {field['name']} on the {side} side ({temperature}).", extra=extra, notify=False)
        if sensitive:
            self._observe(operation, "heat_near_sensitive_place", "critical" if position["zone"] == "outside" else "warning",
                          f"Heat {temperature} about {sensitive['distance_m']:.0f} m from {sensitive['label']}.", extra=extra,
                          key=f"sensitive:{sensitive['label']}", title=f"Heat near {sensitive['label']}")

    @staticmethod
    def _near_sensitive(field: dict[str, Any], hotspot: Any, within_m: float = 100.0) -> dict[str, Any] | None:
        best = None
        for place in field.get("sensitive", []):
            distance = distance_m((hotspot.lat, hotspot.lon), tuple(place["point"]))
            if distance <= within_m and (best is None or distance < best["distance_m"]):
                best = {"label": place["label"], "distance_m": round(distance)}
        return best

    def _coverage(self, operation_id: str) -> dict[str, Any]:
        fire = self.fires.get(operation_id)
        window_s = float(self.settings["coverage_window_s"])
        empty = {"edge_pct": 0.0, "strip_pct": 0.0, "window_min": window_s / 60, "available": False}
        if fire is None:
            return empty
        operation = self._operation(operation_id)
        field = operation.get("field_snapshot") or self.doc["fields"][operation["field_id"]]
        seen = self.runtime.get(operation_id, {}).get("seen_ms", {})
        cutoff = ops.now_ms() - window_s * 1000
        edge = [i for i, s in enumerate(fire.signed_m) if abs(s) <= field["edge_m"]]
        strip = [i for i, s in enumerate(fire.signed_m) if field["edge_m"] < s <= field["watch_buffer_m"]]

        def share(cells: list[int]) -> float:
            return round(100 * sum(1 for i in cells if seen.get(i, 0) >= cutoff) / len(cells), 1) if cells else 0.0

        return {"edge_pct": share(edge), "strip_pct": share(strip), "window_min": window_s / 60, "available": True}

    def _mirror_drone(self, operation: dict[str, Any], manager: Any, telemetry: Any, state: dict[str, Any]) -> None:
        """Drone problems during a burn reach the crew: link loss, battery and
        geofence advisories. The flight controller's failsafes act on their own."""
        safety = manager.safety_state
        previous = state.get("drone_safety", "ok")
        if safety != previous:
            state["drone_safety"] = safety
            if safety != "ok":
                text = {"link_lost": "Drone telemetry lost: its flight controller's failsafe returns it home. Thermal watch has stopped; keep watching the fire line from the ground.",
                        "low_battery_return": "Drone battery low: it is returning home. Thermal watch pauses until a fresh battery is fitted.",
                        "critical_battery_land": "Drone battery critical: it is landing. Thermal watch has stopped.",
                        "geofence_breach": "Drone outside its approved area: it is returning home."}.get(safety, f"Drone safety state: {safety}.")
                self._observe(operation, "drone_safety", "critical" if safety in ("link_lost", "critical_battery_land") else "warning", text,
                              extra={"safety_state": safety}, key=f"drone:{safety}", title=f"Drone problem during the {operation['field_name']} burn")
            else:
                self._observe(operation, "drone_safety", "info", "Drone safety state back to normal.", notify=False)
        if telemetry.in_air and telemetry.wind_mps is not None and ops.now_ms() - state.get("wind_ms", 0) >= 300_000:
            state["wind_ms"] = ops.now_ms()
            self._observe(operation, "drone_wind", "info", f"Flight controller's wind estimate at {telemetry.alt_m:.0f} m: {telemetry.wind_mps:.1f} m/s.",
                          extra={"wind_mps": telemetry.wind_mps, "alt_m": telemetry.alt_m}, notify=False)

    # ---- observations and notifications ---------------------------------------------------

    def _observe(self, operation: dict[str, Any], kind: str, severity: str, text: str, *, extra: dict[str, Any] | None = None, notify: bool = True,
                 key: str = "", title: str = "", cooldown_only: bool = False) -> dict[str, Any]:
        now = ops.now_ms()
        state = self.runtime.setdefault(operation["operation_id"], {})
        sent = state.setdefault("notified", {})
        if key and now - sent.get(key, 0) < self.settings["notify_cooldown_s"] * 1000 and (notify or cooldown_only):
            return {}
        if key:
            sent[key] = now
        simulated = operation.get("simulated", False)
        row = {"observation_id": ops.new_id("obs"), "operation_id": operation["operation_id"], "recorded_ms": now, "kind": kind,
               "severity": severity, "text": text, "simulated": simulated} | (extra or {})
        row["recorded"] = self._record_write(self.store.add_burn_observation, row)
        self._publish("observation", row)
        if notify and severity in ("warning", "critical"):
            operation["counts"]["notifications"] += 1
            self._record_write(self.store.upsert_burn_operation, operation)
            self._notify(operation, severity, title or text[:80], text)
        return row

    def _record_write(self, write: Any, *args: Any) -> bool:
        """A failed record must not prevent an observed emergency reaching people."""
        try:
            write(*args)
            return True
        except (OSError, sqlite3.Error):
            self.record_failures += 1
            if self.record_failures == 1:
                log.exception("Flame Mode record write failed; observation history may be incomplete")
            try:
                self.service.runtime.health.set_component("flame_records", "degraded", "Some Flame Mode records could not be saved; history may be incomplete.", ops.now_ms())
            except (OSError, sqlite3.Error):
                pass  # the component is visible in memory even if its own record failed
            return False

    def _notify(self, operation: dict[str, Any], severity: str, title: str, text: str) -> None:
        prefix = "[SIMULATED] " if operation.get("simulated") else ""
        notification = Notification(
            incident_id=operation["operation_id"], severity="high" if severity == "critical" else "medium", title=prefix + title,
            message=f"{prefix}{text}\n{NOTIFY_DISCLAIMER}", priority=SEVERITY_PRIORITY[severity], camera_ids=(), timestamp_ms=ops.now_ms(), kind="flame")
        runtime = self.service.runtime
        runtime.bus.publish("notification", notification.as_dict())
        self._record_write(runtime.store.audit, "system", "notification", notification.as_dict())
        # Burn emergencies reach people regardless of the security arm mode, like coverage-loss notifications.
        runtime.dispatcher.submit(notification)

    # ---- overview ---------------------------------------------------------------------------

    def overview(self) -> dict[str, Any]:
        drones = []
        for drone_id, device in self._drone_devices().items():
            manager = self._manager(drone_id)
            if manager is None:
                continue
            drones.append(self.configuration_view(drone_id) | {"name": device.get("name", drone_id), "model": manager._flame_base_model["name"]
                                                                if getattr(manager, "_flame_base_model", None) else manager.model["name"],
                                                                "simulated": not manager.adapter.is_physical, "primary": drone_id == self.service.primary_drone})
        return {
            "enabled": self.enabled(), "settings": self.settings, "drones": drones, "accessories": self.accessories(), "fields": self.fields(),
            "recording": {"failures": self.record_failures, "history_incomplete": self.record_failures > 0},
            "operations": self.operations_list(20), "thermal_camera": THERMAL_CAMERAS[DEFAULT_THERMAL] | {"name": DEFAULT_THERMAL},
            "real_thermal": REAL_THERMAL_STATUS, "disclaimer": ops.DISCLAIMER, "roles": ops.ROLES, "qualification_kinds": ops.QUALIFICATION_KINDS,
            "authorization_kinds": ops.AUTHORIZATION_KINDS, "checklist": ops.CHECKLIST, "sensitive_kinds": fieldlib.SENSITIVE_KINDS,
            "qualifications": self.ag_records("qualification"), "authorizations": self.ag_records("authorization"),
            "watch_sides": ["centre", *WATCH_SIDES],
        }


__all__ = ["DEFAULT_SETTINGS", "FlameError", "FlameService", "NOTIFY_DISCLAIMER", "WATCH_SIDES"]
