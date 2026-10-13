from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

from .base import DroneAdapter, FlightCommandRejected, map_point_to_geo
from .geofence import Geofence

OPEN_REQUEST_STATES = {"pending_approval", "in_progress"}


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
    status: str = "pending_approval"  # pending_approval | denied | expired | in_progress | completed | aborted
    decided_by: str = ""
    decided_ms: int | None = None
    phase: str = ""
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DroneSafetyConfig:
    min_launch_battery_pct: float = 60.0
    rth_battery_pct: float = 30.0
    land_battery_pct: float = 15.0
    inspection_altitude_m: float = 15.0
    hover_s: float = 20.0
    request_ttl_s: float = 300.0
    link_lost_s: float = 3.0

    @classmethod
    def from_dict(cls, data: dict | None) -> "DroneSafetyConfig":
        data = data or {}
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


class DroneManager:
    """Sentinel's side of drone operations: operator-approved inspections,
    manual high-level commands, geofence pre-checks, and a safety monitor
    (battery, fence, link). Nothing flies without an operator approving it,
    and the flight controller remains responsible for actual flight safety."""

    def __init__(
        self,
        adapter: DroneAdapter,
        geofence: Geofence,
        site_origin: tuple[float, float],
        safety: DroneSafetyConfig | None = None,
        *,
        audit: Callable[[str, str, dict[str, Any]], None] = lambda actor, action, body: None,
        on_change: Callable[[dict[str, Any]], None] = lambda status: None,
    ) -> None:
        self.adapter = adapter
        self.geofence = geofence
        self.site_origin = site_origin
        self.safety = safety or DroneSafetyConfig()
        self.audit = audit
        self.on_change = on_change
        self.requests: dict[str, InspectionRequest] = {}
        self.safety_state = "ok"
        self.safety_notes: list[str] = []
        self._mission: dict[str, Any] | None = None
        self._seq = 0
        self._lock = threading.RLock()

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
    ) -> InspectionRequest:
        with self._lock:
            if map_point is not None:
                lat, lon = map_point_to_geo(self.site_origin, *map_point)
            if lat is None or lon is None:
                raise ValueError("inspection target needs lat/lon or a map point")
            problems = self.geofence.violations(lat, lon, self.safety.inspection_altitude_m)
            if problems:
                raise FlightCommandRejected(f"target rejected: {', '.join(problems)}")
            for existing in self.requests.values():
                if existing.status in OPEN_REQUEST_STATES and incident_id and existing.incident_id == incident_id:
                    return existing
            self._seq += 1
            request = InspectionRequest(
                request_id=f"insp_{int(time.time() * 1000)}_{self._seq}",
                created_ms=int(time.time() * 1000),
                source=source,
                target_lat=lat,
                target_lon=lon,
                target_label=label,
                requested_by=requested_by,
                incident_id=incident_id,
            )
            self.requests[request.request_id] = request
            self.audit(requested_by, "drone_inspection_requested", request.as_dict())
            self._changed()
            return request

    def approve(self, request_id: str, operator: str) -> InspectionRequest:
        with self._lock:
            request = self._pending(request_id)
            blockers = self.preflight_blockers()
            if blockers:
                raise FlightCommandRejected(f"preflight blocked: {', '.join(blockers)}")
            request.status = "in_progress"
            request.decided_by = operator
            request.decided_ms = int(time.time() * 1000)
            request.phase = "takeoff"
            self.adapter.takeoff(self.safety.inspection_altitude_m)
            self._mission = {"request_id": request_id, "hover_started": None}
            self.audit(operator, "drone_inspection_approved", request.as_dict())
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
            self._changed()
            return request

    def preflight_blockers(self) -> list[str]:
        telemetry = self.adapter.telemetry()
        blockers = []
        if not telemetry.connected or not telemetry.link_ok:
            blockers.append("no_link")
        if telemetry.battery_pct is None or telemetry.battery_pct < self.safety.min_launch_battery_pct:
            blockers.append("battery_below_launch_minimum")
        if telemetry.gps_fix < 3:
            blockers.append("no_3d_gps_fix")
        if telemetry.in_air or self._mission is not None:
            blockers.append("already_flying")
        if self.safety_state not in {"ok"}:
            blockers.append(f"safety_{self.safety_state}")
        return blockers

    # ---- manual commands -------------------------------------------------

    def command(self, action: str, operator: str, params: dict[str, Any] | None = None) -> None:
        params = params or {}
        with self._lock:
            if action == "takeoff":
                blockers = self.preflight_blockers()
                if blockers:
                    raise FlightCommandRejected(f"preflight blocked: {', '.join(blockers)}")
                altitude = float(params.get("altitude_m", self.safety.inspection_altitude_m))
                if altitude > self.geofence.max_altitude_m:
                    raise FlightCommandRejected("above_max_altitude")
                self.adapter.takeoff(altitude)
            elif action == "goto":
                lat, lon = float(params["lat"]), float(params["lon"])
                altitude = float(params.get("altitude_m", self.safety.inspection_altitude_m))
                problems = self.geofence.violations(lat, lon, altitude)
                if problems:
                    raise FlightCommandRejected(f"target rejected: {', '.join(problems)}")
                self.adapter.goto(lat, lon, altitude)
            elif action == "hold":
                self.adapter.hold()
            elif action == "return_home":
                self._abort_mission("operator_return_home")
                self.adapter.return_to_home()
            elif action == "land":
                self._abort_mission("operator_land")
                self.adapter.land()
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

            self._advance_mission(telemetry, now_ms)
            if self.safety_state != previous:
                self.audit("system", "drone_safety_state", {"from": previous, "to": self.safety_state})
                self._changed()

    def status(self) -> dict[str, Any]:
        with self._lock:
            telemetry = self.adapter.telemetry()
            return {
                "adapter": self.adapter.kind,
                "physical": self.adapter.is_physical,
                "telemetry": telemetry.as_dict(),
                "safety_state": self.safety_state,
                "preflight_blockers": self.preflight_blockers(),
                "mission": dict(self._mission) if self._mission else None,
                "requests": [request.as_dict() for request in sorted(self.requests.values(), key=lambda r: -r.created_ms)[:20]],
                "geofence": {
                    "polygon": self.geofence.polygon,
                    "max_altitude_m": self.geofence.max_altitude_m,
                    "min_altitude_m": self.geofence.min_altitude_m,
                },
                "home": list(self.site_origin),
            }

    # ---- internals -------------------------------------------------------

    def _advance_mission(self, telemetry, now_ms: int) -> None:
        if not self._mission:
            return
        request = self.requests[self._mission["request_id"]]
        if request.phase == "takeoff" and telemetry.flight_mode == "holding":
            request.phase = "enroute"
            self.adapter.goto(request.target_lat, request.target_lon, self.safety.inspection_altitude_m)
        elif request.phase == "enroute" and telemetry.flight_mode == "holding":
            request.phase = "inspecting"
            self._mission["hover_started"] = now_ms
        elif request.phase == "inspecting" and now_ms - self._mission["hover_started"] >= self.safety.hover_s * 1000:
            request.phase = "returning"
            self.adapter.return_to_home()
        elif request.phase in {"returning", "aborting"} and not telemetry.in_air:
            request.status = "completed" if request.phase == "returning" else "aborted"
            request.phase = "done"
            self._mission = None
            self.audit("system", f"drone_inspection_{request.status}", request.as_dict())
        else:
            return
        self._changed()

    def _abort_mission(self, reason: str) -> None:
        if self._mission:
            request = self.requests[self._mission["request_id"]]
            request.phase = "aborting"
            request.note = reason

    def _safe_action(self, action: str, reason: str, flight_mode: str) -> None:
        if action == "return_home" and flight_mode in {"returning", "landing"}:
            return
        if action == "land" and flight_mode == "landing":
            return
        self._abort_mission(reason)
        try:
            getattr(self.adapter, "return_to_home" if action == "return_home" else "land")()
            self.audit("system", f"drone_auto_{action}", {"reason": reason})
        except FlightCommandRejected as error:
            self.safety_notes.append(f"{reason}: {error}")

    def _pending(self, request_id: str) -> InspectionRequest:
        request = self.requests.get(request_id)
        if request is None:
            raise KeyError(request_id)
        if request.status != "pending_approval":
            raise FlightCommandRejected(f"request is {request.status}")
        return request

    def _changed(self) -> None:
        self.on_change(self.status())
