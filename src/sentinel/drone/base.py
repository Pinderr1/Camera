from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
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
    # Richer state from real flight controllers; None means "not reported".
    vehicle_state: str = ""  # MAV_STATE name: standby, active, critical, emergency, ...
    landed_state: str = ""  # on_ground | in_air | takeoff | landing ("" when not reported)
    roll_deg: float | None = None
    pitch_deg: float | None = None
    gimbal_pitch_deg: float | None = None  # reported camera pitch (negative = down), None if no gimbal report
    gimbal_yaw_deg: float | None = None  # relative to the vehicle heading
    home_lat: float | None = None
    home_lon: float | None = None
    hdop: float | None = None
    ekf_ok: bool | None = None
    prearm_ok: bool | None = None
    wind_mps: float | None = None
    temperature_c: float | None = None
    autopilot_version: str = ""
    sitl: bool | None = None  # True only when the vehicle proved it is a simulator (SIM_* parameters)
    messages: list[str] = field(default_factory=list)  # recent STATUSTEXT, newest last
    message_count: int = 0  # STATUSTEXT messages received so far (to find new ones)
    telemetry_ms: int = 0  # when the newest position/attitude sample arrived
    # When each signal last arrived (0 = never), so a value that stopped updating
    # is not mistaken for a current one. Hub clock, milliseconds.
    gps_ms: int = 0
    battery_ms: int = 0
    gimbal_ms: int = 0
    ekf_ms: int = 0
    battery_current_a: float | None = None
    position_rejected: int = 0  # position samples ignored as invalid (0,0 or out of range)
    position_jumps: int = 0  # accepted samples that moved implausibly far since the previous one
    last_jump_ms: int = 0
    battery_rejected: int = 0  # battery samples ignored as implausible
    fc_reboots: int = 0  # flight-controller restarts seen on this link (boot time went backwards)
    link_signed: bool | None = None  # the aircraft signs its MAVLink 2 frames (None until a frame is seen)
    other_vehicles: list[str] = field(default_factory=list)  # "system/component" of other autopilots heard on the link (id conflict, second aircraft)
    boot_ms: int = 0  # the flight controller's own time since boot, from its newest timed message
    link_delay_ms: int | None = None  # how much later than the fastest seen the newest timed message arrived (added latency)
    battery_state: str = ""  # the flight controller's own verdict: ok | low | critical | emergency | failed ("" = not reported)
    fence_breach: str = ""  # the flight controller's fence verdict (FENCE_STATUS): "" none/not reported | min_alt | max_alt | boundary
    fence_breaches: int = 0  # breach count reported by the flight controller

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
    flight_controller = ""  # "ardupilot" when parameters can be audited

    def commands_allowed(self) -> tuple[bool, str]:
        """(may Sentinel send flight commands, why). Physical aircraft: never in
        this build. Only simulators may say yes."""
        return (not self.is_physical, "simulator" if not self.is_physical else "physical aircraft: telemetry only")

    def parameters(self) -> dict[str, float]:
        """Flight-controller parameters read from the vehicle (read-only)."""
        return {}

    def fence_polygon(self) -> list[list[float]] | None:
        """The polygon fence loaded on the flight controller as [lat, lon]
        pairs, None when unknown or not applicable."""
        return None

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


def valid_coordinates(lat: float | None, lon: float | None) -> bool:
    """A position a flight controller can really be at: in range, and not the
    (0, 0) that ArduPilot reports before it has a position."""
    if lat is None or lon is None or not (math.isfinite(lat) and math.isfinite(lon)):
        return False
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0 and not (abs(lat) < 1e-7 and abs(lon) < 1e-7)


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Horizontal distance between two [lat, lon] points (flat-earth; fine over a property)."""
    east, north = geo_to_offset(a, b[0], b[1])
    return math.hypot(east, north)


def geo_to_map_point(origin: tuple[float, float], lat: float, lon: float) -> tuple[float, float]:
    east, north = geo_to_offset(origin, lat, lon)
    return east, -north


def map_point_to_geo(origin: tuple[float, float], x_m: float, y_m: float) -> tuple[float, float]:
    """Site map coordinates are metres with x east and y south (screen-style)."""
    return offset_to_geo(origin, x_m, -y_m)
