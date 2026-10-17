"""Keeping the aircraft away from people. Standard library only.

People's safety, the owner's included, comes before any inspection. The
aircraft never flies to a person, over a person, or closer than
`person_standoff_m` (horizontal) to anywhere a person was reported:

- an incident that involves a person: the zone it happened in;
- the fixed cameras: zones where a person is present right now;
- the drone's own video: people it observed, with their position uncertainty.

So an inspection of a place where a person is looks from a distance: the
hover point (the "station") moves back along the line toward home until it is
clear of every reported person by the standoff. A route whose path passes near
a reported person is refused. In flight, a person reported inside the standoff
stops the aircraft (simulators) or tells the pilot to move away (real aircraft).

Positions from cameras are zone-level and positions from drone video carry
uncertainty; both are treated as areas, never as exact points. Absence of a
report does not mean nobody is there: the pilot remains responsible for
keeping clear of people they can see."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

from .base import geo_to_offset, offset_to_geo

DEFAULT_STANDOFF_M = 30.0  # Canadian basic operations: more than 30 m horizontally from bystanders, never over them
MIN_STANDOFF_M = 10.0  # the configurable floor: the standoff can be made larger, never removed
DRONE_SIGHTING_MAX_AGE_MS = 15_000


@dataclass(frozen=True)
class PersonArea:
    source: str  # incident | camera | drone_video
    label: str = ""
    polygon: tuple[tuple[float, float], ...] = ()  # (lat, lon) corners, for zone-level reports
    point: tuple[float, float] | None = None  # (lat, lon), for drone observations
    radius_m: float = 0.0  # position uncertainty around `point`

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "label": self.label, "polygon": [list(p) for p in self.polygon],
                "point": list(self.point) if self.point else None, "radius_m": round(self.radius_m, 1)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PersonArea":
        return cls(data.get("source", ""), data.get("label", ""), tuple(tuple(p) for p in data.get("polygon") or ()),
                   tuple(data["point"]) if data.get("point") else None, float(data.get("radius_m", 0.0)))


def distance_to_area_m(origin: tuple[float, float], area: PersonArea, lat: float, lon: float) -> float:
    """Horizontal distance from (lat, lon) to the nearest point where the person may be (0 inside)."""
    here = geo_to_offset(origin, lat, lon)
    if area.point is not None:
        there = geo_to_offset(origin, *area.point)
        return max(0.0, math.hypot(here[0] - there[0], here[1] - there[1]) - area.radius_m)
    corners = [geo_to_offset(origin, *corner) for corner in area.polygon]
    if len(corners) < 3:
        return math.inf
    return _polygon_distance(here, corners)


def nearest(origin: tuple[float, float], areas: Sequence[PersonArea], lat: float, lon: float) -> tuple[float, PersonArea | None]:
    best, which = math.inf, None
    for area in areas:
        distance = distance_to_area_m(origin, area, lat, lon)
        if distance < best:
            best, which = distance, area
    return best, which


def station_for(home: tuple[float, float], target: tuple[float, float], areas: Sequence[PersonArea], standoff_m: float,
                step_m: float = 1.0) -> tuple[tuple[float, float] | None, float]:
    """Hover point for inspecting `target` while staying `standoff_m` from every reported
    person: the target itself if it is clear, else the first clear point on the line
    from the target back toward home. (None, 0) when not even home is clear."""
    if nearest(home, areas, *target)[0] >= standoff_m:
        return target, 0.0
    east, north = geo_to_offset(target, *home)
    length = math.hypot(east, north)
    steps = max(1, int(length / step_m))
    for index in range(1, steps + 1):
        point = offset_to_geo(target, east * index / steps, north * index / steps)
        if nearest(home, areas, *point)[0] >= standoff_m:
            return point, length * index / steps
    return None, 0.0


def path_conflicts(home: tuple[float, float], station: tuple[float, float], areas: Sequence[PersonArea], standoff_m: float,
                   step_m: float = 1.0) -> tuple[float, float]:
    """(closest approach to a reported person along home -> station, ignoring the
    take-off/landing column within `standoff_m` of home; that closest distance near home)."""
    east, north = geo_to_offset(home, *station)
    length = math.hypot(east, north)
    steps = max(1, int(length / step_m))
    en_route, at_home = math.inf, nearest(home, areas, *home)[0]
    for index in range(steps + 1):
        along = length * index / steps
        point = offset_to_geo(home, east * index / steps, north * index / steps)
        distance = nearest(home, areas, *point)[0]
        if along <= standoff_m:
            at_home = min(at_home, distance)
        else:
            en_route = min(en_route, distance)
    return en_route, at_home


def _polygon_distance(point: tuple[float, float], polygon: list[tuple[float, float]]) -> float:
    x, y = point
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    if inside:
        return 0.0
    best = math.inf
    for index in range(len(polygon)):
        (ax, ay), (bx, by) = polygon[index], polygon[(index + 1) % len(polygon)]
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length2))
        best = min(best, math.hypot(x - ax - t * dx, y - ay - t * dy))
    return best
