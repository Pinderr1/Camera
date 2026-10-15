"""ArduPilot (ArduCopter) knowledge Sentinel needs to *read* a flight controller:
mode names, a flight state derived from telemetry, proof that a vehicle is a
simulator, and a read-only audit of the failsafe and fence parameters.

Sentinel never writes parameters. The audit tells the operator what the
commissioning engineer must change in their ground station; until the
blocking findings are fixed, Sentinel does not offer flights for approval.
Standard library only."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Callable

from .base import DroneTelemetry, geo_to_offset
from .catalog import rule_passes

COPTER_MODES = {
    0: "STABILIZE", 1: "ACRO", 2: "ALT_HOLD", 3: "AUTO", 4: "GUIDED", 5: "LOITER", 6: "RTL", 7: "CIRCLE", 9: "LAND",
    11: "DRIFT", 13: "SPORT", 14: "FLIP", 15: "AUTOTUNE", 16: "POSHOLD", 17: "BRAKE", 18: "THROW", 19: "AVOID_ADSB",
    20: "GUIDED_NOGPS", 21: "SMART_RTL", 22: "FLOWHOLD", 23: "FOLLOW", 24: "ZIGZAG", 25: "SYSTEMID", 26: "AUTOROTATE",
    27: "AUTO_RTL", 28: "TURTLE",
}
COPTER_MODE_IDS = {name: number for number, name in COPTER_MODES.items()}
RETURN_MODES = {"RTL", "SMART_RTL", "AUTO_RTL", "returning"}
LANDING_MODES = {"LAND", "landing"}
MAV_STATES = {0: "uninit", 1: "boot", 2: "calibrating", 3: "standby", 4: "active", 5: "critical", 6: "emergency", 7: "poweroff", 8: "termination"}
LANDED_STATES = {1: "on_ground", 2: "in_air", 3: "takeoff", 4: "landing"}

# EKF_STATUS_REPORT flags
EKF_ATTITUDE, EKF_VEL_HORIZ, EKF_POS_HORIZ_ABS, EKF_POS_VERT_ABS = 1, 2, 16, 32
EKF_CONST_POS_MODE, EKF_UNINITIALIZED = 128, 1024
MAV_SYS_STATUS_PREARM_CHECK = 0x10000000


def ekf_navigation_ok(flags: int) -> bool:
    needed = EKF_ATTITUDE | EKF_VEL_HORIZ | EKF_POS_HORIZ_ABS | EKF_POS_VERT_ABS
    return (flags & needed) == needed and not flags & (EKF_CONST_POS_MODE | EKF_UNINITIALIZED)


def version_text(flight_sw_version: int) -> str:
    major, minor, patch = (flight_sw_version >> 24) & 0xFF, (flight_sw_version >> 16) & 0xFF, (flight_sw_version >> 8) & 0xFF
    kind = flight_sw_version & 0xFF
    suffix = "" if kind == 255 else {0: "-dev", 64: "-alpha", 128: "-beta", 192: "-rc"}.get(kind & 0xC0, "")
    return f"{major}.{minor}.{patch}{suffix}"


def flight_state(telemetry: DroneTelemetry) -> str:
    """One word for the dashboard and the flight log, the same for every adapter."""
    if not telemetry.connected:
        return "offline"
    if not telemetry.link_ok:
        return "link_lost"
    mode = telemetry.flight_mode
    if telemetry.docked:
        return "docked"
    if not telemetry.in_air and telemetry.landed_state not in ("takeoff", "in_air"):
        return "armed" if telemetry.armed else "on_ground"
    if mode in RETURN_MODES:
        return "returning"
    if mode in LANDING_MODES or telemetry.landed_state == "landing":
        return "landing"
    if telemetry.landed_state == "takeoff" or mode == "takeoff":
        return "taking_off"
    return "airborne"


def is_sitl(parameters: dict[str, float]) -> bool:
    """SIM_* parameters exist only in ArduPilot's software-in-the-loop builds."""
    return any(name.startswith("SIM_") for name in parameters)


# Parameters that change by themselves (learned values, calibration, counters)
# are left out of the configuration fingerprint.
_VOLATILE_PREFIXES = ("STAT_", "SIM_", "STAT", "COMPASS_OFS", "COMPASS_DIA", "COMPASS_ODI", "COMPASS_MOT", "COMPASS_DEC",
                      "INS_ACC", "INS_GYR", "INS_TCAL", "BARO", "MOT_THST_HOVER", "AHRS_TRIM", "RC", "FORMAT_VERSION", "SYSID_SW")


