from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any

EARTH_M_PER_DEG = 111_320.0


@dataclass
class DroneTelemetry:
    connected: bool = False
    armed: bool = False
    in_air: bool = False
    flight_mode: str = "unknown"
    battery_pct: float | None = None
    battery_voltage: float | None = None
    lat: float | None = None
    lon: float | None = None
    alt_m: float = 0.0  # relative to home
    heading_deg: float = 0.0
    ground_speed_mps: float = 0.0
    gps_fix: int = 0  # 0 none, 2 2D, 3 3D or better
    satellites: int = 0
    docked: bool = False
    charging: bool = False
    link_ok: bool = False
    last_heartbeat_ms: int = 0
    video_url: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class FlightCommandRejected(RuntimeError):
    pass


class DroneAdapter(ABC):
    """Boundary between Sentinel and a flight stack. Sentinel only asks for
    high-level actions; stabilisation, failsafes, and landing belong to the
    flight controller (ArduPilot/PX4) or, here, the simulator."""

    kind = "abstract"
    is_physical = False

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def telemetry(self) -> DroneTelemetry: ...

    @abstractmethod
    def takeoff(self, altitude_m: float) -> None: ...

    @abstractmethod
    def goto(self, lat: float, lon: float, altitude_m: float) -> None: ...

    @abstractmethod
    def hold(self) -> None: ...

    @abstractmethod
    def return_to_home(self) -> None: ...

    @abstractmethod
    def land(self) -> None: ...

    def step(self, dt_s: float) -> None:
        """Advance simulated time. Real adapters receive state asynchronously."""


def offset_to_geo(origin: tuple[float, float], east_m: float, north_m: float) -> tuple[float, float]:
    lat0, lon0 = origin
    lat = lat0 + north_m / EARTH_M_PER_DEG
    lon = lon0 + east_m / (EARTH_M_PER_DEG * math.cos(math.radians(lat0)))
    return lat, lon


def geo_to_offset(origin: tuple[float, float], lat: float, lon: float) -> tuple[float, float]:
    lat0, lon0 = origin
    north = (lat - lat0) * EARTH_M_PER_DEG
    east = (lon - lon0) * EARTH_M_PER_DEG * math.cos(math.radians(lat0))
    return east, north


def map_point_to_geo(origin: tuple[float, float], x_m: float, y_m: float) -> tuple[float, float]:
    """Site map coordinates are metres with x east and y south (screen-style)."""
    return offset_to_geo(origin, x_m, -y_m)
