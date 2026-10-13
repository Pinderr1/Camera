from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from senior_safety.pose_events import point_in_polygon


@dataclass
class Geofence:
    """Inclusion polygon plus optional exclusions, in [lat, lon] pairs. Sentinel
    checks every target before sending it; the flight controller's own fence
    (ArduPilot FENCE_*) must be configured too and is the authoritative one."""

    polygon: list[list[float]]
    max_altitude_m: float = 30.0
    min_altitude_m: float = 5.0
    exclusions: list[list[list[float]]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Geofence":
        return cls(
            polygon=data["polygon"],
            max_altitude_m=float(data.get("max_altitude_m", 30)),
            min_altitude_m=float(data.get("min_altitude_m", 5)),
            exclusions=data.get("exclusions", []),
        )

    def violations(self, lat: float, lon: float, altitude_m: float | None = None) -> list[str]:
        problems = []
        if not point_in_polygon(lon, lat, [[point[1], point[0]] for point in self.polygon]):
            problems.append("outside_geofence")
        for index, zone in enumerate(self.exclusions):
            if point_in_polygon(lon, lat, [[point[1], point[0]] for point in zone]):
                problems.append(f"inside_exclusion_{index}")
        if altitude_m is not None:
            if altitude_m > self.max_altitude_m:
                problems.append("above_max_altitude")
            if altitude_m < self.min_altitude_m:
                problems.append("below_min_altitude")
        return problems
