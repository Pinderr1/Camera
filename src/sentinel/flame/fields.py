"""Fields for agricultural burn monitoring: boundaries, the strip around them
that is watched for heat, and places smoke or fire must stay away from.

Boundaries are stored as [lat, lon] (the truth) and shown on the site map in
metres from the site origin (x east, y south), like every other map object.
Geometry is flat-earth over a few kilometres, which is well inside what a
field needs. Standard library only."""

from __future__ import annotations

import math
import re
import time
from typing import Any

from senior_safety.pose_events import point_in_polygon

from ..drone.base import geo_to_map_point, geo_to_offset, map_point_to_geo

SENSITIVE_KINDS = {
    "home": "Home or building", "road": "Road", "town": "Town or village", "livestock": "Livestock", "power_line": "Power line",
    "fuel": "Fuel or chemical storage", "bales": "Bales or stored crop", "trees": "Shelterbelt or trees", "other": "Other",
}
MAX_VERTICES = 200
MAX_FIELD_HA = 2000.0
_ID = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,40}$")


class FieldError(ValueError):
    """A field the person must correct."""


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _geo_point(value: Any, what: str) -> list[float]:
    if not (isinstance(value, (list, tuple)) and len(value) == 2 and all(_number(v) for v in value)):
        raise FieldError(f"{what} must be [lat, lon]")
    lat, lon = float(value[0]), float(value[1])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise FieldError(f"{what} is not a valid latitude/longitude")
    return [round(lat, 7), round(lon, 7)]


def local(polygon_geo: list[list[float]], origin: tuple[float, float]) -> list[list[float]]:
    """[lat, lon] → [east, north] metres from `origin`."""
    return [list(geo_to_offset(origin, lat, lon)) for lat, lon in polygon_geo]


def area_m2(points: list[list[float]]) -> float:
    total = 0.0
    for i, (x1, y1) in enumerate(points):
        x2, y2 = points[(i + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2


def perimeter_m(points: list[list[float]]) -> float:
    return sum(math.dist(points[i], points[(i + 1) % len(points)]) for i in range(len(points)))


def _segments_cross(a, b, c, d) -> bool:
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    return orient(a, b, c) * orient(a, b, d) < 0 and orient(c, d, a) * orient(c, d, b) < 0


def self_intersects(points: list[list[float]]) -> bool:
    count = len(points)
    for i in range(count):
        for j in range(i + 1, count):
            if abs(i - j) in (1, count - 1):
                continue
            if _segments_cross(points[i], points[(i + 1) % count], points[j], points[(j + 1) % count]):
                return True
    return False


def distance_to_edge_m(point: list[float], polygon: list[list[float]]) -> float:
    """Shortest distance from a point to the polygon's edge (all in metres)."""
    best = math.inf
    px, py = point
    for i, (x1, y1) in enumerate(polygon):
        x2, y2 = polygon[(i + 1) % len(polygon)]
        dx, dy = x2 - x1, y2 - y1
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length2))
        best = min(best, math.hypot(px - (x1 + t * dx), py - (y1 + t * dy)))
    return best


def signed_distance_m(point: list[float], polygon: list[list[float]]) -> float:
    """Negative inside the boundary, positive outside (metres)."""
    distance = distance_to_edge_m(point, polygon)
    return -distance if point_in_polygon(point[0], point[1], polygon) else distance


