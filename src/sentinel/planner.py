"""Installation planning on the property map: what to install, where, and what
it will roughly see.

A plan is a list of items in site-map metres (x east, y south): property
features (buildings, driveways, gates, public or privacy-sensitive areas),
proposed or installed devices (cameras, sensors, network equipment, drone docks
and observation points). From it this module derives:

- approximate camera coverage on a grid, with line of sight blocked by drawn
  buildings and obstructions, gaps at doors, gates and driveways, and privacy
  warnings where a view reaches public or sensitive areas;
- camera recommendations from a short property questionnaire;
- an equipment estimate (PoE ports and budget, cable runs, power outlets);
- installation and power/network checklists, and a printable report.

Everything here is approximate and says so. Detection range comes from
Sentinel's own benchmark (people smaller than about 10% of the picture height
were mostly missed), not from any camera's datasheet. Nothing here is a
guarantee of coverage or a purchasing claim. Standard library only."""

from __future__ import annotations

import html
import math
import re
import time
import uuid
from typing import Any

from senior_safety.pose_events import point_in_polygon

AREA_KINDS = {
    "building": "Building", "driveway": "Driveway", "garage": "Garage", "yard": "Yard", "parking": "Parking",
    "public_area": "Public area (street, sidewalk)", "privacy_area": "Privacy-sensitive area", "room": "Indoor room or area",
    "obstruction": "Obstruction (tree, wall, hedge)",
}
LINE_KINDS = {"fence": "Fence or wall"}
POINT_KINDS = {
    "entrance": "Door / entrance", "gate": "Gate", "garage_door": "Garage door", "camera": "Camera", "sensor": "Sensor",
    "network": "Network equipment", "drone_dock": "Drone dock", "drone_point": "Drone observation point",
}
ITEM_KINDS = AREA_KINDS | LINE_KINDS | POINT_KINDS
PRIORITY_POINTS = ("entrance", "gate", "garage_door")
PRIORITY_AREAS = ("driveway", "garage", "parking")
BLOCKING = ("building", "obstruction")
CAMERA_POWER = ("poe", "wifi", "dc", "battery")
SENSOR_KINDS = ("contact", "motion", "environmental")
NETWORK_KINDS = ("router", "switch", "recorder", "hub", "access_point", "ups")
POE_PORT_W = 15.4  # IEEE 802.3af maximum per port at the switch
ETHERNET_LINK_M = 90  # permanent-link limit of a 100 m Ethernet channel
PROPERTY_TYPES = {
    "house": "Detached house", "corner_lot": "House on a corner lot", "townhouse": "Townhouse or semi-detached",
    "storefront": "Storefront", "warehouse": "Warehouse", "office": "Small office", "other": "Other",
}
INSTALL_PATHS = ("existing_cameras", "new_cameras", "drone_first", "complete_package")
MAX_ITEMS = 300
_ID = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,40}$")
# Person height and the share of the picture height below which Sentinel's
# benchmark found people were mostly missed (ROADMAP: <10% of frame height,
# 17-18% recall). Used to estimate a reliable detection range per lens.
PERSON_M = 1.7
MIN_PERSON_SHARE = 0.10
ASSUMPTIONS = [
    "Detection range assumes a standing person must fill about 10% of the picture height (Sentinel's benchmark missed most smaller people). Real range depends on resolution, lens, light and the camera's IR range.",
    "Map positions, angles and distances are approximate; nothing here was measured on site.",
    "Only drawn buildings and obstructions block the view. Trees, vehicles, slopes, glare and night lighting are not modelled unless drawn.",
    "Indoor coverage ignores interior walls that are not drawn as rooms or obstructions.",
    "Coverage is visual only. It does not mean anything seen will be detected, recorded or alerted on.",
]


class PlanError(ValueError):
    pass


# ---- plan validation -------------------------------------------------------------------


def _number(value: Any, name: str, low: float = -1000.0, high: float = 6000.0) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not low <= value <= high:
        raise PlanError(f"{name} must be a number between {low:g} and {high:g}")
    return round(float(value), 2)


def _points(raw: Any, name: str, minimum: int) -> list[list[float]]:
    if not isinstance(raw, list) or len(raw) < minimum or len(raw) > 200:
        raise PlanError(f"{name} needs {minimum} to 200 points")
    if not all(isinstance(p, (list, tuple)) and len(p) == 2 for p in raw):
        raise PlanError(f"{name} has a malformed point")
    return [[_number(p[0], name), _number(p[1], name)] for p in raw]


