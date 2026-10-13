"""Drone integration: the drone is Sentinel's mobile camera, not its brain."""

from __future__ import annotations

from typing import Any, Callable

from .base import DroneAdapter, DroneTelemetry, FlightCommandRejected, map_point_to_geo
from .geofence import Geofence
from .manager import DroneManager, DroneSafetyConfig, InspectionRequest

__all__ = [
    "DroneAdapter",
    "DroneManager",
    "DroneTelemetry",
    "FlightCommandRejected",
    "Geofence",
    "InspectionRequest",
    "build_drone_manager",
]


def build_drone_manager(
    config: dict[str, Any],
    *,
    audit: Callable[[str, str, dict[str, Any]], None],
    on_change: Callable[[dict[str, Any]], None],
) -> DroneManager | None:
    drone = config.get("drone", {})
    if not drone.get("enabled", False):
        return None
    origin = tuple(config["site"]["map"]["origin_geo"])
    home = map_point_to_geo(origin, *drone["home_map"]) if "home_map" in drone else origin
    fence = dict(drone["geofence"])
    if "map_polygon" in fence:
        fence["polygon"] = [list(map_point_to_geo(origin, x, y)) for x, y in fence.pop("map_polygon")]
    if drone.get("adapter", "simulated") == "simulated":
        from .mock import SimulatedDrone

        adapter: DroneAdapter = SimulatedDrone(home, **drone.get("simulator", {}))
    elif drone["adapter"] == "mavlink":
        from .mavlink import MavlinkTelemetryAdapter

        adapter = MavlinkTelemetryAdapter(drone["connection"])
    else:
        raise ValueError(f"unknown drone adapter {drone['adapter']!r}")
    adapter.connect()
    return DroneManager(
        adapter,
        Geofence.from_dict(fence),
        origin,
        DroneSafetyConfig.from_dict(drone.get("safety")),
        audit=audit,
        on_change=on_change,
    )