def configuration_fingerprint(parameters: dict[str, float]) -> str:
    stable = {name: round(float(value), 4) for name, value in parameters.items() if not name.startswith(_VOLATILE_PREFIXES)}
    text = "\n".join(f"{name}={value:g}" for name, value in sorted(stable.items()))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def changed_parameters(baseline: dict[str, float], current: dict[str, float]) -> list[str]:
    names = {n for n in set(baseline) | set(current) if not n.startswith(_VOLATILE_PREFIXES)}
    return sorted(n for n in names if baseline.get(n) is None or current.get(n) is None or abs(float(baseline[n]) - float(current[n])) > 1e-4)


@dataclass(frozen=True)
class ParameterRule:
    name: str
    expected: str
    why: str
    check: Callable[[float, dict[str, Any]], bool]
    blocking: bool = True


def _fence_alt_ok(value: float, ctx: dict[str, Any]) -> bool:
    return value <= ctx["max_altitude_m"] + 0.5


def _rtl_alt_ok(value: float, ctx: dict[str, Any]) -> bool:
    return value >= ctx["safe_transit_altitude_m"] * 100 - 50  # RTL_ALT is in centimetres


def _rtl_below_fence(value: float, ctx: dict[str, Any]) -> bool:
    return value <= ctx["max_altitude_m"] * 100


RULES: tuple[ParameterRule, ...] = (
    ParameterRule("FS_GCS_ENABLE", "1, 3, 4 or 5 (return or land)", "Losing the ground-station link must make the aircraft return or land on its own.",
                  lambda v, c: int(v) in (1, 3, 4, 5)),
    ParameterRule("FS_THR_ENABLE", "not 0", "Losing the pilot's radio must trigger a failsafe.", lambda v, c: int(v) != 0),
    ParameterRule("BATT_FS_LOW_ACT", "not 0 (land or return)", "A low battery must make the aircraft return or land.", lambda v, c: int(v) != 0),
    ParameterRule("BATT_FS_CRT_ACT", "not 0 (land or return)", "A critical battery must make the aircraft land.", lambda v, c: int(v) != 0),
    ParameterRule("FS_EKF_ACTION", "not 0", "A navigation (EKF) failure must trigger a failsafe.", lambda v, c: int(v) != 0),
    ParameterRule("FENCE_ENABLE", "1", "The flight controller's fence is the authoritative geofence.", lambda v, c: int(v) == 1),
    ParameterRule("FENCE_TYPE", "altitude and polygon bits set (5 or 7)", "Both the height limit and the property polygon must be enforced.",
                  lambda v, c: (int(v) & 5) == 5),
    ParameterRule("FENCE_ACTION", "not 0 (0 only reports)", "A fence breach must make the aircraft return or land.", lambda v, c: int(v) != 0),
    ParameterRule("FENCE_ALT_MAX", "at or below Sentinel's geofence ceiling", "The FC height limit must not be looser than the approved one.", _fence_alt_ok),
    ParameterRule("RTL_ALT", "at or above the site's safe transit altitude", "Returning home must happen above the surveyed obstacles.", _rtl_alt_ok),
    ParameterRule("RTL_ALT", "below the geofence ceiling", "Returning home must not breach the altitude fence.", _rtl_below_fence),
    ParameterRule("ARMING_CHECK", "1 (all pre-arm checks)", "Pre-arm checks should not be disabled.", lambda v, c: int(v) == 1, blocking=False),
)
THRESHOLD_ONE_OF = (("BATT_LOW_VOLT", "BATT_LOW_MAH"), ("BATT_CRT_VOLT", "BATT_CRT_MAH"))
# ArduCopter 4.7 renamed some parameters. Rules use the older names and units;
# the newer names are converted: name -> (new name, conversion).
ALIASES: dict[str, tuple[str, Callable[[float], float]]] = {
    "RTL_ALT": ("RTL_ALT_M", lambda metres: metres * 100),
    "ARMING_CHECK": ("ARMING_SKIPCHK", lambda skipped: 1.0 if int(skipped) == 0 else 0.0),
    "SYSID_MYGCS": ("MAV_GCS_SYSID", lambda value: value),
}


def parameter_value(parameters: dict[str, float], name: str) -> tuple[float | None, str]:
    """(value in the rule's units, the name it was read from)."""
    if name in parameters:
        return float(parameters[name]), name
    alias = ALIASES.get(name)
    if alias and alias[0] in parameters:
        return alias[1](float(parameters[alias[0]])), alias[0]
    return None, name