def clean_plan(raw: dict[str, Any]) -> dict[str, Any]:
    """Validated copy of a plan posted by the planner. Unknown fields are dropped."""
    if not isinstance(raw, dict):
        raise PlanError("plan must be an object")
    items = raw.get("items", [])
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise PlanError(f"plan items must be a list of at most {MAX_ITEMS}")
    cleaned, seen = [], set()
    for index, item in enumerate(items):
        if not isinstance(item, dict) or item.get("kind") not in ITEM_KINDS:
            raise PlanError(f"items[{index}].kind must be one of {sorted(ITEM_KINDS)}")
        kind = item["kind"]
        item_id = str(item.get("id") or f"{kind}_{uuid.uuid4().hex[:8]}")
        if not _ID.match(item_id) or item_id in seen:
            raise PlanError(f"items[{index}].id must be a unique lowercase identifier")
        seen.add(item_id)
        name = str(item.get("name") or ITEM_KINDS[kind])[:60]
        entry: dict[str, Any] = {"id": item_id, "kind": kind, "name": name}
        if kind in AREA_KINDS:
            entry["polygon"] = _points(item.get("polygon"), f"items[{index}].polygon", 3)
        elif kind in LINE_KINDS:
            entry["line"] = _points(item.get("line"), f"items[{index}].line", 2)
        else:
            point = item.get("point")
            if not (isinstance(point, (list, tuple)) and len(point) == 2):
                raise PlanError(f"items[{index}].point must be [x, y]")
            entry["point"] = [_number(point[0], f"items[{index}].point"), _number(point[1], f"items[{index}].point")]
        if kind == "camera":
            entry["heading_deg"] = _number(item.get("heading_deg", 0), "heading_deg", -360, 720) % 360
            entry["fov_deg"] = _number(item.get("fov_deg", 90), "fov_deg", 10, 360)
            if item.get("range_m") not in (None, ""):
                entry["range_m"] = _number(item["range_m"], "range_m", 1, 200)
            entry["mount"] = item.get("mount") if item.get("mount") in ("outdoor", "indoor") else "outdoor"
            entry["power"] = item.get("power") if item.get("power") in CAMERA_POWER else "poe"
            if item.get("power_w") not in (None, ""):
                entry["power_w"] = _number(item["power_w"], "power_w", 0.5, 100)
            entry["status"] = "installed" if item.get("status") == "installed" else "proposed"
            if item.get("camera_id"):
                entry["camera_id"] = str(item["camera_id"])[:60]
        elif kind == "sensor":
            entry["sensor"] = item.get("sensor") if item.get("sensor") in SENSOR_KINDS else "contact"
            entry["power"] = "wired" if item.get("power") == "wired" else "battery"
            if item.get("device_id"):
                entry["device_id"] = str(item["device_id"])[:60]
        elif kind == "network":
            entry["equipment"] = item.get("equipment") if item.get("equipment") in NETWORK_KINDS else "switch"
            if item.get("poe_ports") not in (None, ""):
                entry["poe_ports"] = int(_number(item["poe_ports"], "poe_ports", 0, 96))
        elif kind == "drone_dock" and item.get("device_id"):
            entry["device_id"] = str(item["device_id"])[:60]
        if item.get("note"):
            entry["note"] = str(item["note"])[:200]
        cleaned.append(entry)
    return {"items": cleaned, "updated_ms": int(time.time() * 1000)}


# ---- geometry --------------------------------------------------------------------------


def detection_range_m(fov_deg: float) -> float:
    """Distance at which a standing person fills MIN_PERSON_SHARE of a 16:9
    picture's height, for a given horizontal field of view."""
    fov = max(10.0, min(float(fov_deg), 170.0))
    vertical = 2 * math.atan(math.tan(math.radians(fov) / 2) * 9 / 16)
    return round(max(3.0, min(PERSON_M / (MIN_PERSON_SHARE * 2 * math.tan(vertical / 2)), 60.0)), 1)


def _segments_cross(a, b, c, d) -> bool:
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    d1, d2, d3, d4 = orient(c, d, a), orient(c, d, b), orient(a, b, c), orient(a, b, d)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _blocked(origin, target, blockers: list[list[list[float]]], ignore: list[list[float]] | None) -> bool:
    for polygon in blockers:
        if polygon is ignore:
            continue
        count = len(polygon)
        for i in range(count):
            if _segments_cross(origin, target, polygon[i], polygon[(i + 1) % count]):
                return True
    return False


def _polygon_area(polygon) -> float:
    return abs(sum(polygon[i][0] * polygon[(i + 1) % len(polygon)][1] - polygon[(i + 1) % len(polygon)][0] * polygon[i][1]
                   for i in range(len(polygon)))) / 2


