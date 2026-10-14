from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .ardupilot import RETURN_MODES, audit_parameters, flight_state
from .base import DroneAdapter, FlightCommandRejected, distance_m, map_point_to_geo
from .geofence import Geofence
from .mission import Route, plan_route, to_qgc_plan, to_waypoints_text

OPEN_REQUEST_STATES = {"pending_approval", "approved", "in_progress"}
FINAL_STATES = {"denied", "expired", "completed", "aborted", "interrupted", "not_flown"}
MAINTENANCE_KINDS = ("inspection", "repair", "battery", "propellers", "firmware", "calibration", "commissioning", "note")


@dataclass
class InspectionRequest:
    request_id: str
    created_ms: int
    source: str  # operator | incident
    target_lat: float
    target_lon: float
    target_label: str
    requested_by: str
    incident_id: str = ""
    # pending_approval | denied | expired | approved (waiting for the pilot) | in_progress | completed | aborted | interrupted | not_flown
    status: str = "pending_approval"
    decided_by: str = ""
    decided_ms: int | None = None
    phase: str = ""
    note: str = ""
    drone_id: str = ""
    reason: str = ""  # what was observed that led to the request (behaviour, never intent)
    mode: str = ""  # command (Sentinel flies a simulator) | propose (the pilot flies the exported route); fixed at approval
    route: dict[str, Any] = field(default_factory=dict)
    approved_until_ms: int | None = None
    started_ms: int | None = None
    reached_target_ms: int | None = None
    ended_ms: int | None = None
    flight_id: str = ""
    observations: dict[str, Any] = field(default_factory=dict)
    evidence_paths: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DroneSafetyConfig:
    min_launch_battery_pct: float = 60.0
    rth_battery_pct: float = 30.0
    land_battery_pct: float = 15.0
    inspection_altitude_m: float = 15.0
    safe_transit_altitude_m: float = 0.0  # surveyed height above all obstacles; 0 = the inspection altitude
    hover_s: float = 20.0
    request_ttl_s: float = 300.0
    approval_window_s: float = 900.0  # an approved (pilot-flown) inspection must start within this time
    arrival_radius_m: float = 3.0
    link_lost_s: float = 3.0
    max_hdop: float = 2.0
    min_satellites: int = 6
    inspect_every_flights: int = 20  # maintenance reminder

    @classmethod
    def from_dict(cls, data: dict | None) -> "DroneSafetyConfig":
        data = data or {}
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})

    @property
    def transit_altitude_m(self) -> float:
        return max(self.safe_transit_altitude_m or self.inspection_altitude_m, self.inspection_altitude_m)


@dataclass
class WeatherLimits:
    require_attestation: bool | None = None  # None: required for anything but the built-in simulator
    max_wind_mps: float = 8.0
    min_temperature_c: float = -5.0
    max_temperature_c: float = 40.0
    conditions_ttl_s: float = 3600.0

    @classmethod
    def from_dict(cls, data: dict | None) -> "WeatherLimits":
        data = data or {}
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


class _NoRecords:
    def __getattr__(self, name: str) -> Callable[..., Any]:
        return lambda *args, **kwargs: []


