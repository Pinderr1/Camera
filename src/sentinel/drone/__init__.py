"""Drone integration: the drone is Sentinel's mobile camera, not its brain."""

from __future__ import annotations

from typing import Any, Callable

from . import catalog
from .base import DroneAdapter, DroneTelemetry, FlightCommandRejected, map_point_to_geo
from .geofence import Geofence
from .manager import DroneManager, DroneSafetyConfig, InspectionRequest, WeatherLimits

__all__ = [
    "DroneAdapter",
    "DroneManager",
    "DroneTelemetry",
    "FlightCommandRejected",
    "Geofence",
    "InspectionRequest",
    "build_drone_fleet",
    "build_drone_manager",
]


def build_drone_manager(
    config: dict[str, Any],
    *,
    audit: Callable[[str, str, dict[str, Any]], None],
    on_change: Callable[[dict[str, Any]], None],
    device: dict[str, Any] | None = None,
    records: Any = None,
) -> DroneManager | None:
    """One manager for one aircraft. `device` is a registered drone from
    `devices[]`; without it the legacy `drone` section describes the aircraft."""
    drone = config.get("drone", {})
    if not drone.get("enabled", False):
        return None
    aircraft = device or drone
    origin = tuple(config["site"]["map"]["origin_geo"])
    position = (device or {}).get("map")  # a registered drone's home (dock) position on the property map
    home_map = [position["x"], position["y"]] if position else drone.get("home_map")
    home = map_point_to_geo(origin, *home_map) if home_map else origin
    fence = dict(drone["geofence"])
    if "map_polygon" in fence:
        fence["polygon"] = [list(map_point_to_geo(origin, x, y)) for x, y in fence.pop("map_polygon")]
    adapter_kind = aircraft.get("adapter", "simulated")
    model = catalog.model_for(aircraft)
    link = {"gcs_system_id": int(drone.get("gcs_system_id", 245))}
    if adapter_kind == "simulated":
        from .mock import SimulatedDrone

        adapter: DroneAdapter = SimulatedDrone(home, **(catalog.simulator_settings(model) | drone.get("simulator", {})))
    elif adapter_kind == "mavlink":
        from .mavlink import MavlinkTelemetryAdapter

        adapter = MavlinkTelemetryAdapter(aircraft["connection"], **link)  # telemetry only: flight commands are refused
    elif adapter_kind == "sitl":
        from .mavlink import SitlMavlinkAdapter

        # Commands only for a verified simulator on a loopback link (see mavlink.py).
        adapter = SitlMavlinkAdapter(aircraft["connection"], allow_flight_commands=bool(aircraft.get("allow_flight_commands", False)), **link)
    else:
        raise ValueError(f"unknown drone adapter {adapter_kind!r}")
    adapter.connect()
    return DroneManager(
        adapter,
        Geofence.from_dict(fence),
        origin,
        DroneSafetyConfig.from_dict(drone.get("safety")),
        audit=audit,
        on_change=on_change,
        drone_id=(device or {}).get("id", "drone"),
        home=home,
        records=records,
        weather=WeatherLimits.from_dict(catalog.weather_defaults(model) | (drone.get("weather") or {})),
        firmware_tested=tuple(drone.get("firmware_tested") or model["firmware_tested"]),
        model=model,
        options=catalog.options_for(model, aircraft),
        gcs_system_id=link["gcs_system_id"],
    )


def build_drone_fleet(
    config: dict[str, Any],
    *,
    audit: Callable[[str, str, dict[str, Any]], None],
    on_change: Callable[[dict[str, Any]], None],
    records: Any = None,
) -> tuple[str, dict[str, DroneManager]]:
    """(primary id, managers by device id). The primary drone takes inspection
    requests and publishes on the bus; others report telemetry and readiness.
    A config with no registered drones keeps the legacy single `drone` section
    under the id "drone"."""
    from ..devices import drone_devices, has_registered_drones, primary_drone_id

    if not config.get("drone", {}).get("enabled", False):
        return "", {}
    if not has_registered_drones(config):
        manager = build_drone_manager(config, audit=audit, on_change=on_change, records=records)
        return ("drone", {"drone": manager}) if manager else ("", {})
    registered = drone_devices(config)  # planned hardware is registered but never given an adapter
    primary = primary_drone_id(config)
    fleet: dict[str, DroneManager] = {}
    for device in registered:
        fleet[device["id"]] = build_drone_manager(
            config, audit=lambda actor, action, body, d=device["id"]: audit(actor, action, body | {"drone": d}),
            on_change=on_change if device["id"] == primary else (lambda status: None), device=device, records=records,
        )
    return primary, fleet