def _sees(camera: dict[str, Any], x: float, y: float, blockers, inside: list[list[float]] | None) -> bool:
    cx, cy = camera["point"]
    dx, dy = x - cx, y - cy
    distance = math.hypot(dx, dy)
    if distance > camera["range_m"] or distance < 0.05:
        return distance < 0.05
    if camera["fov_deg"] < 360:
        # Map heading: 0 = north (up, -y), clockwise, matching the property editor.
        bearing = (math.degrees(math.atan2(dx, -dy)) + 360) % 360
        offset = abs((bearing - camera["heading_deg"] + 180) % 360 - 180)
        if offset > camera["fov_deg"] / 2:
            return False
    return not _blocked((cx, cy), (x, y), blockers, inside)


def plan_cameras(plan: dict[str, Any], installed: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Plan cameras plus configured cameras that have a map position and are not already in the plan."""
    cameras = []
    linked = set()
    for item in plan.get("items", []):
        if item["kind"] == "camera":
            fov = item.get("fov_deg", 90)
            cameras.append(item | {"range_m": item.get("range_m") or detection_range_m(fov), "range_assumed": not item.get("range_m"), "source": "plan"})
            if item.get("camera_id"):
                linked.add(item["camera_id"])
    for camera in installed or []:
        pose = camera.get("map")
        if not pose or camera["id"] in linked:
            continue
        fov = pose.get("fov_deg", 90)
        cameras.append({
            "id": f"installed_{camera['id']}", "kind": "camera", "name": camera.get("name", camera["id"]), "point": [pose["x"], pose["y"]],
            "heading_deg": pose.get("heading_deg", 0), "fov_deg": fov, "range_m": detection_range_m(fov), "range_assumed": True,
            "mount": "outdoor", "power": "poe", "status": "installed", "camera_id": camera["id"], "source": "config",
        })
    return cameras


def analyse_coverage(plan: dict[str, Any], width_m: float, height_m: float, installed: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    items = plan.get("items", [])
    cameras = plan_cameras(plan, installed)
    blockers = [item["polygon"] for item in items if item["kind"] in BLOCKING]
    rooms = [item for item in items if item["kind"] == "room"]
    buildings = [item["polygon"] for item in items if item["kind"] == "building"]
    cell = max(0.5, max(width_m, height_m) / 80)
    cols, rows = max(1, int(math.ceil(width_m / cell))), max(1, int(math.ceil(height_m / cell)))

    def container(camera):
        x, y = camera["point"]
        return next((polygon for polygon in blockers if point_in_polygon(x, y, polygon)), None)

    homes = {camera["id"]: container(camera) for camera in cameras}
    grid: list[list[int]] = []
    outdoor_cells = covered_cells = multi_cells = room_cells = room_covered = 0
    for row in range(rows):
        line = []
        for col in range(cols):
            x, y = (col + 0.5) * cell, (row + 0.5) * cell
            in_room = any(point_in_polygon(x, y, room["polygon"]) for room in rooms)
            in_building = any(point_in_polygon(x, y, polygon) for polygon in buildings)
            if in_building and not in_room:
                line.append(-1)
                continue
            count = 0
            for camera in cameras:
                indoor = camera.get("mount") == "indoor"
                if indoor != in_room:
                    continue
                if _sees(camera, x, y, blockers, homes[camera["id"]] if indoor else None):
                    count += 1
            if in_room:
                room_cells += 1
                room_covered += count > 0
            else:
                outdoor_cells += 1
                covered_cells += count > 0
                multi_cells += count > 1
            line.append(min(count, 9))
        grid.append(line)

    def seen_by(x, y):
        names = []
        for camera in cameras:
            in_room = any(point_in_polygon(x, y, room["polygon"]) for room in rooms)
            if (camera.get("mount") == "indoor") == in_room and _sees(camera, x, y, blockers, homes[camera["id"]] if in_room else None):
                names.append(camera["name"])
        return names

    features, gaps = [], []
    for item in items:
        if item["kind"] in PRIORITY_POINTS:
            # A door in a building wall: test a point just outside it, so the wall itself does not block the view.
            x, y = _nudge_outside(item["point"], blockers)
            viewers = seen_by(x, y)
            features.append({"id": item["id"], "name": item["name"], "kind": item["kind"], "covered_by": viewers})
            if not viewers:
                gaps.append(f"{item['name']} ({POINT_KINDS[item['kind']].lower()}) is not in view of any planned camera")
        elif item["kind"] in PRIORITY_AREAS + ("room",):
            share = _area_share(item["polygon"], cell, lambda x, y: bool(seen_by(x, y)))
            features.append({"id": item["id"], "name": item["name"], "kind": item["kind"], "covered_pct": share})
            if share < 50:
                gaps.append(f"{item['name']} is only about {share}% in view")
    privacy = []
    for camera in cameras:
        for item in items:
            if item["kind"] not in ("public_area", "privacy_area"):
                continue
            share = _area_share(item["polygon"], cell, lambda x, y, c=camera: c.get("mount") != "indoor" and _sees(c, x, y, blockers, None))
            if share > 0:
                what = "public area" if item["kind"] == "public_area" else "privacy-sensitive area"
                privacy.append({
                    "camera": camera["name"], "area": item["name"], "kind": item["kind"], "share_pct": share,
                    "advice": f"{camera['name']} may see part of {item['name']} ({what}, about {share}%). Aim it away or add a privacy mask after installation"
                              + ("; recording public areas may be regulated where you live." if item["kind"] == "public_area" else "."),
                })
    outdoor_pct = round(100 * covered_cells / outdoor_cells) if outdoor_cells else 0
    return {
        "cell_m": round(cell, 2), "cols": cols, "rows": rows, "grid": grid,
        "cameras": [{"id": c["id"], "name": c["name"], "range_m": c["range_m"], "range_assumed": c["range_assumed"], "status": c.get("status", "proposed"),
                     "mount": c.get("mount", "outdoor"), "source": c["source"], "point": c["point"], "heading_deg": c.get("heading_deg", 0),
                     "fov_deg": c.get("fov_deg", 90)} for c in cameras],
        "outdoor_area_m2": round(outdoor_cells * cell * cell), "outdoor_covered_pct": outdoor_pct,
        "overlap_pct": round(100 * multi_cells / outdoor_cells) if outdoor_cells else 0,
        "indoor_covered_pct": round(100 * room_covered / room_cells) if room_cells else None,
        "features": features, "gaps": gaps, "privacy": privacy, "assumptions": ASSUMPTIONS,
        "summary": _coverage_sentence(cameras, outdoor_pct, gaps),
    }


def _nudge_outside(point, blockers) -> tuple[float, float]:
    """A door drawn on a building wall is judged by its approach: the spot about
    2 m outside it, which is what a door camera (mounted beside it, facing out) sees."""
    x, y = point

    def free(px, py):
        return not any(point_in_polygon(px, py, polygon) for polygon in blockers)

    near_wall = [angle for angle in range(0, 360, 15)
                 if not free(x + 0.6 * math.sin(math.radians(angle)), y - 0.6 * math.cos(math.radians(angle)))]
    if free(x, y) and not near_wall:
        return x, y
    for angle in sorted(range(0, 360, 15), key=lambda a: min((abs((a - w + 180) % 360 - 180) for w in near_wall), default=0), reverse=True):
        nx, ny = x + 2.0 * math.sin(math.radians(angle)), y - 2.0 * math.cos(math.radians(angle))
        if free(nx, ny):
            return nx, ny
    return x, y


def _area_share(polygon, cell, predicate) -> int:
    xs, ys = [p[0] for p in polygon], [p[1] for p in polygon]
    step = max(cell / 2, 0.25)
    total = hits = 0
    y = min(ys) + step / 2
    while y < max(ys) and total < 4000:
        x = min(xs) + step / 2
        while x < max(xs):
            if point_in_polygon(x, y, polygon):
                total += 1
                hits += predicate(x, y)
            x += step
        y += step
    return round(100 * hits / total) if total else 0


def _coverage_sentence(cameras, outdoor_pct, gaps) -> str:
    if not cameras:
        return "No cameras are placed on the plan yet."
    text = f"{len(cameras)} camera(s) would have about {outdoor_pct}% of the open property area in view (approximate)."
    return text + (f" {len(gaps)} priority spot(s) look uncovered." if gaps else " Every marked door, gate and driveway appears to be in view.")


# ---- recommendations -------------------------------------------------------------------


def _count(info: dict[str, Any], key: str, high: int = 20) -> int:
    value = info.get(key, 0)
    try:
        return max(0, min(int(value), high))
    except (TypeError, ValueError):
        return 0


def recommend(info: dict[str, Any]) -> dict[str, Any]:
    """A starting camera count from a few questions. Every line says why, and
    the result is a suggestion to refine on the map, not a guarantee."""
    property_type = info.get("property_type") if info.get("property_type") in PROPERTY_TYPES else "house"
    doors, garage_doors, gates = _count(info, "exterior_doors"), _count(info, "garage_doors", 10), _count(info, "gates", 10)
    yard_sides, loading, indoor = _count(info, "yard_sides", 4), _count(info, "loading_areas", 10), _count(info, "indoor_areas")
    driveway = bool(info.get("driveway"))
    frontages = 2 if property_type == "corner_lot" else _count(info, "street_frontages", 4) or 1
    lines: list[dict[str, Any]] = []

    def add(role, count, essential, mount, lens, why):
        if count:
            lines.append({"role": role, "count": count, "essential": essential, "mount": mount, "lens": lens, "why": why,
                          "range_m": detection_range_m({"wide": 105, "standard": 90, "narrow": 60}[lens])})

    add("Exterior doors", doors, True, "outdoor", "wide", "One camera per door, mounted about 2.5-3 m high and angled down the approach path.")
    if driveway or garage_doors:
        extra = max(0, math.ceil(garage_doors / 2) - 1)
        add("Driveway and garage", 1 + extra, True, "outdoor", "standard",
            "One camera along the driveway can usually see up to two garage doors; aim it so vehicles cross the view rather than face it head-on.")
    add("Gates", gates, False, "outdoor", "standard", "One per gate, unless the planner shows another camera already sees it.")
    add("Side and back yards", yard_sides, False, "outdoor", "wide", "One per side of the building with an approach worth watching; corner mounts can cover two sides.")
    add("Loading areas", loading, True, "outdoor", "standard", "One per loading door or bay.")
    add("Indoor areas", indoor, False, "indoor", "wide",
        "Entrances, registers or stock rooms. Never in bathrooms, changing rooms or bedrooms; businesses must follow local workplace-recording rules.")
    essential = sum(line["count"] for line in lines if line["essential"])
    total = sum(line["count"] for line in lines)
    notes = [
        "Start with the essential cameras; place them on the property map to check what they would see before buying anything.",
        "Prefer wired PoE cameras: one cable carries power and video, and nothing depends on Wi-Fi signal or batteries.",
        "Cameras must offer RTSP or ONVIF (check the manual). Battery and cloud-only cameras usually cannot stream continuously to Sentinel.",
        "Choose outdoor cameras rated for the weather where they hang (IP66/IP67 is the usual outdoor rating).",
        "Night vision: infrared light reflects off nearby eaves, walls and glass. Never aim a camera through a window.",
    ]
    if frontages > 1 or property_type == "corner_lot":
        notes.append("A corner lot faces two streets: aim cameras at your property, and expect to mask parts of the sidewalks.")
    sensors = []
    if info.get("want_sensors"):
        if doors:
            sensors.append({"kind": "contact", "count": doors, "why": "One door contact per exterior door."})
        if indoor:
            sensors.append({"kind": "motion", "count": indoor, "why": "Motion sensors for indoor areas without cameras."})
    drone = None
    if info.get("want_drone"):
        drone = {"docks": 1, "why": "One dock at an open spot with clear sky above, away from trees, wires and where people gather. "
                 "Flight is simulated in this build; check local drone regulations before any real flight."}
    wired = info.get("wired") if info.get("wired") in ("poe", "wifi", "mixed", "unsure") else "unsure"
    poe = total if wired in ("poe", "unsure") else 0 if wired == "wifi" else essential
    return {
        "property_type": property_type, "lines": lines, "essential": essential, "total": total, "sensors": sensors, "drone": drone,
        "poe_ports": poe, "poe_budget_w": round(poe * POE_PORT_W, 1), "notes": notes,
        "disclaimer": "A starting point based on your answers, not a guarantee of coverage. Check placements on the property map.",
    }


# ---- equipment estimate and checklists ---------------------------------------------------


def estimate(plan: dict[str, Any], installed: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    items = plan.get("items", [])
    cameras = [item for item in items if item["kind"] == "camera"]
    poe = [camera for camera in cameras if camera.get("power", "poe") == "poe"]
    switches = [item for item in items if item["kind"] == "network" and item.get("equipment") in ("switch", "recorder")]
    routers = [item for item in items if item["kind"] == "network" and item.get("equipment") in ("router", "access_point")]
    warnings, runs = [], []
    known_w = sum(camera["power_w"] for camera in poe if camera.get("power_w"))
    unknown = sum(1 for camera in poe if not camera.get("power_w"))
    budget = round(known_w + unknown * POE_PORT_W, 1)
    for camera in poe:
        if not switches:
            break
        nearest = min(switches, key=lambda s: math.dist(s["point"], camera["point"]))
        length = round(math.dist(nearest["point"], camera["point"]) * 1.25 + 5)
        runs.append({"camera": camera["name"], "from": nearest["name"], "approx_m": length})
        if length > ETHERNET_LINK_M:
            warnings.append(f"Cable to {camera['name']} is roughly {length} m: longer than Ethernet allows (90 m fixed cable, 100 m in total). "
                            "Move the switch closer or use a PoE extender.")
    if poe and not switches:
        warnings.append("No PoE switch or PoE recorder is placed on the plan, so cable lengths cannot be estimated.")
    available = sum(item.get("poe_ports", 0) for item in switches)
    if switches and any("poe_ports" in item for item in switches) and available < len(poe):
        warnings.append(f"The planned switch/recorder offers {available} PoE port(s) for {len(poe)} PoE camera(s).")
    for camera in cameras:
        if camera.get("power") == "wifi":
            if routers:
                distance = min(math.dist(r["point"], camera["point"]) for r in routers)
                if distance > 20:
                    warnings.append(f"{camera['name']} is about {round(distance)} m from the nearest router or access point on the plan: "
                                    "check the Wi-Fi signal at the mounting point before drilling.")
            else:
                warnings.append(f"{camera['name']} uses Wi-Fi: check the signal at the mounting point, and plan a power outlet.")
        if camera.get("power") == "battery":
            warnings.append(f"{camera['name']} is battery-powered: most battery cameras sleep between events and cannot stream continuously to Sentinel.")
    sensors = [item for item in items if item["kind"] == "sensor"]
    docks = [item for item in items if item["kind"] == "drone_dock"]
    return {
        "cameras": {"total": len(cameras), "proposed": sum(1 for c in cameras if c.get("status") != "installed"),
                    "installed_on_plan": sum(1 for c in cameras if c.get("status") == "installed"),
                    "outdoor": sum(1 for c in cameras if c.get("mount", "outdoor") == "outdoor"), "indoor": sum(1 for c in cameras if c.get("mount") == "indoor"),
                    "by_power": {power: sum(1 for c in cameras if c.get("power", "poe") == power) for power in CAMERA_POWER}},
        "poe": {"ports_needed": len(poe), "suggested_switch_ports": _switch_size(len(poe)) if poe else 0, "budget_w": budget,
                "budget_basis": (f"{unknown} camera(s) at {POE_PORT_W} W (the 802.3af per-port maximum) where no wattage is entered"
                                 + (f", plus {round(known_w, 1)} W entered" if known_w else "")) if poe else ""},
        "cable_runs": runs, "cable_total_m": sum(run["approx_m"] for run in runs),
        "outlets": sum(1 for c in cameras if c.get("power") in ("wifi", "dc")) + len(docks),
        "sensors": {kind: sum(1 for s in sensors if s.get("sensor") == kind) for kind in SENSOR_KINDS},
        "drone_docks": len(docks), "drone_points": sum(1 for item in items if item["kind"] == "drone_point"),
        "network_equipment": [{"name": item["name"], "equipment": item.get("equipment")} for item in items if item["kind"] == "network"],
        "configured_cameras": len(installed or []),
        "warnings": warnings,
    }


def _switch_size(ports: int) -> int:
    """Smallest common PoE port count leaving at least one spare port."""
    return next((size for size in (4, 8, 16, 24, 48) if size >= ports + 1), ports + 1)


COMMON_CHECKLIST = [
    "Run Sentinel on a computer that stays on, wired to the router where possible.",
    "Put the router, PoE switch/recorder and Sentinel computer on a UPS so short power cuts do not stop recording.",
    "Never port-forward cameras or recorders to the internet; use a VPN for remote access.",
    "Change every default password; keep the recorder's admin password written down somewhere safe.",
]
PATH_CHECKLISTS = {
    "existing_cameras": [
        "Check each camera or recorder offers RTSP, ONVIF or an HTTP snapshot (Setup → hardware compatibility).",
        "Read the recorder's address, subnet mask and gateway from its own screen (Menu → Network).",
        "Connect this computer to the same network as the recorder; wired is most reliable.",
        "Have the recorder's admin password ready: several wrong attempts lock the account.",
        "Set each camera's sub-stream to H.264, about 640x360-640x480, 5-10 fps.",
    ],
    "new_cameras": [
        "Mark doors, gates, the driveway and privacy-sensitive areas on the property plan.",
        "Check each mounting point: about 2.5-3 m high, no sun or bright sky straight into the lens, no wall or eave right in front of the IR lights.",
        "Run and test each cable before mounting the camera.",
        "Activate each camera and set its password (some brands require this before first use).",
        "Give cameras fixed addresses or DHCP reservations so they keep the same address.",
        "Turn on RTSP/ONVIF where the brand ships it turned off.",
        "Set the sub-stream to H.264, about 640x360-640x480, 5-10 fps; keep the main stream for recording.",
    ],
    "drone_first": [
        "Choose a dock location with open sky above, away from trees, wires and where people gather.",
        "Draw the property boundary on the map: the drone geofence stays inside it.",
        "Mark the places a drone should be able to look at (observation points).",
        "Check local drone regulations (pilot certification, flights over people, at night, beyond line of sight) before any real flight.",
        "Flight stays simulated in this build: real flight commands are disabled.",
    ],
}
PATH_CHECKLISTS["complete_package"] = PATH_CHECKLISTS["new_cameras"] + PATH_CHECKLISTS["drone_first"][:4] + [
    "Fit door/window sensors on exterior doors; note that sensor inputs are registration-only in this build.",
]
POWER_NETWORK = [
    "PoE cameras: one cable per camera to a PoE switch or a PoE recorder; keep each run under 90 m of fixed cable.",
    "PoE budget: add up each camera's maximum draw from its datasheet (infrared at night draws more); if unknown, plan 15.4 W per camera.",
    "Wi-Fi cameras: a power outlet within reach, and a strong signal where the camera will hang (2.4 GHz reaches further than 5 GHz).",
    "Outdoor connections: weatherproof junction boxes and drip loops on cables.",
    "Keep cameras on the local network. A separate camera network (VLAN) is fine as long as Sentinel can reach it.",
]


def checklists(path: str) -> dict[str, list[str]]:
    return {"installation": PATH_CHECKLISTS.get(path, PATH_CHECKLISTS["existing_cameras"]) + COMMON_CHECKLIST, "power_network": POWER_NETWORK}


def build_report(*, site_name: str, path: str, property_info: dict[str, Any], plan: dict[str, Any], width_m: float, height_m: float,
                 installed: list[dict[str, Any]], devices: list[dict[str, Any]], recommendation: dict[str, Any] | None) -> dict[str, Any]:
    coverage = analyse_coverage(plan, width_m, height_m, installed)
    return {
        "site_name": site_name, "path": path, "generated_ms": int(time.time() * 1000), "property": property_info,
        "map": {"width_m": width_m, "height_m": height_m}, "plan": plan, "coverage": coverage,
        "estimate": estimate(plan, installed), "checklists": checklists(path), "recommendation": recommendation,
        "inventory": [{"name": d["name"], "category": d["category"], "profile": d.get("profile", ""), "support": d.get("support", ""),
                       "level": d.get("level", "registered")} for d in devices],
        "disclaimer": "Approximate plan for installation. It is not a guarantee of coverage, detection or security, and it is not a product quote.",
    }


# ---- printable report --------------------------------------------------------------------

_COLOURS = {"building": "#9aa3ad", "driveway": "#c9b48a", "garage": "#b49c6c", "yard": "#a9d39e", "parking": "#d8cfa8", "public_area": "#f2c46f",
            "privacy_area": "#e58f8f", "room": "#c6d3ea", "obstruction": "#6f8f5f"}


def plan_svg(report: dict[str, Any]) -> str:
    width, height = report["map"]["width_m"], report["map"]["height_m"]
    coverage = report["coverage"]
    cell = coverage["cell_m"]
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="-1 -1 {width + 2} {height + 2}" class="plan">',
             f'<rect x="0" y="0" width="{width}" height="{height}" fill="#f7f7f2" stroke="#555" stroke-width="0.15"/>']
    for r, line in enumerate(coverage["grid"]):
        for c, value in enumerate(line):
            if value > 0:
                parts.append(f'<rect x="{c * cell:.2f}" y="{r * cell:.2f}" width="{cell:.2f}" height="{cell:.2f}" fill="#3b82f6" opacity="{0.15 + 0.1 * min(value, 3):.2f}"/>')
    esc = html.escape
    for item in report["plan"].get("items", []):
        if "polygon" in item:
            points = " ".join(f"{x},{y}" for x, y in item["polygon"])
            parts.append(f'<polygon points="{points}" fill="{_COLOURS.get(item["kind"], "#ccc")}" fill-opacity="0.7" stroke="#333" stroke-width="0.1"><title>{esc(item["name"])}</title></polygon>')
        elif "line" in item:
            points = " ".join(f"{x},{y}" for x, y in item["line"])
            parts.append(f'<polyline points="{points}" fill="none" stroke="#7a5230" stroke-width="0.3"/>')
    for index, item in enumerate(i for i in report["plan"].get("items", []) if "point" in i):
        x, y = item["point"]
        colour = {"camera": "#1d4ed8", "sensor": "#7c3aed", "network": "#0f766e", "drone_dock": "#b45309", "drone_point": "#b45309"}.get(item["kind"], "#b91c1c")
        parts.append(f'<circle cx="{x}" cy="{y}" r="0.6" fill="{colour}"><title>{esc(item["name"])}</title></circle>')
        parts.append(f'<text x="{x + 0.8}" y="{y + 0.4}" font-size="1.1" fill="#111">{esc(item["name"][:24])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def render_html(report: dict[str, Any]) -> str:
    esc = html.escape
    coverage, estimate_ = report["coverage"], report["estimate"]
    path_label = {"existing_cameras": "Connect existing cameras", "new_cameras": "Install new cameras", "drone_first": "Drone-first installation",
                  "complete_package": "Complete Sentinel package"}.get(report["path"], report["path"] or "Not chosen")

    def ul(values):
        return "<ul>" + "".join(f"<li>{esc(str(value))}</li>" for value in values) + "</ul>" if values else "<p>None.</p>"

    def check(values):
        return "<ul class='check'>" + "".join(f"<li>☐ {esc(value)}</li>" for value in values) + "</ul>"

    rows = "".join(f"<tr><td>{esc(d['name'])}</td><td>{esc(d['category'])}</td><td>{esc(d['support'])}</td><td>{esc(d['level'].replace('_', ' '))}</td></tr>"
                   for d in report["inventory"]) or "<tr><td colspan='4'>No devices registered yet.</td></tr>"
    plan_rows = "".join(f"<tr><td>{esc(i['name'])}</td><td>{esc(ITEM_KINDS.get(i['kind'], i['kind']))}</td><td>{esc(i.get('status', ''))}</td>"
                        f"<td>{esc(i.get('power', i.get('sensor', i.get('equipment', ''))))}</td></tr>"
                        for i in report["plan"].get("items", []) if i["kind"] in ("camera", "sensor", "network", "drone_dock", "drone_point"))
    recommendation = report.get("recommendation") or {}
    rec_rows = "".join(f"<tr><td>{esc(line['role'])}</td><td>{line['count']}</td><td>{'yes' if line['essential'] else 'optional'}</td><td>{esc(line['why'])}</td></tr>"
                       for line in recommendation.get("lines", []))
    features = "".join(f"<li>{esc(f['name'])}: " + (f"in view of {esc(', '.join(f['covered_by']))}" if f.get("covered_by") else
                       f"about {f['covered_pct']}% in view" if "covered_pct" in f else "not in view") + "</li>" for f in coverage["features"])
    property_info = report.get("property") or {}
    generated = time.strftime("%Y-%m-%d %H:%M", time.localtime(report["generated_ms"] / 1000))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Installation plan</title><style>
body{{font:14px/1.45 system-ui,sans-serif;color:#111;background:#fff;margin:24px auto;max-width:900px;padding:0 16px}}
h1{{font-size:22px;margin:0 0 4px}}h2{{font-size:17px;margin:22px 0 6px;border-bottom:1px solid #ccc;padding-bottom:3px}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:4px 6px;text-align:left;vertical-align:top}}
.muted{{color:#555}}.warn{{background:#fff4d6;border:1px solid #e5c56b;padding:8px 10px;border-radius:4px}}
.plan{{width:100%;max-height:520px;border:1px solid #ccc}}ul.check{{list-style:none;padding-left:4px}}
@media print{{body{{margin:0}}h2{{break-after:avoid}}.plan{{max-height:none}}}}
</style></head><body>
<h1>Installation plan: {esc(report['site_name'])}</h1>
<p class="muted">{esc(path_label)} · {esc(PROPERTY_TYPES.get(property_info.get('property_type', ''), ''))} · generated {esc(generated)}</p>
<p class="warn">{esc(report['disclaimer'])}</p>
<h2>Property plan and approximate coverage</h2>
{plan_svg(report)}
<p>{esc(coverage['summary'])} Overlap (two or more cameras): about {coverage['overlap_pct']}%.{f" Indoor rooms in view: about {coverage['indoor_covered_pct']}%." if coverage['indoor_covered_pct'] is not None else ''}</p>
<h3>Priority spots</h3>{('<ul>' + features + '</ul>') if features else '<p>No doors, gates or driveways marked.</p>'}
<h3>Gaps</h3>{ul(coverage['gaps'])}
<h3>Privacy</h3>{ul([p['advice'] for p in coverage['privacy']])}
<h3>Assumptions</h3>{ul(coverage['assumptions'])}
{f"<h2>Recommended cameras</h2><table><tr><th>Where</th><th>Count</th><th>Essential</th><th>Why</th></tr>{rec_rows}</table><p class='muted'>{esc(recommendation.get('disclaimer', ''))}</p>" if rec_rows else ''}
<h2>Planned equipment</h2>
<table><tr><th>Item</th><th>Type</th><th>Status</th><th>Power / kind</th></tr>{plan_rows or "<tr><td colspan='4'>Nothing placed yet.</td></tr>"}</table>
<p>PoE ports needed: <b>{estimate_['poe']['ports_needed']}</b>{f" (a {estimate_['poe']['suggested_switch_ports']}-port PoE switch or PoE recorder leaves a spare)" if estimate_['poe']['ports_needed'] else ''}.
PoE budget: <b>{estimate_['poe']['budget_w']} W</b> {esc(estimate_['poe']['budget_basis'])}. Approximate cable: {estimate_['cable_total_m']} m. Power outlets needed: {estimate_['outlets']}.</p>
{ul(estimate_['warnings'])}
<h2>Device inventory</h2>
<table><tr><th>Device</th><th>Category</th><th>Support</th><th>Status</th></tr>{rows}</table>
<h2>Installation checklist</h2>{check(report['checklists']['installation'])}
<h2>Power and network checklist</h2>{check(report['checklists']['power_network'])}
</body></html>"""