class DroneManager:
    """Sentinel's side of drone operations: operator-approved inspections,
    readiness checks, a safety monitor, flight and maintenance logs.

    Two operating modes, fixed per aircraft by its adapter:
    - `propose` (any real or unknown aircraft): approval exports a route for
      the pilot's own ground station; Sentinel follows the flight from
      telemetry and never sends a flight command.
    - `command` (simulators only: the built-in simulator, or ArduPilot SITL that
      proved it is a simulator): Sentinel flies the approved route itself.
    Nothing flies without an operator approving it, and the flight controller
    remains responsible for flight safety either way."""

    def __init__(
        self,
        adapter: DroneAdapter,
        geofence: Geofence,
        site_origin: tuple[float, float],
        safety: DroneSafetyConfig | None = None,
        *,
        audit: Callable[[str, str, dict[str, Any]], None] = lambda actor, action, body: None,
        on_change: Callable[[dict[str, Any]], None] = lambda status: None,
        drone_id: str = "drone",
        home: tuple[float, float] | None = None,
        records: Any = None,
        weather: WeatherLimits | None = None,
        firmware_tested: tuple[str, ...] = (),
    ) -> None:
        self.adapter = adapter
        self.geofence = geofence
        self.site_origin = site_origin
        self.safety = safety or DroneSafetyConfig()
        self.audit = audit
        self.on_change = on_change
        self.drone_id = drone_id
        self.home = home or site_origin
        self.records = records if records is not None else _NoRecords()
        self.weather = weather or WeatherLimits()
        self.firmware_tested = tuple(firmware_tested)
        self.companion_status: Callable[[], dict[str, Any] | None] = lambda: None  # set by the service
        self.video_status: Callable[[], dict[str, Any] | None] = lambda: None
        self.on_inspection_finished: Callable[[dict[str, Any]], None] = lambda request: None
        self.requests: dict[str, InspectionRequest] = {}
        self.safety_state = "ok"
        self.safety_notes: list[str] = []
        self.advisories: list[dict[str, Any]] = []
        self.conditions: dict[str, Any] | None = None
        self.flight_state = "offline"
        self._flight: dict[str, Any] | None = None
        self._flight_saved_ms = 0
        self._mission: dict[str, Any] | None = None
        self._seq = 0
        self._lock = threading.RLock()
        self._restore()

    # ---- modes and readiness ---------------------------------------------

    def mode(self) -> tuple[str, str]:
        allowed, why = self.adapter.commands_allowed()
        return ("command", why) if allowed else ("propose", why)

    def readiness(self) -> dict[str, Any]:
        with self._lock:
            telemetry = self.adapter.telemetry()
            checks = []

            def add(check_id: str, label: str, state: str, detail: str, blocker: str = "") -> None:
                checks.append({"id": check_id, "label": label, "state": state, "detail": detail, "blocker": blocker if state == "fail" else ""})

            linked = telemetry.connected and telemetry.link_ok
            add("link", "Telemetry link", "pass" if linked else "fail", "receiving telemetry" if linked else "no telemetry from the aircraft", "no_link")
            battery = telemetry.battery_pct
            add("battery", "Battery", "pass" if battery is not None and battery >= self.safety.min_launch_battery_pct else "fail",
                f"{battery:.0f}% (minimum {self.safety.min_launch_battery_pct:g}%)" if battery is not None else "not reported", "battery_below_launch_minimum")
            if telemetry.gps_fix < 3:
                add("gps", "GPS", "fail", "no 3D fix", "no_3d_gps_fix")
            elif (telemetry.hdop is not None and telemetry.hdop > self.safety.max_hdop) or (self.adapter.is_physical and telemetry.satellites < self.safety.min_satellites):
                add("gps", "GPS", "fail", f"poor accuracy: HDOP {telemetry.hdop}, {telemetry.satellites} satellites", "gps_poor_accuracy")
            else:
                add("gps", "GPS", "pass", f"3D fix, {telemetry.satellites} satellites" + (f", HDOP {telemetry.hdop:g}" if telemetry.hdop is not None else ""))
            if telemetry.ekf_ok is None:
                add("ekf", "Navigation (EKF)", "na", "not reported by this aircraft")
            else:
                add("ekf", "Navigation (EKF)", "pass" if telemetry.ekf_ok else "fail", "position estimate healthy" if telemetry.ekf_ok else "position estimate not ready", "ekf_not_ready")
            if telemetry.prearm_ok is None:
                add("prearm", "Pre-arm checks", "na", "not reported by this aircraft")
            else:
                latest = next((m for m in reversed(telemetry.messages) if m.lower().startswith("prearm")), "")
                add("prearm", "Pre-arm checks", "pass" if telemetry.prearm_ok else "fail", "passing" if telemetry.prearm_ok else (latest or "failing"), "prearm_checks_failing")
            flying = telemetry.in_air or self._mission is not None
            add("on_ground", "On the ground, no inspection in progress", "fail" if flying else "pass",
                "airborne" if telemetry.in_air else "an inspection is approved or in progress" if flying else "ready", "already_flying")
            add("safety", "Safety monitor", "pass" if self.safety_state == "ok" else "fail", self.safety_state, f"safety_{self.safety_state}")
            home_problems = self.geofence.violations(*self.current_home())
            add("home_fence", "Home inside the geofence", "fail" if home_problems else "pass", ", ".join(home_problems) or "inside", "home_outside_geofence")
            if telemetry.lat is not None and not telemetry.in_air:
                offset = distance_m((telemetry.lat, telemetry.lon), self.home)
                add("launch_point", "At the planned launch point", "pass" if offset <= 10 else "warn",
                    f"{offset:.0f} m from the configured home" + ("" if offset <= 10 else ": routes start where the aircraft is now; check the take-off area"))
            audit = self.parameter_audit()
            if audit is None:
                add("fc_settings", "Failsafe and fence settings", "na", "simulator without flight-controller parameters")
            else:
                detail = "all required failsafes and fences set" if audit["ok"] else "; ".join(f"{f['param']}: {f['actual']} (needs {f['expected']})" for f in audit["blocking"][:4])
                add("fc_settings", "Failsafe and fence settings", "pass" if audit["ok"] else "fail", detail, "flight_controller_settings")
            self._conditions_check(add)
            companion = self.companion_status()
            if companion is None:
                add("companion", "Companion computer", "warn" if self.adapter.is_physical else "na",
                    "none paired: telemetry comes straight from the flight-controller link" if self.adapter.is_physical else "not used by this simulator")
            else:
                reporting = companion.get("state") == "reporting"
                add("companion", "Companion computer", "pass" if reporting else "fail",
                    f"{companion.get('name', '')} reporting" if reporting else f"{companion.get('name', '')} silent", "companion_silent")
            video = self.video_status()
            if video is None:
                add("video", "Drone video", "warn", "no drone video configured: inspections will have telemetry only")
            else:
                add("video", "Drone video", "pass" if video.get("state") in ("ready", "streaming", "idle") else "warn", video.get("detail", video.get("state", "")))
            version = telemetry.autopilot_version
            if self.firmware_tested and version:
                tested = any(version.startswith(prefix) for prefix in self.firmware_tested)
                add("firmware", "Firmware version", "pass" if tested else "warn",
                    f"{version}" + ("" if tested else f" (tested with {', '.join(self.firmware_tested)})"))
            maintenance = self.maintenance_summary()
            due = maintenance["inspection_due"]
            add("maintenance", "Maintenance", "warn" if due else "pass",
                f"{maintenance['flights_since_inspection']} flight(s) since the last recorded inspection" + (" (inspection due)" if due else ""))
            blockers = [check["blocker"] for check in checks if check["state"] == "fail"]
            return {"ready": not blockers, "checks": checks, "blockers": blockers, "mode": self.mode()[0]}

    def _conditions_check(self, add: Callable[..., None]) -> None:
        required = self.weather.require_attestation
        if required is None:
            required = self.adapter.kind != "simulated"
        if not required:
            add("conditions", "Weather and conditions", "na", "not required for the built-in simulator")
            return
        now = int(time.time() * 1000)
        conditions = self.conditions
        if conditions is None or now - conditions["recorded_ms"] > self.weather.conditions_ttl_s * 1000:
            add("conditions", "Weather and conditions", "fail", "not confirmed by the operator in the last hour", "conditions_not_confirmed")
            return
        problems = []
        if conditions.get("precipitation"):
            problems.append("precipitation")
        if not conditions.get("visibility_ok", False):
            problems.append("poor visibility")
        wind = conditions.get("wind_mps")
        if wind is not None and wind > self.weather.max_wind_mps:
            problems.append(f"wind {wind:g} m/s > {self.weather.max_wind_mps:g}")
        temperature = conditions.get("temperature_c")
        if temperature is not None and not self.weather.min_temperature_c <= temperature <= self.weather.max_temperature_c:
            problems.append(f"temperature {temperature:g} °C outside {self.weather.min_temperature_c:g}..{self.weather.max_temperature_c:g}")
        add("conditions", "Weather and conditions", "fail" if problems else "pass",
            ", ".join(problems) if problems else f"confirmed by {conditions['by']}", "conditions_outside_limits")

    def preflight_blockers(self) -> list[str]:
        return self.readiness()["blockers"]

    def parameter_audit(self) -> dict[str, Any] | None:
        if self.adapter.flight_controller != "ardupilot":
            return None
        baseline = self.commissioning_baseline()
        return audit_parameters(
            self.adapter.parameters(), max_altitude_m=self.geofence.max_altitude_m, safe_transit_altitude_m=self.safety.transit_altitude_m,
            fence_polygon=self.adapter.fence_polygon(), geofence_polygon=self.geofence.polygon, baseline=(baseline or {}).get("parameters"),
        )

    def set_conditions(self, operator: str, *, precipitation: bool, visibility_ok: bool, wind_mps: float | None = None,
                       temperature_c: float | None = None, note: str = "") -> dict[str, Any]:
        with self._lock:
            self.conditions = {"recorded_ms": int(time.time() * 1000), "by": operator, "precipitation": precipitation, "visibility_ok": visibility_ok,
                               "wind_mps": wind_mps, "temperature_c": temperature_c, "note": note[:300]}
            self.audit(operator, "drone_conditions_confirmed", self.conditions | {"drone": self.drone_id})
            self._changed()
            return dict(self.conditions)

    def current_home(self) -> tuple[float, float]:
        telemetry = self.adapter.telemetry()
        if telemetry.home_lat is not None and telemetry.home_lon is not None:
            return telemetry.home_lat, telemetry.home_lon
        return self.home

    # ---- inspection workflow --------------------------------------------

    def request_inspection(
        self,
        *,
        requested_by: str,
        lat: float | None = None,
        lon: float | None = None,
        map_point: tuple[float, float] | None = None,
        label: str = "",
        source: str = "operator",
        incident_id: str = "",
        reason: str = "",
    ) -> InspectionRequest:
        with self._lock:
            if map_point is not None:
                lat, lon = map_point_to_geo(self.site_origin, *map_point)
            if lat is None or lon is None:
                raise ValueError("inspection target needs lat/lon or a map point")
            problems = self.geofence.violations(lat, lon, self.safety.transit_altitude_m)
            if problems:
                raise FlightCommandRejected(f"target rejected: {', '.join(problems)}")
            for existing in self.requests.values():
                if existing.status in OPEN_REQUEST_STATES and incident_id and existing.incident_id == incident_id:
                    return existing
            self._seq += 1
            now = int(time.time() * 1000)
            request = InspectionRequest(
                request_id=f"insp_{now}_{self._seq}", created_ms=now, source=source, target_lat=lat, target_lon=lon,
                target_label=label, requested_by=requested_by, incident_id=incident_id, drone_id=self.drone_id, reason=reason[:300],
            )
            self.requests[request.request_id] = request
            self.audit(requested_by, "drone_inspection_requested", request.as_dict())
            self._save(request)
            self._changed()
            return request

    def plan(self, request: InspectionRequest) -> Route:
        return plan_route(self.current_home(), (request.target_lat, request.target_lon), self.geofence,
                          safe_transit_altitude_m=self.safety.transit_altitude_m, inspection_altitude_m=self.safety.inspection_altitude_m,
                          hover_s=self.safety.hover_s, label=request.target_label)

    def approve(self, request_id: str, operator: str) -> InspectionRequest:
        with self._lock:
            request = self._pending(request_id)
            blockers = self.preflight_blockers()
            if blockers:
                raise FlightCommandRejected(f"preflight blocked: {', '.join(blockers)}")
            route = self.plan(request)
            if route.problems:
                raise FlightCommandRejected(f"route rejected: {', '.join(route.problems)}")
            mode, _ = self.mode()
            now = int(time.time() * 1000)
            request.route, request.mode, request.decided_by, request.decided_ms = route.as_dict(), mode, operator, now
            if mode == "command":
                try:
                    self.adapter.takeoff(route.transit_alt_m)
                except FlightCommandRejected:
                    request.route, request.mode, request.decided_by, request.decided_ms = {}, "", "", None
                    raise
                request.status, request.phase, request.started_ms = "in_progress", "takeoff", now
            else:
                request.status, request.phase = "approved", "awaiting_pilot"
                request.approved_until_ms = now + int(self.safety.approval_window_s * 1000)
            self._mission = {"request_id": request_id, "hover_started": None}
            self.audit(operator, "drone_inspection_approved", request.as_dict())
            self._save(request)
            self._changed()
            return request

    def deny(self, request_id: str, operator: str, note: str = "") -> InspectionRequest:
        with self._lock:
            request = self._pending(request_id)
            request.status = "denied"
            request.decided_by = operator
            request.decided_ms = int(time.time() * 1000)
            request.note = note
            self.audit(operator, "drone_inspection_denied", request.as_dict())
            self._save(request)
            self._changed()
            return request

    def mission_export(self, request_id: str, fmt: str = "plan") -> tuple[str, str]:
        """(filename, content) for the pilot's ground station. Pending requests
        get a preview of the route that approval would produce."""
        with self._lock:
            request = self.requests.get(request_id)
            if request is None:
                raise KeyError(request_id)
            route = Route(**{k: (tuple(v) if k in ("home", "target") else v) for k, v in request.route.items()}) if request.route else self.plan(request)
            if fmt == "waypoints":
                return f"{request_id}.waypoints", to_waypoints_text(route)
            plan = to_qgc_plan(route, self.geofence, request_id=request_id)
            plan["sentinel"] |= {"status": request.status, "approved_by": request.decided_by, "problems": route.problems, "notes": route.notes}
            return f"{request_id}.plan", json.dumps(plan, indent=2)

    def active_inspection(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._mission:
                return None
            request = self.requests[self._mission["request_id"]]
            return request.as_dict() if request.status == "in_progress" else None

    def attach_observations(self, request_id: str, summary: dict[str, Any], evidence_paths: list[str]) -> None:
        with self._lock:
            request = self.requests.get(request_id)
            if request is None:
                return
            request.observations = summary
            request.evidence_paths = sorted(set(request.evidence_paths) | set(evidence_paths))
            self._save(request)
            self._changed()

    def inspection(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            request = self.requests.get(request_id)
        if request is not None:
            return request.as_dict()
        rows = [row for row in self.records.list_drone_inspections(drone_id=self.drone_id, limit=500) if row["request_id"] == request_id]
        return rows[0] if rows else None

    # ---- manual commands -------------------------------------------------

    def command(self, action: str, operator: str, params: dict[str, Any] | None = None) -> None:
        params = params or {}
        with self._lock:
            if action == "takeoff":
                blockers = self.preflight_blockers()
                if blockers:
                    raise FlightCommandRejected(f"preflight blocked: {', '.join(blockers)}")
                altitude = float(params.get("altitude_m", self.safety.transit_altitude_m))
                if altitude > self.geofence.max_altitude_m:
                    raise FlightCommandRejected("above_max_altitude")
                self.adapter.takeoff(altitude)
            elif action == "goto":
                lat, lon = float(params["lat"]), float(params["lon"])
                altitude = float(params.get("altitude_m", self.safety.transit_altitude_m))
                problems = self.geofence.violations(lat, lon, altitude)
                if problems:
                    raise FlightCommandRejected(f"target rejected: {', '.join(problems)}")
                self.adapter.goto(lat, lon, altitude)
            elif action == "hold":
                self.adapter.hold()
            elif action == "return_home":
                self.adapter.return_to_home()
                self._abort_mission("operator_return_home")
            elif action == "land":
                self.adapter.land()
                self._abort_mission("operator_land")
            else:
                raise ValueError(f"unknown drone action {action!r}")
            self.audit(operator, f"drone_{action}", params)
            self._changed()

    # ---- periodic safety monitor ----------------------------------------

    def step(self, dt_s: float) -> None:
        with self._lock:
            self.adapter.step(dt_s)
            telemetry = self.adapter.telemetry()
            now_ms = int(time.time() * 1000)
            previous = self.safety_state
            self.safety_state = "ok"

            for request in self.requests.values():
                if request.status == "pending_approval" and now_ms - request.created_ms > self.safety.request_ttl_s * 1000:
                    request.status = "expired"
                    self._save(request)

            if telemetry.connected and telemetry.in_air:
                if telemetry.lat is not None and self.geofence.violations(telemetry.lat, telemetry.lon):
                    self.safety_state = "geofence_breach"
                    self._safe_action("return_home", "geofence_breach", telemetry.flight_mode)
                if telemetry.battery_pct is not None and telemetry.battery_pct <= self.safety.land_battery_pct:
                    self.safety_state = "critical_battery_land"
                    self._safe_action("land", "critical_battery", telemetry.flight_mode)
                elif telemetry.battery_pct is not None and telemetry.battery_pct <= self.safety.rth_battery_pct:
                    self.safety_state = "low_battery_return"
                    self._safe_action("return_home", "low_battery", telemetry.flight_mode)
                if not telemetry.link_ok:
                    self.safety_state = "link_lost"  # FC failsafe owns the response; we only report it
            elif telemetry.connected and not telemetry.link_ok:
                self.safety_state = "link_lost"

            changed = False
            state = flight_state(telemetry)
            if state != self.flight_state:
                if "link_lost" in (state, self.flight_state):
                    self.audit("system", "drone_link_lost" if state == "link_lost" else "drone_link_restored", {"drone": self.drone_id, "flight_state": state})
                self.flight_state = state
                changed = True
            self._track_flight(telemetry, state, now_ms)
            self._advance_mission(telemetry, now_ms)
            if self.safety_state != previous:
                self.audit("system", "drone_safety_state", {"from": previous, "to": self.safety_state, "drone": self.drone_id})
                changed = True
            if changed:
                self._changed()

    def status(self) -> dict[str, Any]:
        with self._lock:
            telemetry = self.adapter.telemetry()
            mode, mode_reason = self.mode()
            audit = self.parameter_audit()
            return {
                "drone_id": self.drone_id,
                "adapter": self.adapter.kind,
                "physical": self.adapter.is_physical,
                "mode": mode,
                "mode_reason": mode_reason,
                "telemetry": telemetry.as_dict(),
                "flight_state": self.flight_state,
                "safety_state": self.safety_state,
                "preflight_blockers": self.preflight_blockers(),
                "readiness": self.readiness(),
                "parameter_audit": audit,
                "conditions": self.conditions,
                "advisories": self.advisories[-10:],
                "companion": self.companion_status(),
                "video": self.video_status(),
                "flight": self._flight_view(self._flight) if self._flight else None,
                "maintenance": self.maintenance_summary(),
                "mission": dict(self._mission) if self._mission else None,
                "requests": [request.as_dict() for request in sorted(self.requests.values(), key=lambda r: -r.created_ms)[:20]],
                "geofence": {
                    "polygon": self.geofence.polygon,
                    "exclusions": self.geofence.exclusions,
                    "max_altitude_m": self.geofence.max_altitude_m,
                    "min_altitude_m": self.geofence.min_altitude_m,
                },
                "safe_transit_altitude_m": self.safety.transit_altitude_m,
                "home": list(self.current_home()),
            }

    # ---- flight and maintenance logs ----------------------------------------

    def flights(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.records.list_drone_flights(drone_id=self.drone_id, limit=limit)
        if self._flight and not any(row["flight_id"] == self._flight["flight_id"] for row in rows):
            rows.insert(0, self._flight_view(self._flight))
        return rows

    def add_maintenance(self, kind: str, operator: str, note: str = "") -> dict[str, Any]:
        if kind not in MAINTENANCE_KINDS:
            raise ValueError(f"kind must be one of {list(MAINTENANCE_KINDS)}")
        now = int(time.time() * 1000)
        record = {"record_id": f"mnt_{self.drone_id}_{now}", "drone_id": self.drone_id, "recorded_ms": now, "kind": kind, "by": operator, "note": note[:1000]}
        if kind == "commissioning":
            parameters = self.adapter.parameters()
            if not parameters:
                raise ValueError("commissioning needs the flight controller's parameters; wait until they are read")
            audit = self.parameter_audit()
            record |= {"parameters": parameters, "fingerprint": (audit or {}).get("fingerprint", ""),
                       "firmware": self.adapter.telemetry().autopilot_version, "audit_ok": bool(audit and audit["ok"])}
        self.records.add_drone_maintenance(record)
        self.audit(operator, "drone_maintenance_recorded", {k: v for k, v in record.items() if k != "parameters"})
        self._changed()
        return {k: v for k, v in record.items() if k != "parameters"}

    def maintenance(self, limit: int = 100) -> list[dict[str, Any]]:
        return [{k: v for k, v in row.items() if k != "parameters"} for row in self.records.list_drone_maintenance(drone_id=self.drone_id, limit=limit)]

    def commissioning_baseline(self) -> dict[str, Any] | None:
        rows = self.records.list_drone_maintenance(drone_id=self.drone_id, kind="commissioning", limit=1)
        return rows[0] if rows else None

    def maintenance_summary(self) -> dict[str, Any]:
        flights = self.records.list_drone_flights(drone_id=self.drone_id, limit=1000)
        inspections = self.records.list_drone_maintenance(drone_id=self.drone_id, kind="inspection", limit=1)
        last_inspection = inspections[0]["recorded_ms"] if inspections else 0
        since = sum(1 for flight in flights if flight["started_ms"] > last_inspection)
        return {"flights": len(flights), "flight_time_s": round(sum(flight.get("duration_s", 0) for flight in flights)),
                "flights_since_inspection": since, "last_inspection_ms": last_inspection or None,
                "inspection_due": since >= self.safety.inspect_every_flights, "commissioned": self.commissioning_baseline() is not None}

    # ---- internals -------------------------------------------------------

    def _track_flight(self, telemetry, state: str, now_ms: int) -> None:
        flight = self._flight
        if flight is None:
            if telemetry.armed or telemetry.in_air:
                mission = self._mission["request_id"] if self._mission else ""
                self._flight = flight = {
                    "flight_id": f"flt_{self.drone_id}_{now_ms}", "drone_id": self.drone_id, "started_ms": now_ms, "ended_ms": None,
                    "simulated": not self.adapter.is_physical, "adapter": self.adapter.kind, "request_id": mission,
                    "battery_start_pct": telemetry.battery_pct, "battery_min_pct": telemetry.battery_pct, "max_alt_m": 0.0, "distance_m": 0.0,
                    "states": [[now_ms, state]], "messages": [], "safety_events": [], "_last_pos": None, "_messages_seen": telemetry.message_count,
                }
                if mission:
                    self.requests[mission].flight_id = flight["flight_id"]
                self.audit("system", "drone_flight_started", {"flight_id": flight["flight_id"], "drone": self.drone_id})
                self._save_flight(now_ms, force=True)
            return
        flight["max_alt_m"] = round(max(flight["max_alt_m"], telemetry.alt_m), 1)
        if telemetry.battery_pct is not None:
            flight["battery_min_pct"] = min(flight["battery_min_pct"] if flight["battery_min_pct"] is not None else 100, telemetry.battery_pct)
            flight["battery_end_pct"] = telemetry.battery_pct
        if telemetry.lat is not None and telemetry.link_ok:
            if flight["_last_pos"]:
                flight["distance_m"] = round(flight["distance_m"] + distance_m(flight["_last_pos"], (telemetry.lat, telemetry.lon)), 1)
            flight["_last_pos"] = (telemetry.lat, telemetry.lon)
        if flight["states"][-1][1] != state and len(flight["states"]) < 300:
            flight["states"].append([now_ms, state])
        if self.safety_state != "ok" and (not flight["safety_events"] or flight["safety_events"][-1][1] != self.safety_state):
            flight["safety_events"].append([now_ms, self.safety_state])
        fresh = min(len(telemetry.messages), max(0, telemetry.message_count - flight["_messages_seen"]))
        if fresh:
            flight["messages"] = (flight["messages"] + telemetry.messages[-fresh:])[-40:]
        flight["_messages_seen"] = telemetry.message_count
        if not telemetry.armed and not telemetry.in_air and state in ("on_ground", "docked"):
            flight["ended_ms"] = now_ms
            flight["duration_s"] = round((now_ms - flight["started_ms"]) / 1000, 1)
            self._save_flight(now_ms, force=True)
            self.audit("system", "drone_flight_logged", {"flight_id": flight["flight_id"], "duration_s": flight["duration_s"], "drone": self.drone_id})
            self._flight = None
        else:
            self._save_flight(now_ms)

    @staticmethod
    def _flight_view(flight: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in flight.items() if not k.startswith("_")}

    def _save_flight(self, now_ms: int, *, force: bool = False) -> None:
        if self._flight and (force or now_ms - self._flight_saved_ms >= 10_000):
            self._flight_saved_ms = now_ms
            self.records.upsert_drone_flight(self._flight_view(self._flight))

    def _advance_mission(self, telemetry, now_ms: int) -> None:
        if not self._mission:
            return
        request = self.requests[self._mission["request_id"]]
        command = request.mode == "command"
        transit = float(request.route.get("transit_alt_m", self.safety.transit_altitude_m))
        landed = not telemetry.in_air and not telemetry.armed
        if request.status == "approved":
            if telemetry.armed or telemetry.in_air:
                request.status, request.phase, request.started_ms = "in_progress", "takeoff", now_ms
                request.flight_id = self._flight["flight_id"] if self._flight else ""
                self.audit("system", "drone_inspection_started", {"request_id": request.request_id, "drone": self.drone_id})
            elif request.approved_until_ms and now_ms > request.approved_until_ms:
                self._finish(request, "not_flown", "the approved flight did not start within the approval window", now_ms)
                return
            else:
                return
        elif telemetry.lat is None:
            return
        distance = distance_m((telemetry.lat, telemetry.lon), (request.target_lat, request.target_lon)) if telemetry.lat is not None else 1e9
        returning = telemetry.flight_mode in RETURN_MODES
        if request.phase in {"takeoff", "enroute", "inspecting"} and returning:
            # The flight controller is returning on its own (failsafe) or the pilot chose to.
            request.phase = "returning"
            request.note = request.note or ("the flight controller returned home before the inspection finished"
                                            if command else "returned before the inspection finished")
        elif request.phase == "takeoff":
            if telemetry.in_air and telemetry.alt_m >= transit - 1.5:
                request.phase = "enroute"
                if command:
                    self.adapter.goto(request.target_lat, request.target_lon, transit)
            elif landed and now_ms - (request.started_ms or now_ms) > 5000:
                self._finish(request, "aborted", "the aircraft landed or disarmed before reaching the inspection point", now_ms)
                return
            else:
                return
        elif request.phase == "enroute":
            if distance <= self.safety.arrival_radius_m:
                request.phase, request.reached_target_ms = "inspecting", now_ms
                self._mission["hover_started"] = now_ms
            elif landed:
                self._finish(request, "aborted", "the aircraft landed before reaching the inspection point", now_ms)
                return
            else:
                return
        elif request.phase == "inspecting":
            hovered = now_ms - (self._mission["hover_started"] or now_ms)
            if command and hovered >= self.safety.hover_s * 1000:
                request.phase = "returning"
                self.adapter.return_to_home()
            elif not command and (distance > 2 * self.safety.arrival_radius_m + 2 or landed):
                request.phase = "returning"
                if hovered < self.safety.hover_s * 1000:
                    request.note = f"left the inspection point after {hovered / 1000:.0f} s (planned {self.safety.hover_s:g} s)"
            else:
                return
        elif request.phase in {"returning", "aborting"} and landed:
            status = "completed" if request.phase == "returning" and request.reached_target_ms else "aborted"
            self._finish(request, status, request.note, now_ms)
            return
        else:
            return
        self._save(request)
        self._changed()

    def _finish(self, request: InspectionRequest, status: str, note: str, now_ms: int) -> None:
        request.status, request.phase, request.ended_ms = status, "done", now_ms
        request.note = note or request.note
        self._mission = None
        self.audit("system", f"drone_inspection_{status}", request.as_dict())
        self._save(request)
        self._changed()
        try:
            self.on_inspection_finished(request.as_dict())
        except Exception:  # a listener must never break the safety loop
            pass

    def _abort_mission(self, reason: str) -> None:
        if self._mission:
            request = self.requests[self._mission["request_id"]]
            if request.status == "approved":
                self._finish(request, "aborted", reason, int(time.time() * 1000))
                return
            request.phase = "aborting"
            request.note = reason
            self._save(request)

    def _safe_action(self, action: str, reason: str, flight_mode: str) -> None:
        if action == "return_home" and flight_mode in RETURN_MODES | {"landing", "LAND"}:
            return
        if action == "land" and flight_mode in {"landing", "LAND"}:
            return
        if self.mode()[0] != "command":
            # Sentinel may not command this aircraft: tell the operator; the FC failsafe acts on its own.
            text = {"low_battery": "Battery low: the pilot should return home now",
                    "critical_battery": "Battery critical: the pilot should land now",
                    "geofence_breach": "Outside the approved area: the pilot should return home now"}.get(reason, reason)
            if not self.advisories or self.advisories[-1]["reason"] != reason:
                self.advisories.append({"ms": int(time.time() * 1000), "reason": reason, "text": text})
                self.audit("system", "drone_operator_advisory", {"reason": reason, "drone": self.drone_id})
                if self._flight is not None:
                    self._flight["safety_events"].append([int(time.time() * 1000), f"advisory:{reason}"])
            return
        self._abort_mission(reason)
        try:
            getattr(self.adapter, "return_to_home" if action == "return_home" else "land")()
            self.audit("system", f"drone_auto_{action}", {"reason": reason, "drone": self.drone_id})
        except FlightCommandRejected as error:
            self.safety_notes.append(f"{reason}: {error}")

    def _pending(self, request_id: str) -> InspectionRequest:
        request = self.requests.get(request_id)
        if request is None:
            raise KeyError(request_id)
        if request.status != "pending_approval":
            raise FlightCommandRejected(f"request is {request.status}")
        return request

    def _save(self, request: InspectionRequest) -> None:
        self.records.upsert_drone_inspection(request.as_dict())

    def _restore(self) -> None:
        """After a restart: requests come back; an inspection that was flying is
        marked interrupted (never resumed), and open flight logs are closed."""
        now = int(time.time() * 1000)
        for row in self.records.list_drone_inspections(drone_id=self.drone_id, statuses=tuple(OPEN_REQUEST_STATES), limit=50):
            request = InspectionRequest(**{key: row[key] for key in InspectionRequest.__dataclass_fields__ if key in row})
            if request.status == "in_progress":
                request.status, request.phase, request.ended_ms = "interrupted", "done", now
                request.note = "Sentinel restarted during this inspection; it was not resumed. Check the aircraft and the pilot's ground station."
                self.audit("system", "drone_inspection_interrupted", {"request_id": request.request_id, "drone": self.drone_id})
            elif request.status == "approved" and request.approved_until_ms and now > request.approved_until_ms:
                request.status, request.phase, request.ended_ms = "not_flown", "done", now
            elif request.status == "approved":
                self._mission = {"request_id": request.request_id, "hover_started": None}
            elif request.status == "pending_approval" and now - request.created_ms > self.safety.request_ttl_s * 1000:
                request.status = "expired"
            self.requests[request.request_id] = request
            self._save(request)
        for flight in self.records.list_drone_flights(drone_id=self.drone_id, open_only=True, limit=10):
            flight["ended_ms"] = flight.get("ended_ms") or now
            flight["interrupted"] = True
            flight["duration_s"] = round((flight["ended_ms"] - flight["started_ms"]) / 1000, 1)
            self.records.upsert_drone_flight(flight)

    def _changed(self) -> None:
        self.on_change(self.status())