def nearest_edge_label(point: list[float], polygon: list[list[float]]) -> str:
    """'north-east' etc.: which side of the field a point is on, from the centroid."""
    cx = sum(p[0] for p in polygon) / len(polygon)
    cy = sum(p[1] for p in polygon) / len(polygon)
    bearing = math.degrees(math.atan2(point[0] - cx, point[1] - cy)) % 360
    names = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"]
    return names[int((bearing + 22.5) // 45) % 8]


def bearing_deg(origin: list[float], point: list[float]) -> float:
    """Compass bearing from origin to point (local east/north metres)."""
    return math.degrees(math.atan2(point[0] - origin[0], point[1] - origin[1])) % 360


def clean_field(payload: dict[str, Any], *, site_origin: tuple[float, float] | None, existing: dict[str, Any] | None = None,
                taken: set[str] | None = None, actor: str = "") -> dict[str, Any]:
    """Validate and normalise a field. `boundary` is [[lat, lon], ...] or
    `boundary_map` is [[x, y], ...] in site-map metres (needs the site origin)."""
    if not isinstance(payload, dict):
        raise FieldError("field must be an object")
    field = dict(existing or {})
    name = str(payload.get("name", field.get("name", ""))).strip()
    if not 0 < len(name) <= 60:
        raise FieldError("the field needs a name (1-60 characters)")
    field["name"] = name
    if "boundary_map" in payload and payload["boundary_map"] is not None:
        if site_origin is None:
            raise FieldError("set the property location first (Install tab) to draw a field on the map")
        points = payload["boundary_map"]
        if not isinstance(points, list) or not all(isinstance(p, (list, tuple)) and len(p) == 2 and all(_number(v) for v in p) for p in points):
            raise FieldError("boundary_map must be a list of [x, y] map points in metres")
        boundary = [list(map_point_to_geo(site_origin, float(x), float(y))) for x, y in points]
    elif "boundary" in payload:
        boundary = payload["boundary"]
        if not isinstance(boundary, list):
            raise FieldError("boundary must be a list of [lat, lon] corners")
    else:
        boundary = field.get("boundary")
        if boundary is None:
            raise FieldError("the field needs a boundary: at least three corners")
    boundary = [_geo_point(point, f"corner {index + 1}") for index, point in enumerate(boundary)]
    if len(boundary) >= 2 and boundary[0] == boundary[-1]:
        boundary = boundary[:-1]
    if not 3 <= len(boundary) <= MAX_VERTICES:
        raise FieldError(f"a field boundary needs 3-{MAX_VERTICES} corners")
    points = local(boundary, tuple(boundary[0]))
    if self_intersects(points):
        raise FieldError("the boundary crosses itself: put the corners in order around the field")
    area = area_m2(points)
    if area < 100:
        raise FieldError("the field is smaller than 100 m²: check the corners")
    if area / 10_000 > MAX_FIELD_HA:
        raise FieldError(f"the field is larger than {MAX_FIELD_HA:g} ha: split it into several fields")
    field["boundary"] = boundary
    for key, low, high, default in (("watch_buffer_m", 0, 1000, 100.0), ("edge_m", 2, 100, 15.0), ("fireguard_m", 0, 200, 0.0)):
        value = payload.get(key, field.get(key, default))
        if not _number(value) or not low <= value <= high:
            raise FieldError(f"{key} must be between {low} and {high} metres")
        field[key] = float(value)
    crop = str(payload.get("crop", field.get("crop", ""))).strip()[:40]
    field["crop"] = crop
    field["notes"] = str(payload.get("notes", field.get("notes", ""))).strip()[:500]
    sensitive = payload.get("sensitive", field.get("sensitive", []))
    if not isinstance(sensitive, list) or len(sensitive) > 50:
        raise FieldError("sensitive must be a list of at most 50 places")
    cleaned = []
    for index, place in enumerate(sensitive):
        if not isinstance(place, dict):
            raise FieldError(f"sensitive place {index + 1} must be an object")
        kind = place.get("kind", "other")
        if kind not in SENSITIVE_KINDS:
            raise FieldError(f"sensitive place {index + 1}: kind must be one of {list(SENSITIVE_KINDS)}")
        if "point_map" in place and site_origin is not None and "point" not in place:
            x, y = place["point_map"]
            point = list(map_point_to_geo(site_origin, float(x), float(y)))
        else:
            point = place.get("point")
        cleaned.append({"kind": kind, "label": str(place.get("label") or SENSITIVE_KINDS[kind]).strip()[:60],
                        "point": _geo_point(point, f"sensitive place {index + 1}")})
    field["sensitive"] = cleaned
    now = int(time.time() * 1000)
    if not field.get("id"):
        wanted = str(payload.get("id") or "").strip()
        base = wanted if _ID.match(wanted) else re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:30] or "field"
        candidate, index = base, 2
        while candidate in (taken or set()):
            candidate, index = f"{base}_{index}", index + 1
        field["id"] = candidate
        field["created_ms"] = now
    field["updated_ms"] = now
    field["updated_by"] = actor
    return field


def view(field: dict[str, Any], site_origin: tuple[float, float] | None) -> dict[str, Any]:
    """The field with derived geometry for the dashboard and the assistant."""
    boundary = field["boundary"]
    origin = tuple(boundary[0])
    points = local(boundary, origin)
    data = dict(field)
    data["area_ha"] = round(area_m2(points) / 10_000, 2)
    data["perimeter_m"] = round(perimeter_m(points))
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)
    data["centre"] = [round(v, 7) for v in _to_geo(origin, cx, cy)]
    if site_origin is not None:
        data["boundary_map"] = [[round(v, 1) for v in geo_to_map_point(site_origin, lat, lon)] for lat, lon in boundary]
        data["sensitive_map"] = [place | {"point_map": [round(v, 1) for v in geo_to_map_point(site_origin, *place["point"])]}
                                 for place in field.get("sensitive", [])]
    data["monitored_areas"] = monitored_areas(field)
    return data


def _to_geo(origin: tuple[float, float], east: float, north: float) -> tuple[float, float]:
    from ..drone.base import offset_to_geo

    return offset_to_geo(origin, east, north)


def monitored_areas(field: dict[str, Any]) -> list[dict[str, str]]:
    """Plain description of what is watched for this field."""
    areas = [{"id": "interior", "label": "Inside the boundary", "detail": "heat here is expected during a burn and is logged, not alarmed"},
             {"id": "edge", "label": f"Boundary edge (±{field['edge_m']:g} m)", "detail": "heat this close to the boundary is reported as a warning"}]
    if field["watch_buffer_m"]:
        areas.append({"id": "watch_strip", "label": f"Watch strip ({field['watch_buffer_m']:g} m outside the boundary)",
                      "detail": "heat here is outside the field and triggers an emergency notification"})
    if field.get("sensitive"):
        areas.append({"id": "sensitive", "label": f"{len(field['sensitive'])} sensitive place(s)",
                      "detail": "smoke direction is checked against these, and heat near them is reported"})
    return areas


def corner_from_point(lat: float, lon: float) -> list[float]:
    return _geo_point([lat, lon], "corner")


__all__ = ["FieldError", "SENSITIVE_KINDS", "area_m2", "bearing_deg", "clean_field", "corner_from_point", "distance_to_edge_m", "local",
           "monitored_areas", "nearest_edge_label", "perimeter_m", "self_intersects", "signed_distance_m", "view"]
