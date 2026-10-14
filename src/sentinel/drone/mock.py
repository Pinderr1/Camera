from __future__ import annotations

import math
import time

from .base import DroneAdapter, DroneTelemetry, FlightCommandRejected, geo_to_offset, offset_to_geo


class SimulatedDrone(DroneAdapter):
    """Kinematic stand-in for a docked quadcopter: climbs, flies straight lines,
    drains and recharges its battery, and performs RTL on link loss the way an
    ArduPilot failsafe would. Good enough to exercise approvals, geofencing and
    safety logic; not a flight-dynamics model (use ArduPilot SITL for that)."""

    kind = "simulated"
    is_physical = False

    def __init__(
        self,
        home: tuple[float, float],
        *,
        cruise_speed_mps: float = 6.0,
        climb_rate_mps: float = 2.5,
        drain_pct_per_s: float = 0.12,
        charge_pct_per_s: float = 0.3,
        link_loss_failsafe_s: float = 5.0,
        video_url: str = "",
    ) -> None:
        self.home = home
        self.cruise_speed = cruise_speed_mps
        self.climb_rate = climb_rate_mps
        self.drain = drain_pct_per_s
        self.charge = charge_pct_per_s
        self.link_loss_failsafe_s = link_loss_failsafe_s
        self.video_url = video_url
        self.connected = False
        self.link_ok = True
        self._link_lost_s = 0.0
        self.battery = 100.0
        self.east = 0.0
        self.north = 0.0
        self.alt = 0.0
        self.heading = 0.0
        self.speed = 0.0
        self.mode = "docked"
        self.armed = False
        self._target: tuple[float, float, float] | None = None
        self._last_heartbeat_ms = 0

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def set_link(self, ok: bool) -> None:
        self.link_ok = ok

    def telemetry(self) -> DroneTelemetry:
        lat, lon = offset_to_geo(self.home, self.east, self.north)
        docked = self.mode == "docked"
        return DroneTelemetry(
            connected=self.connected,
            armed=self.armed,
            in_air=self.alt > 0.2,
            flight_mode=self.mode,
            battery_pct=round(self.battery, 1),
            battery_voltage=round(13.2 + 3.6 * self.battery / 100, 2),
            lat=lat,
            lon=lon,
            alt_m=round(self.alt, 1),
            heading_deg=round(self.heading, 1),
            ground_speed_mps=round(self.speed, 1),
            gps_fix=3 if self.connected else 0,
            satellites=14 if self.connected else 0,
            docked=docked,
            charging=docked and self.battery < 100,
            link_ok=self.connected and self.link_ok,
            last_heartbeat_ms=self._last_heartbeat_ms,
            video_url=self.video_url,
            home_lat=self.home[0],
            home_lon=self.home[1],
            telemetry_ms=int(time.time() * 1000),
        )

    def takeoff(self, altitude_m: float) -> None:
        self._require_link()
        if self.alt > 0.2:
            raise FlightCommandRejected("already airborne")
        self.armed = True
        self.mode = "takeoff"
        self._target = (self.east, self.north, altitude_m)

    def goto(self, lat: float, lon: float, altitude_m: float) -> None:
        self._require_link()
        if self.alt <= 0.2:
            raise FlightCommandRejected("take off first")
        east, north = geo_to_offset(self.home, lat, lon)
        self.mode = "enroute"
        self._target = (east, north, altitude_m)

    def hold(self) -> None:
        self._require_link()
        if self.alt > 0.2:
            self.mode = "holding"
            self._target = (self.east, self.north, self.alt)

    def return_to_home(self) -> None:
        if self.alt <= 0.2:
            return
        self.mode = "returning"
        self._target = (0.0, 0.0, max(self.alt, 10.0))

    def land(self) -> None:
        if self.alt <= 0.2:
            return
        self.mode = "landing"
        self._target = (self.east, self.north, 0.0)

    def step(self, dt_s: float) -> None:
        if not self.connected:
            return
        if self.link_ok:
            self._last_heartbeat_ms = int(time.time() * 1000)
            self._link_lost_s = 0.0
        else:
            self._link_lost_s += dt_s
            if self._link_lost_s >= self.link_loss_failsafe_s and self.alt > 0.2 and self.mode not in {"returning", "landing"}:
                self.return_to_home()

        if self.mode == "docked":
            self.battery = min(100.0, self.battery + self.charge * dt_s)
            return
        if self.alt > 0.2 or self.mode == "takeoff":
            self.battery = max(0.0, self.battery - self.drain * dt_s)
        if self._target is None:
            return

        target_east, target_north, target_alt = self._target
        if self.mode == "returning" and math.hypot(self.east, self.north) < 0.5:
            self.mode = "landing"
            self._target = (0.0, 0.0, 0.0)
            target_east, target_north, target_alt = self._target

        if self.mode in {"takeoff", "landing"}:
            delta = target_alt - self.alt
            self.alt += max(-self.climb_rate * dt_s, min(self.climb_rate * dt_s, delta))
            self.speed = 0.0
            if abs(target_alt - self.alt) < 0.05:
                self.alt = target_alt
                if self.mode == "takeoff":
                    self.mode = "holding"
                elif self.at_home():
                    self.mode, self.armed, self._target, self.alt = "docked", False, None, 0.0
                else:
                    self.mode, self.armed, self._target = "landed", False, None
            return

        dx, dy = target_east - self.east, target_north - self.north
        distance = math.hypot(dx, dy)
        step = self.cruise_speed * dt_s
        if distance <= step:
            self.east, self.north = target_east, target_north
            self.speed = 0.0
            if self.mode == "enroute":
                self.mode = "holding"
        else:
            self.east += dx / distance * step
            self.north += dy / distance * step
            self.heading = (math.degrees(math.atan2(dx, dy)) + 360) % 360
            self.speed = self.cruise_speed
        self.alt += max(-self.climb_rate * dt_s, min(self.climb_rate * dt_s, target_alt - self.alt))

    def at_home(self) -> bool:
        return math.hypot(self.east, self.north) < 1.0

    def _require_link(self) -> None:
        if not (self.connected and self.link_ok):
            raise FlightCommandRejected("no command link")
