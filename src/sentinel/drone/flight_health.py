"""Is the telemetry Sentinel shows about an aircraft current, plausible and
good enough to fly on? Judged from telemetry alone, before and during flight.

The flight controller owns the response to every failure (its failsafes act
without the Hub). This module only decides what Sentinel may *claim* and what
it must tell people:

- a value that stopped updating is reported with its age, never as current;
- a position of (0, 0), a jump no aircraft can fly, or a battery reading above
  100 % is a sensor or estimator fault, not a fact about the aircraft;
- when navigation has failed, return-to-home may not be possible: ArduPilot's
  EKF failsafe lands or holds altitude instead, and the pilot may have to fly
  manually. Sentinel never says "returning home" in that state.

Every condition carries the evidence it was derived from. Times come from the
caller (`now_ms`), so replays and tests are deterministic. Standard library only."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base import DroneTelemetry, distance_m, valid_coordinates

SEVERITIES = ("info", "warn", "critical")


@dataclass
class HealthLimits:
    position_stale_s: float = 3.0
    gps_stale_s: float = 5.0
    ekf_stale_s: float = 5.0
    battery_stale_s: float = 10.0
    battery_frozen_s: float = 90.0  # an unchanged level for this long in flight looks like a stuck monitor
    gimbal_stale_s: float = 2.0
    min_satellites: int = 6
    max_hdop: float = 2.0
    jump_window_s: float = 30.0
    reboot_window_s: float = 120.0
    home_mismatch_m: float = 25.0
    link_flap_window_s: float = 60.0
    link_flaps_warn: int = 3
    drain_window_s: float = 30.0  # observe at least this long before judging the drain rate
    drain_factor_warn: float = 1.5  # observed drain this many times the model's estimate
    link_delay_warn_ms: int = 1500

    @classmethod
    def from_dict(cls, data: dict | None) -> "HealthLimits":
        data = data or {}
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


@dataclass
class _BatterySample:
    ms: int
    pct: float


@dataclass
class FlightHealthMonitor:
    """Feed every telemetry snapshot to `update`; read `conditions`."""

    limits: HealthLimits = field(default_factory=HealthLimits)
    endurance_min_est: float = 0.0  # the model's estimated hover time; 0 = unknown (no drain comparison)
    cruise_mps: float = 5.0
    configured_home: tuple[float, float] | None = None
    reserve_pct: float = 30.0
    max_wind_mps: float | None = None  # the model's wind limit; compared with the flight controller's own estimate in flight
    conditions: list[dict[str, Any]] = field(default_factory=list)
    _link_was_ok: bool | None = None
    _link_drops: list[int] = field(default_factory=list)
    _reboots_seen: int = 0
    _reboot_ms: int = 0
    _battery: list[_BatterySample] = field(default_factory=list)
    _battery_changed_ms: int = 0
    _battery_last: float | None = None
    _flying_since_ms: int = 0

    # ---- update ----------------------------------------------------------------

    def update(self, telemetry: DroneTelemetry, now_ms: int) -> list[dict[str, Any]]:
        self._track(telemetry, now_ms)
        found: list[dict[str, Any]] = []
        add = lambda cid, severity, text, **evidence: found.append({"id": cid, "severity": severity, "text": text, "evidence": evidence})  # noqa: E731
        airborne = telemetry.in_air
        if not telemetry.connected:
            self.conditions = []
            return []
        if not telemetry.link_ok:
            age = _age_s(telemetry.last_heartbeat_ms, now_ms)
            add("link_lost", "critical" if airborne else "warn",
                "No telemetry from the aircraft" + (f" for {age:.0f} s" if age is not None else "") +
                (": everything shown is from before the link was lost. The flight controller's own failsafe decides what the aircraft does."
                 if airborne else "."), heartbeat_age_s=age)
        recent_drops = [ms for ms in self._link_drops if now_ms - ms <= self.limits.link_flap_window_s * 1000]
        if len(recent_drops) >= self.limits.link_flaps_warn:
            add("link_unstable", "warn", f"The telemetry link dropped {len(recent_drops)} times in the last "
                f"{self.limits.link_flap_window_s:g} s: expect gaps and delays in what Sentinel shows.", drops=len(recent_drops))
        if telemetry.link_ok:
            if (telemetry.link_delay_ms or 0) > self.limits.link_delay_warn_ms:
                add("link_delayed", "critical" if airborne else "warn",
                    f"Telemetry is arriving about {telemetry.link_delay_ms / 1000:.1f} s late: what Sentinel shows lags the aircraft.",
                    delay_ms=telemetry.link_delay_ms)
            self._navigation(telemetry, now_ms, airborne, add)
            self._battery_conditions(telemetry, now_ms, airborne, add)
            if airborne and self.max_wind_mps and telemetry.wind_mps is not None and telemetry.wind_mps > self.max_wind_mps:
                add("wind_over_limit", "warn", f"The flight controller estimates the wind at {telemetry.wind_mps:g} m/s, above this "
                    f"aircraft's {self.max_wind_mps:g} m/s limit (an estimate): expect drift and faster battery use.",
                    wind_mps=telemetry.wind_mps, limit_mps=self.max_wind_mps)
        if telemetry.link_ok and telemetry.fence_breach:
            where = {"max_alt": "above the altitude ceiling", "min_alt": "below the minimum altitude", "boundary": "outside the fence polygon"}
            add("fence_breach", "critical" if airborne else "warn",
                f"The flight controller reports a fence breach ({where.get(telemetry.fence_breach, telemetry.fence_breach)})"
                + (": its fence action (return or land) is in charge." if airborne else ": the aircraft is outside its fence on the ground."),
                breach=telemetry.fence_breach, count=telemetry.fence_breaches)
        if telemetry.vehicle_state in ("critical", "emergency"):
            add("vehicle_emergency", "critical", f"The flight controller reports its state as {telemetry.vehicle_state}.",
                vehicle_state=telemetry.vehicle_state)
        if self._reboot_ms and now_ms - self._reboot_ms <= self.limits.reboot_window_s * 1000:
            add("fc_restarted", "critical" if airborne else "warn",
                "The flight controller restarted" + (" in flight" if airborne else "") +
                ": its home position and settings are re-read before Sentinel trusts them again.", reboots=telemetry.fc_reboots)
        if telemetry.other_vehicles:
            add("other_vehicle", "warn", f"Another autopilot is on the same link ({', '.join(telemetry.other_vehicles)}): "
                "two aircraft, or two with the same system id. Sentinel follows only the first one it heard.",
                others=list(telemetry.other_vehicles))
        if telemetry.gimbal_ms and telemetry.link_ok:
            age = _age_s(telemetry.gimbal_ms, now_ms)
            if age is not None and age > self.limits.gimbal_stale_s:
                add("gimbal_stale", "warn", f"The gimbal stopped reporting its angle {age:.0f} s ago: ground positions use the "
                    "configured camera angle until it reports again.", age_s=age)
        self.conditions = found
        return found

    def _track(self, telemetry: DroneTelemetry, now_ms: int) -> None:
        link = telemetry.connected and telemetry.link_ok
        if self._link_was_ok and not link:
            self._link_drops = [ms for ms in self._link_drops if now_ms - ms <= self.limits.link_flap_window_s * 1000] + [now_ms]
        self._link_was_ok = link
        if telemetry.fc_reboots > self._reboots_seen:
            self._reboot_ms = now_ms
        self._reboots_seen = telemetry.fc_reboots
        flying = telemetry.in_air and telemetry.armed
        if flying and not self._flying_since_ms:
            self._flying_since_ms, self._battery = now_ms, []
        elif not flying:
            self._flying_since_ms = 0
        pct = telemetry.battery_pct
        if pct is not None and telemetry.battery_ms:
            if self._battery_last is None or abs(pct - self._battery_last) >= 0.5:
                self._battery_changed_ms, self._battery_last = telemetry.battery_ms, pct
            if flying and (not self._battery or self._battery[-1].ms != telemetry.battery_ms):
                self._battery.append(_BatterySample(telemetry.battery_ms, pct))
                del self._battery[:-600]

    # ---- navigation ------------------------------------------------------------

    def _navigation(self, telemetry: DroneTelemetry, now_ms: int, airborne: bool, add: Any) -> None:
        limits = self.limits
        position_age = _age_s(telemetry.telemetry_ms, now_ms)
        if not valid_coordinates(telemetry.lat, telemetry.lon):
            add("position_unknown", "critical" if airborne else "warn",
                "The aircraft has not reported a valid position" + (" (it sends 0,0 until it has one)" if telemetry.position_rejected else "") + ".",
                rejected_samples=telemetry.position_rejected)
        elif position_age is not None and position_age > limits.position_stale_s:
            add("position_stale", "critical" if airborne else "warn",
                f"The position shown is {position_age:.0f} s old: position reports stopped arriving.", age_s=position_age)
        if telemetry.gps_fix < 3:
            add("gps_no_fix", "critical" if airborne else "warn",
                ("GPS lost in flight: no 3D fix. " if airborne else "No 3D GPS fix. ") +
                ("The flight controller may not be able to return home; its failsafe may land or hold altitude, and the pilot may "
                 "have to fly manually." if airborne else "Routes, the geofence and return-to-home need one."),
                fix=telemetry.gps_fix, satellites=telemetry.satellites)
        elif telemetry.satellites < limits.min_satellites or (telemetry.hdop is not None and telemetry.hdop > limits.max_hdop):
            add("gps_degraded", "warn", f"GPS accuracy is poor: {telemetry.satellites} satellites"
                + (f", HDOP {telemetry.hdop:g}" if telemetry.hdop is not None else "") + ".",
                satellites=telemetry.satellites, hdop=telemetry.hdop)
        gps_age = _age_s(telemetry.gps_ms, now_ms)
        if telemetry.gps_ms and gps_age is not None and gps_age > limits.gps_stale_s:
            add("gps_stale", "critical" if airborne else "warn",
                f"GPS reports stopped arriving {gps_age:.0f} s ago: the fix and satellite count shown are not current.", age_s=gps_age)
        if telemetry.ekf_ok is False:
            add("ekf_unhealthy", "critical" if airborne else "warn",
                "The flight controller's position estimate (EKF) is not healthy" +
                (": return-to-home may not be possible. The EKF failsafe lands or holds altitude; the pilot may have to fly manually."
                 if airborne else "."), )
        ekf_age = _age_s(telemetry.ekf_ms, now_ms)
        if telemetry.ekf_ms and ekf_age is not None and ekf_age > limits.ekf_stale_s:
            add("ekf_stale", "warn", f"Navigation (EKF) reports stopped arriving {ekf_age:.0f} s ago.", age_s=ekf_age)
        if telemetry.last_jump_ms and now_ms - telemetry.last_jump_ms <= limits.jump_window_s * 1000:
            add("position_jump", "critical" if airborne else "warn",
                "The reported position jumped further than the aircraft can fly (GPS glitch or estimator reset): positions and "
                "distances may be wrong.", jumps=telemetry.position_jumps)
        home = (telemetry.home_lat, telemetry.home_lon)
        if valid_coordinates(*home):
            if self.configured_home and distance_m(home, self.configured_home) > limits.home_mismatch_m:
                gap = distance_m(home, self.configured_home)
                add("home_mismatch", "warn", f"The flight controller's home is {gap:.0f} m from the drone's configured home: "
                    "return-to-home goes to the flight controller's home, not the configured one.", distance_m=round(gap, 1))
        elif telemetry.gps_fix >= 3 and not airborne:
            add("home_unknown", "warn", "The flight controller has not reported a home position yet.")

    def return_home_possible(self, telemetry: DroneTelemetry, now_ms: int) -> tuple[bool, str]:
        """Can the flight controller navigate home? (False also when Sentinel cannot tell.)"""
        if not telemetry.link_ok:
            return False, "unknown: no telemetry"
        if telemetry.gps_fix < 3:
            return False, "no GPS fix"
        if telemetry.ekf_ok is False:
            return False, "position estimate unhealthy"
        age = _age_s(telemetry.telemetry_ms, now_ms)  # None: the adapter does not time-stamp positions (age unknown, not stale)
        if not valid_coordinates(telemetry.lat, telemetry.lon) or (age is not None and age > self.limits.position_stale_s):
            return False, "position not current"
        return True, "navigation healthy"

    # ---- battery ---------------------------------------------------------------

    def _battery_conditions(self, telemetry: DroneTelemetry, now_ms: int, airborne: bool, add: Any) -> None:
        limits = self.limits
        if telemetry.battery_pct is None:
            add("battery_unknown", "critical" if airborne else "warn",
                "The battery level is not reported" + (f" ({telemetry.battery_rejected} implausible readings ignored)" if telemetry.battery_rejected else "")
                + ": check the power module and BATT_MONITOR.", rejected=telemetry.battery_rejected)
            return
        if telemetry.battery_state in ("low", "critical", "emergency", "failed", "unhealthy"):
            # The flight controller's own judgement (voltage or capacity thresholds): it acts on it, whatever the % shows.
            add("battery_failsafe", "critical" if airborne or telemetry.battery_state != "low" else "warn",
                f"The flight controller reports the battery as {telemetry.battery_state}"
                + (f" at {telemetry.battery_voltage:.2f} V" if telemetry.battery_voltage else "") + (f" and {telemetry.battery_pct:.0f}%" if telemetry.battery_pct is not None else "")
                + (": its battery failsafe returns or lands the aircraft." if airborne else "."), state=telemetry.battery_state,
                voltage=telemetry.battery_voltage)
        age = _age_s(telemetry.battery_ms, now_ms)
        if telemetry.battery_ms and age is not None and age > limits.battery_stale_s:
            add("battery_stale", "critical" if airborne else "warn",
                f"Battery reports stopped arriving {age:.0f} s ago: the {telemetry.battery_pct:.0f}% shown is not current.", age_s=age)
        flying_s = (now_ms - self._flying_since_ms) / 1000 if self._flying_since_ms else 0.0
        unchanged_s = (now_ms - self._battery_changed_ms) / 1000 if self._battery_changed_ms else 0.0
        if airborne and flying_s > limits.battery_frozen_s and unchanged_s > limits.battery_frozen_s and (age or 0) <= limits.battery_stale_s:
            add("battery_frozen", "warn", f"The battery level has not changed for {unchanged_s:.0f} s of flight: the monitor may be stuck. "
                "Judge the remaining flight time by the clock and the voltage.", unchanged_s=round(unchanged_s))
        drain = self.drain()
        if airborne and drain["observed_pct_per_min"] is not None:
            if drain["expected_pct_per_min"] and drain["observed_pct_per_min"] > limits.drain_factor_warn * drain["expected_pct_per_min"]:
                add("battery_fast_drain", "warn", f"The battery is draining at {drain['observed_pct_per_min']:.1f}%/min, "
                    f"{drain['observed_pct_per_min'] / drain['expected_pct_per_min']:.1f}x the estimate for this aircraft.", **drain)
            minutes_left = drain["minutes_to_reserve"]
            minutes_home = self._minutes_home(telemetry)
            if minutes_left is not None and minutes_home is not None and minutes_left <= minutes_home * 1.5:
                add("battery_return_now", "critical", f"At the observed drain the battery reaches the {self.reserve_pct:g}% reserve in about "
                    f"{minutes_left:.1f} min; flying home takes about {minutes_home:.1f} min.", minutes_to_reserve=minutes_left,
                    minutes_home=minutes_home)

    def drain(self) -> dict[str, Any]:
        """Observed battery use in this flight against the model's estimate."""
        expected = round(100.0 / self.endurance_min_est, 2) if self.endurance_min_est else None
        samples = self._battery
        result: dict[str, Any] = {"observed_pct_per_min": None, "expected_pct_per_min": expected, "minutes_to_reserve": None, "samples": len(samples)}
        if len(samples) < 2 or (samples[-1].ms - samples[0].ms) < self.limits.drain_window_s * 1000:
            return result
        minutes = (samples[-1].ms - samples[0].ms) / 60000
        used = samples[0].pct - samples[-1].pct
        rate = max(0.0, used / minutes)
        result["observed_pct_per_min"] = round(rate, 2)
        if rate > 0:
            result["minutes_to_reserve"] = round(max(0.0, samples[-1].pct - self.reserve_pct) / rate, 1)
        return result

    def _minutes_home(self, telemetry: DroneTelemetry) -> float | None:
        home = (telemetry.home_lat, telemetry.home_lon) if valid_coordinates(telemetry.home_lat, telemetry.home_lon) else self.configured_home
        if home is None or not valid_coordinates(telemetry.lat, telemetry.lon):
            return None
        return round((distance_m((telemetry.lat, telemetry.lon), home) / max(self.cruise_mps, 0.5) + telemetry.alt_m / 1.0 + 20) / 60, 1)

    # ---- summaries -------------------------------------------------------------

    def worst(self) -> str:
        """'ok', or the highest severity present."""
        levels = [SEVERITIES.index(c["severity"]) for c in self.conditions]
        return SEVERITIES[max(levels)] if levels else "ok"

    def summary(self, telemetry: DroneTelemetry, now_ms: int) -> dict[str, Any]:
        possible, why = self.return_home_possible(telemetry, now_ms)
        return {"state": self.worst(), "conditions": list(self.conditions), "return_home_possible": possible, "return_home_detail": why,
                "drain": self.drain()}


def _age_s(stamp_ms: int, now_ms: int) -> float | None:
    if not stamp_ms:
        return None
    return round(max(0.0, (now_ms - stamp_ms) / 1000), 1)


__all__ = ["FlightHealthMonitor", "HealthLimits", "SEVERITIES"]
