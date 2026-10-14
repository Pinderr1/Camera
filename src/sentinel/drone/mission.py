"""Inspection routes and their export for a ground station.

V1 operating model: Sentinel *proposes* a route; the pilot loads it into their
own ground station (QGroundControl, Mission Planner), reviews it, and flies it.
The route avoids obstacles by design: climb vertically over home to the site's
surveyed safe transit altitude, fly straight to the inspection point at that
altitude, hover, and return home (RTL, which climbs to RTL_ALT first; the
parameter audit checks RTL_ALT is not below the transit altitude).

Every leg is sampled against Sentinel's geofence. The flight controller's own
fence stays authoritative. Standard library only."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any

from .base import geo_to_offset, offset_to_geo
from .geofence import Geofence

MAV_CMD_NAV_WAYPOINT = 16
MAV_CMD_NAV_RETURN_TO_LAUNCH = 20
MAV_CMD_NAV_TAKEOFF = 22
MAV_CMD_NAV_FENCE_POLYGON_VERTEX_INCLUSION = 5001
MAV_CMD_NAV_FENCE_POLYGON_VERTEX_EXCLUSION = 5002
MAV_FRAME_GLOBAL = 0
MAV_FRAME_MISSION = 2
MAV_FRAME_GLOBAL_RELATIVE_ALT = 3


@dataclass
class Route:
    home: tuple[float, float]
    target: tuple[float, float]
    target_label: str
    transit_alt_m: float
    hover_s: float
    waypoints: list[dict[str, Any]] = field(default_factory=list)
    distance_m: float = 0.0
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["home"], data["target"] = list(self.home), list(self.target)
        return data


def plan_route(home: tuple[float, float], target: tuple[float, float], geofence: Geofence, *, safe_transit_altitude_m: float,
               inspection_altitude_m: float, hover_s: float, label: str = "") -> Route:
    transit = max(safe_transit_altitude_m, inspection_altitude_m)
    notes = []
    if inspection_altitude_m < safe_transit_altitude_m:
        notes.append(f"Inspection altitude raised from {inspection_altitude_m:g} m to the safe transit altitude {safe_transit_altitude_m:g} m: "
                     "the aircraft never descends below the surveyed obstacle height away from home.")
    route = Route(home=home, target=target, target_label=label, transit_alt_m=transit, hover_s=hover_s, notes=notes)
    route.waypoints = [
        {"kind": "takeoff", "lat": home[0], "lon": home[1], "alt_m": transit, "hold_s": 0},
        {"kind": "inspect", "lat": target[0], "lon": target[1], "alt_m": transit, "hold_s": hover_s},
        {"kind": "return_home", "lat": home[0], "lon": home[1], "alt_m": transit, "hold_s": 0},
    ]
    east, north = geo_to_offset(home, *target)
    route.distance_m = round(2 * math.hypot(east, north) + 2 * transit, 1)
    problems = set(geofence.violations(home[0], home[1]))
    problems = {f"home_{p}" for p in problems}
    problems |= {f"climb_{p}" for p in geofence.violations(home[0], home[1], transit) if "altitude" in p}
    for point in _sample(home, target, step_m=1.0):
        problems |= {f"route_{p}" for p in geofence.violations(point[0], point[1], transit)}
    route.problems = sorted(problems)
    return route


def _sample(a: tuple[float, float], b: tuple[float, float], step_m: float) -> list[tuple[float, float]]:
    east, north = geo_to_offset(a, *b)
    steps = max(1, int(math.hypot(east, north) / step_m))
    return [offset_to_geo(a, east * i / steps, north * i / steps) for i in range(steps + 1)]


def mission_items(route: Route) -> list[dict[str, Any]]:
    """MAVLink mission items (relative altitudes), without the home item."""
    items = []
    for waypoint in route.waypoints:
        if waypoint["kind"] == "takeoff":
            items.append({"command": MAV_CMD_NAV_TAKEOFF, "frame": MAV_FRAME_GLOBAL_RELATIVE_ALT,
                          "params": [0, 0, 0, None, waypoint["lat"], waypoint["lon"], waypoint["alt_m"]]})
        elif waypoint["kind"] == "inspect":
            items.append({"command": MAV_CMD_NAV_WAYPOINT, "frame": MAV_FRAME_GLOBAL_RELATIVE_ALT,
                          "params": [waypoint["hold_s"], 1.0, 0, None, waypoint["lat"], waypoint["lon"], waypoint["alt_m"]]})
        elif waypoint["kind"] == "return_home":
            items.append({"command": MAV_CMD_NAV_RETURN_TO_LAUNCH, "frame": MAV_FRAME_MISSION, "params": [0, 0, 0, 0, 0, 0, 0]})
    return items


def to_qgc_plan(route: Route, geofence: Geofence, *, request_id: str = "", home_amsl_m: float = 0.0) -> dict[str, Any]:
    """QGroundControl `.plan` (JSON). Mission Planner opens it too."""
    items = []
    for number, item in enumerate(mission_items(route), start=1):
        altitude = item["params"][6] if item["frame"] == MAV_FRAME_GLOBAL_RELATIVE_ALT else None
        items.append({"type": "SimpleItem", "autoContinue": True, "command": item["command"], "doJumpId": number, "frame": item["frame"],
                      "params": item["params"], "Altitude": altitude, "AltitudeMode": 1 if altitude is not None else None, "AMSLAltAboveTerrain": None})
    polygons = [{"inclusion": True, "version": 1, "polygon": [list(point) for point in geofence.polygon]}]
    polygons += [{"inclusion": False, "version": 1, "polygon": [list(point) for point in zone]} for zone in geofence.exclusions]
    return {
        "fileType": "Plan", "version": 1, "groundStation": "Sentinel",
        "sentinel": {"request_id": request_id, "target": route.target_label, "transit_alt_m": route.transit_alt_m,
                     "note": "Proposed by Sentinel for pilot review. The pilot in command decides whether and how to fly it."},
        "mission": {"version": 2, "firmwareType": 3, "vehicleType": 2, "cruiseSpeed": 5, "hoverSpeed": 5,
                    "plannedHomePosition": [route.home[0], route.home[1], home_amsl_m], "items": items},
        "geoFence": {"version": 2, "circles": [], "polygons": polygons},
        "rallyPoints": {"version": 2, "points": []},
    }


def to_waypoints_text(route: Route, *, home_amsl_m: float = 0.0) -> str:
    """`QGC WPL 110` text (Mission Planner .waypoints). Line 0 is home."""
    def number(value: Any) -> str:
        return "0" if value is None else f"{float(value):.8f}".rstrip("0").rstrip(".") or "0"

    lines = ["QGC WPL 110", "\t".join(["0", "1", str(MAV_FRAME_GLOBAL), str(MAV_CMD_NAV_WAYPOINT), "0", "0", "0", "0",
                                         number(route.home[0]), number(route.home[1]), number(home_amsl_m), "1"])]
    for index, item in enumerate(mission_items(route), start=1):
        lines.append("\t".join([str(index), "0", str(item["frame"]), str(item["command"])] + [number(v) for v in item["params"]] + ["1"]))
    return "\n".join(lines) + "\n"


def fence_items(geofence: Geofence) -> list[dict[str, Any]]:
    """Fence polygons as MAVLink fence items (for a commissioning tool or SITL)."""
    items = []
    for command, polygons in ((MAV_CMD_NAV_FENCE_POLYGON_VERTEX_INCLUSION, [geofence.polygon]),
                              (MAV_CMD_NAV_FENCE_POLYGON_VERTEX_EXCLUSION, geofence.exclusions)):
        for polygon in polygons:
            for lat, lon in polygon:
                items.append({"command": command, "frame": MAV_FRAME_GLOBAL, "params": [len(polygon), 0, 0, 0, lat, lon, 0]})
    return items


def parse_qgc_plan(text: str) -> dict[str, Any]:
    """Read back a `.plan` (used by tests and the SITL pilot stand-in)."""
    plan = json.loads(text)
    if plan.get("fileType") != "Plan":
        raise ValueError("not a QGroundControl plan")
    return plan
