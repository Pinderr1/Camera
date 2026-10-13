"""ArduPilot/PX4 MAVLink adapter: TELEMETRY ONLY in this build.

Reads heartbeat, battery, GPS and position so a real drone (or ArduPilot SITL)
can be monitored on the dashboard. Every flight command is refused. Enabling
commands is a deliberate future milestone gated on the checklist in
docs/drone-integration.md (FC failsafes configured and bench-tested, FC-side
geofence, operator authorization, local aviation rules). Do not remove the
refusal to "try it out"."""

from __future__ import annotations

import math
import threading
import time
from typing import Any

from .base import DroneAdapter, DroneTelemetry, FlightCommandRejected

MAV_MODE_FLAG_SAFETY_ARMED = 128
REFUSAL = "physical flight commands are disabled in this build (telemetry-only MAVLink adapter)"


def apply_mavlink_message(telemetry: DroneTelemetry, message: Any, now_ms: int) -> None:
    kind = message.get_type()
    if kind == "HEARTBEAT":
        telemetry.armed = bool(message.base_mode & MAV_MODE_FLAG_SAFETY_ARMED)
        telemetry.flight_mode = f"custom_{message.custom_mode}"
        telemetry.last_heartbeat_ms = now_ms
        telemetry.link_ok = True
    elif kind == "SYS_STATUS":
        if message.battery_remaining >= 0:
            telemetry.battery_pct = float(message.battery_remaining)
        if message.voltage_battery not in (0, 65535):
            telemetry.battery_voltage = message.voltage_battery / 1000
    elif kind == "GLOBAL_POSITION_INT":
        telemetry.lat = message.lat / 1e7
        telemetry.lon = message.lon / 1e7
        telemetry.alt_m = message.relative_alt / 1000
        telemetry.in_air = telemetry.alt_m > 0.5
        if message.hdg != 65535:
            telemetry.heading_deg = message.hdg / 100
        telemetry.ground_speed_mps = math.hypot(message.vx, message.vy) / 100
    elif kind == "GPS_RAW_INT":
        telemetry.gps_fix = int(message.fix_type)
        telemetry.satellites = int(message.satellites_visible)


class MavlinkTelemetryAdapter(DroneAdapter):
    kind = "mavlink"
    is_physical = True

    def __init__(self, connection: str, *, heartbeat_timeout_s: float = 3.0) -> None:
        self.connection_string = connection
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self._telemetry = DroneTelemetry()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def connect(self) -> None:
        from pymavlink import mavutil  # optional dependency: pip install pymavlink

        link = mavutil.mavlink_connection(self.connection_string)
        self._stop.clear()
        self._thread = threading.Thread(target=self._read_loop, args=(link,), name="mavlink-telemetry", daemon=True)
        self._thread.start()
        self._telemetry.connected = True

    def disconnect(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._telemetry.connected = False

    def telemetry(self) -> DroneTelemetry:
        with self._lock:
            snapshot = DroneTelemetry(**self._telemetry.as_dict())
        if snapshot.last_heartbeat_ms and time.time() * 1000 - snapshot.last_heartbeat_ms > self.heartbeat_timeout_s * 1000:
            snapshot.link_ok = False
        return snapshot

    def _read_loop(self, link: Any) -> None:
        while not self._stop.is_set():
            message = link.recv_match(blocking=True, timeout=1)
            if message is None:
                continue
            with self._lock:
                apply_mavlink_message(self._telemetry, message, int(time.time() * 1000))
        link.close()

    def takeoff(self, altitude_m: float) -> None:
        raise FlightCommandRejected(REFUSAL)

    def goto(self, lat: float, lon: float, altitude_m: float) -> None:
        raise FlightCommandRejected(REFUSAL)

    def hold(self) -> None:
        raise FlightCommandRejected(REFUSAL)

    def return_to_home(self) -> None:
        raise FlightCommandRejected(REFUSAL)

    def land(self) -> None:
        raise FlightCommandRejected(REFUSAL)