def audit_parameters(parameters: dict[str, float], *, max_altitude_m: float, safe_transit_altitude_m: float,
                     fence_polygon: list[list[float]] | None = None, geofence_polygon: list[list[float]] | None = None,
                     baseline: dict[str, float] | None = None, extra_rules: list[dict[str, Any]] | None = None,
                     extra_label: str = "") -> dict[str, Any]:
    """Compare the vehicle's parameters with what a Sentinel inspection needs.
    Returns {"ok", "findings": [...], "blocking": [...], "fingerprint", "changed_since_commissioning"}."""
    if not parameters:
        finding = {"param": "*", "expected": "parameters read from the flight controller", "actual": "not received yet",
                   "ok": False, "blocking": True, "why": "Sentinel cannot check the failsafes before it has read the parameters."}
        return {"ok": False, "findings": [finding], "blocking": [finding], "fingerprint": "", "changed_since_commissioning": []}
    ctx = {"max_altitude_m": max_altitude_m, "safe_transit_altitude_m": safe_transit_altitude_m}
    findings = []
    for rule in RULES:
        value, source = parameter_value(parameters, rule.name)
        ok = value is not None and rule.check(value, ctx)
        actual = "not reported" if value is None else _fmt(rule.name, value)
        if value is not None and source != rule.name:
            actual += f" (from {source}={parameters[source]:g})"
        findings.append({"param": rule.name, "expected": rule.expected, "actual": actual, "ok": ok, "blocking": rule.blocking, "why": rule.why})
    for names in THRESHOLD_ONE_OF:
        ok = any(float(parameters.get(name, 0) or 0) > 0 for name in names)
        findings.append({"param": " or ".join(names), "expected": "a threshold above 0", "actual": ", ".join(f"{n}={_fmt(n, parameters.get(n, 0))}" for n in names),
                         "ok": ok, "blocking": True, "why": "The battery failsafe needs a threshold to act on."})
    for rule in extra_rules or ():
        # Hardware-specific settings of the drone model (catalog.py), e.g. the gimbal driver or the lidar.
        value, source = parameter_value(parameters, rule["param"])
        actual = "not reported" if value is None else _fmt(rule["param"], value)
        findings.append({"param": rule["param"], "expected": rule["expected"], "actual": actual, "ok": rule_passes(rule, value),
                         "blocking": bool(rule.get("blocking", True)), "why": rule["why"], "model": extra_label})
    if geofence_polygon:
        findings.append(_fence_finding(fence_polygon, geofence_polygon))
    changed: list[str] = []
    if baseline:
        changed = changed_parameters(baseline, parameters)
        findings.append({"param": "configuration", "expected": "unchanged since commissioning",
                         "actual": "unchanged" if not changed else f"{len(changed)} changed: {', '.join(changed[:8])}" + ("…" if len(changed) > 8 else ""),
                         "ok": not changed, "blocking": False, "why": "Changes after commissioning should be reviewed by the person responsible for the aircraft."})
    blocking = [f for f in findings if not f["ok"] and f["blocking"]]
    return {"ok": not blocking, "findings": findings, "blocking": blocking, "fingerprint": configuration_fingerprint(parameters),
            "changed_since_commissioning": changed}


def _fence_finding(fence_polygon: list[list[float]] | None, geofence_polygon: list[list[float]]) -> dict[str, Any]:
    why = "The flight controller's polygon must not allow flight outside the area Sentinel approves."
    if not fence_polygon:
        return {"param": "fence polygon", "expected": "an inclusion polygon loaded on the flight controller", "actual": "none loaded",
                "ok": False, "blocking": True, "why": why}
    from senior_safety.pose_events import point_in_polygon

    origin = (geofence_polygon[0][0], geofence_polygon[0][1])
    allowed = [list(geo_to_offset(origin, lat, lon)) for lat, lon in geofence_polygon]
    outside = []
    for lat, lon in fence_polygon:
        point = geo_to_offset(origin, lat, lon)
        if not point_in_polygon(point[0], point[1], allowed) and _distance_to_polygon(point, allowed) > 1.0:
            outside.append(f"{lat:.6f},{lon:.6f}")
    return {"param": "fence polygon", "expected": "inside Sentinel's geofence (1 m tolerance)",
            "actual": "inside" if not outside else f"{len(outside)} vertex(es) outside", "ok": not outside, "blocking": True, "why": why}


def _distance_to_polygon(point: tuple[float, float], polygon: list[list[float]]) -> float:
    best = math.inf
    for (ax, ay), (bx, by) in zip(polygon, polygon[1:] + polygon[:1]):
        dx, dy = bx - ax, by - ay
        length = dx * dx + dy * dy
        t = 0.0 if length == 0 else max(0.0, min(1.0, ((point[0] - ax) * dx + (point[1] - ay) * dy) / length))
        best = min(best, math.hypot(point[0] - (ax + t * dx), point[1] - (ay + t * dy)))
    return best


def _fmt(name: str, value: Any) -> str:
    if value is None:
        return "not reported"
    number = float(value)
    text = f"{number:g}"
    if name == "RTL_ALT":
        text += f" cm ({number / 100:g} m)"
    return text
