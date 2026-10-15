"""Sentinel drone models: which aircraft supports which features.

One customer, one drone. Three reference designs at different price points
share as much as possible:
- one flight stack: ArduCopter 4.6/4.7 on a Pixhawk-class flight controller;
- one radio architecture: a SIYI HM30 video and telemetry link that lands on
  the Hub's local network, plus an independent ExpressLRS pilot radio;
- one camera protocol: SIYI gimbal cameras (RTSP over Ethernet, gimbal angle
  reported through ArduPilot's SIYI driver);
- the same Sentinel software: readiness, parameter audit, approvals, route
  export, drone video, observations and evidence.

They differ in airframe, camera, endurance, redundancy and optional sensors.

Every model here is a **reference design**: no hardware has been bought and
nothing has flown. Figures marked as estimates come from manufacturer listings
and simple scaling, not measurement. Physical flight commands stay disabled
for every model; this catalog never enables them.

The catalog drives:
- model-specific readiness checks (command lockout, companion, video, night,
  wind and temperature limits, flight-controller settings);
- the camera geometry and video processing settings for drone video;
- the built-in simulator's performance (speed, climb, battery drain);
- an endurance check for every approved route;
- the comparison screen and the AI Guide's recommendations.

Prices are deliberately absent: parts estimates live in docs/drone-models.md.
Standard library only."""

from __future__ import annotations

import copy
import math
from typing import Any

# Values for a capability: what the model offers.
CAPABILITY_VALUES = {"yes": "Included", "optional": "Optional add-on", "limited": "Limited (read the notes)", "no": "Not available"}

CAPABILITIES: dict[str, dict[str, str]] = {
    "telemetry": {"label": "Telemetry to the Hub", "detail": "Position, height, attitude, battery and flight state reach Sentinel."},
    "gps": {"label": "GPS positioning", "detail": "Needed for routes, the geofence and return-to-home."},
    "dual_gps": {"label": "Redundant GPS", "detail": "A second receiver takes over after a GPS glitch near buildings."},
    "battery_monitoring": {"label": "Battery monitoring", "detail": "Voltage and current sensing for battery failsafes and readiness."},
    "manual_control": {"label": "Independent pilot radio", "detail": "The pilot can take over on a separate radio even if the Hub or video link fails."},
    "fc_failsafes": {"label": "Flight-controller failsafes", "detail": "Return home or land on link loss, low battery, navigation failure or fence breach."},
    "parameter_audit": {"label": "Failsafe and fence audit", "detail": "Sentinel reads the flight controller's settings and blocks approval when they are wrong."},
    "command_lockout": {"label": "Hub command lockout", "detail": "A second layer, independent of the Hub software, stops the Hub from flying a real aircraft."},
    "video_stream": {"label": "Live video to the Hub", "detail": "Inspection video reaches Sentinel for detection and evidence."},
    "stabilized_video": {"label": "Stabilised camera", "detail": "A 3-axis gimbal keeps the picture steady in wind."},
    "gimbal_attitude": {"label": "Gimbal angle reported", "detail": "Makes ground positions of what the camera sees more precise."},
    "optical_zoom": {"label": "Optical zoom", "detail": "Inspect from farther away or higher up."},
    "thermal": {"label": "Thermal imaging", "detail": "Shows people and vehicles by heat, also in the dark."},
    "night_inspection": {"label": "Useful video at night", "detail": "Whether an inspection after dark shows anything useful."},
    "obstacle_sensing": {"label": "Obstacle sensing", "detail": "A scanning lidar for ArduPilot's own avoidance. Routes avoid obstacles by design first."},
    "companion_computer": {"label": "Companion computer", "detail": "On-aircraft relay with a command filter, paired identity and health reports."},
}

SUPPORT_NOTE = ("Reference design: no hardware has been bought and nothing has flown. Sentinel's software for it is tested with "
                "its built-in simulator and ArduPilot SITL. Physical aircraft are telemetry-only in this build.")

_SHARED_HARDWARE = {
    "link": "SIYI HM30: video and telemetry radio whose ground unit plugs into the Hub's network",
    "pilot_radio": "ExpressLRS transmitter and receiver, independent of the video link",
    "firmware": "ArduCopter 4.6 or 4.7 (pinned)",
}

MODELS: list[dict[str, Any]] = [
    {
        "id": "sentinel_lite", "tier": "lite", "name": "Sentinel Lite", "status": "reference_design", "cost_rank": 1,
        "cost_label": "Lowest parts cost",
        "summary": "The cheapest drone that does the job: a stabilised daytime camera, GPS return-to-home and audited failsafes, "
                   "with nothing on board that the job does not need.",
        "best_for": "Daytime and dusk look-arounds of a home or small business, flown by the customer's pilot.",
        "hardware": _SHARED_HARDWARE | {
            "airframe": "Holybro X500 V2 (500 mm) ARF kit: frame, motors, ESCs, propellers",
            "flight_controller": "Pixhawk 6C or 6C Mini (cost-down candidate: Matek H743 class, after bench tests)",
            "companion": "None: the flight controller locks out Hub commands itself (MAV_OPTIONS)",
            "camera": "SIYI A8 mini: 3-axis gimbal, 1/1.7-inch 8 MP, 6x digital zoom, Ethernet RTSP",
            "gps": "One Holybro M10 GPS with compass",
            "battery": "4S 5000 mAh LiPo, two packs",
            "sensors": "None",
            "lights": "Anti-collision strobe (needed at dusk)",
        },
        "capabilities": {
            "telemetry": "yes", "gps": "yes", "dual_gps": "no", "battery_monitoring": "yes", "manual_control": "yes", "fc_failsafes": "yes",
            "parameter_audit": "yes", "command_lockout": "yes", "video_stream": "yes", "stabilized_video": "yes", "gimbal_attitude": "yes",
            "optical_zoom": "no", "thermal": "no", "night_inspection": "no", "obstacle_sensing": "no", "companion_computer": "optional",
        },
        "camera": {"name": "SIYI A8 mini", "hfov_deg": 81.0, "vfov_deg": 51.0, "default_pitch_deg": -90.0, "gimbal_axes": 3, "reports_attitude": True,
                   "zoom": "6x digital", "low_light": False, "thermal": None, "default_stream": "rtsp://192.168.144.25:8554/main.264",
                   "source": "HFOV from the manufacturer listing; VFOV derived for 16:9; stream address from the camera manual (confirm on the bench)"},
        "flight": {"endurance_min_est": 15.0, "cruise_mps": 5.0, "climb_mps": 2.0, "max_wind_mps": 6.0, "min_temperature_c": 0.0,
                   "max_temperature_c": 40.0, "precipitation": False, "night_operations": False, "takeoff_mass_kg_est": 1.8,
                   "source": "Estimates: X500 V2 hover time with about 250 g of payload, scaled from the kit figure; no flight measured"},
        "requires": {"companion": "none", "video": True, "lockout": True, "lights_at_night": True},
        "options": {"companion": "Raspberry Pi companion running Sentinel's relay (adds pairing, health and a second command filter)",
                    "strobe": "Anti-collision strobe"},
        "parameters": [
            {"param": "BATT_MONITOR", "test": "not", "value": 0, "expected": "a battery monitor (not 0)", "blocking": True,
             "why": "Battery failsafes and readiness need voltage and current from the power module."},
            {"param": "MNT1_TYPE", "test": "eq", "value": 8, "expected": "8 (SIYI gimbal)", "blocking": False,
             "why": "ArduPilot's SIYI driver reports the gimbal angle, which makes ground positions more precise."},
        ],
        "video": {"fps": 5.0, "analyse": "visible", "notes": []},
        "firmware_tested": ["4.6", "4.7"],
        "limitations": [
            "Daylight and dusk only: the camera shows little of a dark yard, and Sentinel will not approve a night inspection with it.",
            "Wind limit 6 m/s (estimate) until flight tests establish one; a 500 mm aircraft is pushed around more than the larger models.",
            "About 15 minutes of hover (estimate), so a 30% reserve leaves roughly 10 minutes of flying.",
            "One GPS: a GPS glitch near buildings means a failsafe (land or return) rather than a switch-over.",
            "No obstacle sensing: safety comes from the surveyed transit altitude above roofs, trees and wires.",
            "No companion computer: the flight controller itself refuses the Hub's commands; Sentinel reads its settings when the pilot's "
            "ground station downloads them over the shared link.",
            "Not rain-proof; LiPo packs lose capacity below 0 °C.",
        ],
    },
    {
        "id": "sentinel_pro", "tier": "pro", "name": "Sentinel Pro", "status": "reference_design", "cost_rank": 2,
        "cost_label": "Medium parts cost",
        "summary": "A larger, steadier aircraft with optical zoom, longer flights, redundant GPS and an on-board companion computer.",
        "best_for": "Larger or windier properties, inspecting from a higher or farther position, small businesses with frequent inspections.",
        "hardware": _SHARED_HARDWARE | {
            "airframe": "Holybro X650 (650 mm, carbon)",
            "flight_controller": "Pixhawk 6X on the Holybro RPi CM4 baseboard",
            "companion": "Raspberry Pi CM4 on the baseboard: Sentinel relay, pairing, health",
            "camera": "SIYI ZR10: 3-axis gimbal, 10x optical / 30x hybrid zoom, 2K",
            "gps": "Two Holybro M10 GPS receivers with automatic switch-over",
            "battery": "6S 10 Ah, two or three packs",
            "sensors": "Optional LightWare SF45/B scanning lidar",
            "lights": "Anti-collision strobe and position lights",
        },
        "capabilities": {
            "telemetry": "yes", "gps": "yes", "dual_gps": "yes", "battery_monitoring": "yes", "manual_control": "yes", "fc_failsafes": "yes",
            "parameter_audit": "yes", "command_lockout": "yes", "video_stream": "yes", "stabilized_video": "yes", "gimbal_attitude": "yes",
            "optical_zoom": "yes", "thermal": "no", "night_inspection": "limited", "obstacle_sensing": "optional", "companion_computer": "yes",
        },
        "camera": {"name": "SIYI ZR10", "hfov_deg": 71.5, "vfov_deg": 44.0, "default_pitch_deg": -90.0, "gimbal_axes": 3, "reports_attitude": True,
                   "zoom": "10x optical, 30x hybrid", "low_light": False, "thermal": None, "default_stream": "rtsp://192.168.144.25:8554/main.264",
                   "source": "HFOV at 1x from retailer listings (not yet checked against the manual); VFOV derived for 16:9"},
        "flight": {"endurance_min_est": 24.0, "cruise_mps": 6.0, "climb_mps": 2.5, "max_wind_mps": 8.0, "min_temperature_c": -5.0,
                   "max_temperature_c": 40.0, "precipitation": False, "night_operations": False, "takeoff_mass_kg_est": 3.9,
                   "source": "Estimates: X650 30 min no-payload figure scaled for about 0.7 kg of payload; no flight measured"},
        "requires": {"companion": "required", "video": True, "lockout": True, "lights_at_night": True},
        "options": {"obstacle_sensor": "LightWare SF45/B lidar for ArduPilot proximity avoidance"},
        "parameters": [
            {"param": "BATT_MONITOR", "test": "not", "value": 0, "expected": "a battery monitor (not 0)", "blocking": True,
             "why": "Battery failsafes and readiness need voltage and current from the power module."},
            {"param": "MNT1_TYPE", "test": "eq", "value": 8, "expected": "8 (SIYI gimbal)", "blocking": False,
             "why": "ArduPilot's SIYI driver reports the gimbal angle, which makes ground positions more precise."},
            {"param": "GPS_AUTO_SWITCH", "test": "not", "value": 0, "expected": "not 0 (use the better GPS)", "blocking": False,
             "why": "The second GPS only helps when the flight controller may switch to it."},
            {"param": "PRX1_TYPE", "test": "eq", "value": 8, "expected": "8 (LightWare SF45/B)", "blocking": True, "option": "obstacle_sensor",
             "why": "The lidar is installed, so ArduPilot must be configured to use it."},
        ],
        "video": {"fps": 5.0, "analyse": "visible", "notes": []},
        "firmware_tested": ["4.6", "4.7"],
        "limitations": [
            "No thermal camera: at night it shows only what lights illuminate, so night inspections are not approved without Thermal.",
            "Wind limit 8 m/s (estimate) until flight tests establish one.",
            "About 24 minutes of hover (estimate).",
            "Heavier aircraft (about 3.9 kg): more energy near people, which matters for Transport Canada operations near bystanders.",
            "Not rain-proof.",
        ],
    },
    {
        "id": "sentinel_thermal", "tier": "thermal", "name": "Sentinel Thermal", "status": "reference_design", "cost_rank": 3,
        "cost_label": "Highest parts cost",
        "summary": "The Pro platform with a visible and thermal camera, lidar and lights, for inspections that must work after dark.",
        "best_for": "Night-time inspections where a person or vehicle has to be seen without lighting.",
        "hardware": _SHARED_HARDWARE | {
            "airframe": "Holybro X650 (650 mm, carbon)",
            "flight_controller": "Pixhawk 6X on the Holybro RPi CM4 baseboard",
            "companion": "Raspberry Pi CM4 on the baseboard: Sentinel relay, pairing, health",
            "camera": "SIYI ZT6: 4K visible plus 640x512 thermal, 3-axis gimbal",
            "gps": "Two Holybro M10 GPS receivers with automatic switch-over",
            "battery": "6S 10 Ah, three packs",
            "sensors": "LightWare SF45/B scanning lidar and a downward rangefinder",
            "lights": "Anti-collision strobe and position lights (required at night)",
        },
        "capabilities": {
            "telemetry": "yes", "gps": "yes", "dual_gps": "yes", "battery_monitoring": "yes", "manual_control": "yes", "fc_failsafes": "yes",
            "parameter_audit": "yes", "command_lockout": "yes", "video_stream": "yes", "stabilized_video": "yes", "gimbal_attitude": "yes",
            "optical_zoom": "no", "thermal": "yes", "night_inspection": "yes", "obstacle_sensing": "yes", "companion_computer": "yes",
        },
        "camera": {"name": "SIYI ZT6", "hfov_deg": 81.0, "vfov_deg": 51.0, "default_pitch_deg": -90.0, "gimbal_axes": 3, "reports_attitude": True,
                   "zoom": "6x digital (visible)", "low_light": True,
                   "thermal": {"resolution": "640x512", "analysed": False,
                               "why": "Sentinel's detector is trained on visible-light images and has not been validated on thermal or aerial video."},
                   "default_stream": "rtsp://192.168.144.25:8554/main.264",
                   "source": "Visible-channel field of view assumed equal to the A8 mini (estimate; measure on the bench)"},
        "flight": {"endurance_min_est": 22.0, "cruise_mps": 6.0, "climb_mps": 2.5, "max_wind_mps": 8.0, "min_temperature_c": -5.0,
                   "max_temperature_c": 40.0, "precipitation": False, "night_operations": True, "takeoff_mass_kg_est": 3.9,
                   "source": "Estimates from docs/drone-hardware-v1.md section 4 (22-25 min with payload); no flight measured"},
        "requires": {"companion": "required", "video": True, "lockout": True, "lights_at_night": True},
        "options": {},
        "parameters": [
            {"param": "BATT_MONITOR", "test": "not", "value": 0, "expected": "a battery monitor (not 0)", "blocking": True,
             "why": "Battery failsafes and readiness need voltage and current from the power module."},
            {"param": "MNT1_TYPE", "test": "eq", "value": 8, "expected": "8 (SIYI gimbal)", "blocking": False,
             "why": "ArduPilot's SIYI driver reports the gimbal angle, which makes ground positions more precise."},
            {"param": "GPS_AUTO_SWITCH", "test": "not", "value": 0, "expected": "not 0 (use the better GPS)", "blocking": False,
             "why": "The second GPS only helps when the flight controller may switch to it."},
            {"param": "PRX1_TYPE", "test": "eq", "value": 8, "expected": "8 (LightWare SF45/B)", "blocking": True,
             "why": "The Thermal model carries the lidar, so ArduPilot must be configured to use it."},
        ],
        "video": {"fps": 5.0, "analyse": "visible",
                  "notes": ["Thermal channel not analysed: Sentinel's detector has not been validated on thermal imagery. "
                            "The pilot sees it in the ground station."]},
        "firmware_tested": ["4.6", "4.7"],
        "limitations": [
            "Thermal video is shown to the pilot, but Sentinel does not run detection on it yet (no validated thermal model).",
            "Thermal cores above 9 Hz are export-controlled; check before shipping outside Canada.",
            "Wind limit 8 m/s (estimate) until flight tests establish one.",
            "About 22 minutes of hover (estimate).",
            "Night flights need visible anti-collision lights and a pilot qualified for the operation.",
            "Not rain-proof.",
        ],
    },
    {
        "id": "generic_ardupilot", "tier": "other", "name": "Other ArduPilot multicopter", "status": "customer_hardware", "cost_rank": 0,
        "cost_label": "Customer's own hardware",
        "summary": "Any ArduCopter 4.6/4.7 aircraft the customer already owns. Sentinel audits its failsafes and follows it from telemetry.",
        "best_for": "Integrators and customers with their own ArduPilot aircraft.",
        "hardware": {"firmware": "ArduCopter 4.6 or 4.7", "link": "Any MAVLink telemetry link that reaches the Hub",
                     "camera": "Any camera with an RTSP stream (optional)"},
        "capabilities": {
            "telemetry": "yes", "gps": "yes", "dual_gps": "limited", "battery_monitoring": "yes", "manual_control": "limited", "fc_failsafes": "yes",
            "parameter_audit": "yes", "command_lockout": "limited", "video_stream": "limited", "stabilized_video": "limited",
            "gimbal_attitude": "limited", "optical_zoom": "limited", "thermal": "limited", "night_inspection": "limited",
            "obstacle_sensing": "limited", "companion_computer": "optional",
        },
        "camera": {"name": "Configured camera", "hfov_deg": 80.0, "vfov_deg": 50.0, "default_pitch_deg": -90.0, "gimbal_axes": 0,
                   "reports_attitude": False, "zoom": "", "low_light": False, "thermal": None, "default_stream": "",
                   "source": "Defaults; set the real field of view in the drone's video settings"},
        "flight": {"endurance_min_est": 0.0, "cruise_mps": 6.0, "climb_mps": 2.5, "max_wind_mps": 8.0, "min_temperature_c": -5.0,
                   "max_temperature_c": 40.0, "precipitation": False, "night_operations": False, "takeoff_mass_kg_est": 0.0,
                   "source": "Sentinel defaults; endurance unknown, so no endurance check is made"},
        "requires": {"companion": "optional", "video": False, "lockout": False, "lights_at_night": True},
        "options": {},
        "parameters": [],
        "video": {"fps": 5.0, "analyse": "visible", "notes": []},
        "firmware_tested": ["4.6", "4.7"],
        "limitations": ["Sentinel knows nothing about this aircraft beyond what its flight controller reports.",
                        "Without a companion relay or the flight controller's command lockout, only the Hub's own refusal stops it from commanding the aircraft."],
    },
]
MODEL_INDEX = {model["id"]: model for model in MODELS}
TIERS = ("sentinel_lite", "sentinel_pro", "sentinel_thermal")
DEFAULT_MODEL = "generic_ardupilot"
OPTION_KEYS = {"companion", "strobe", "obstacle_sensor"}
ROUTE_OVERHEAD_S = 30.0  # arming, take-off settle, landing


def get_model(model_id: str | None) -> dict[str, Any]:
    return MODEL_INDEX.get(model_id or "", MODEL_INDEX[DEFAULT_MODEL])


def model_for(device: dict[str, Any] | None) -> dict[str, Any]:
    return get_model((device or {}).get("drone_model"))


def options_for(model: dict[str, Any], device: dict[str, Any] | None) -> dict[str, bool]:
    """Installed options: what the device says, limited to what the model offers."""
    chosen = (device or {}).get("drone_options") or {}
    return {key: bool(chosen.get(key)) for key in model.get("options", {})}


def validate_drone_model(device: dict[str, Any], path: str, errors: list[str]) -> None:
    model_id = device.get("drone_model")
    if model_id is None:
        return
    model = MODEL_INDEX.get(model_id)
    if model is None:
        errors.append(f"{path}.drone_model must be one of {list(MODEL_INDEX)}")
        return
    options = device.get("drone_options", {})
    if not isinstance(options, dict):
        errors.append(f"{path}.drone_options must be an object")
        return
    for key, value in options.items():
        if key not in model.get("options", {}):
            errors.append(f"{path}.drone_options.{key} is not an option of {model['name']}")
        elif not isinstance(value, bool):
            errors.append(f"{path}.drone_options.{key} must be true or false")


# ---- what the model means for the software --------------------------------------------


def camera_settings(model: dict[str, Any]) -> dict[str, float]:
    camera = model["camera"]
    return {key: camera[key] for key in ("hfov_deg", "vfov_deg", "default_pitch_deg")}


def video_settings(model: dict[str, Any], device: dict[str, Any] | None, *, simulated: bool,
                   site_video: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Drone video settings for this aircraft. Explicit `device.video` wins
    (its camera geometry is completed from the model). Otherwise a simulated
    connection gets the simulated aerial camera (with the site's scripted
    objects, if any) and a physical one gets nothing until a stream address is
    set: Sentinel never guesses a network address for a real camera."""
    explicit = (device or {}).get("video")
    if explicit:
        settings = copy.deepcopy(explicit)
    elif simulated:
        objects = ((site_video or {}).get("source") or {}).get("objects", []) if (site_video or {}).get("source", {}).get("type") == "simulated_aerial" else []
        settings = {"source": {"type": "simulated_aerial", "objects": copy.deepcopy(objects)}}
    else:
        return None
    settings["camera"] = camera_settings(model) | (settings.get("camera") or {})
    settings.setdefault("fps", model["video"]["fps"])
    settings["model"] = model["id"]
    notes = list(model["video"]["notes"])
    if simulated:
        notes.append(f"Simulated aerial camera with the {model['camera']['name']}'s field of view.")
    settings["notes"] = notes
    return settings


def simulator_settings(model: dict[str, Any]) -> dict[str, Any]:
    """Built-in simulator performance for this model (kinematic, not physics)."""
    flight = model["flight"]
    settings: dict[str, Any] = {"cruise_speed_mps": flight["cruise_mps"], "climb_rate_mps": flight["climb_mps"]}
    if flight["endurance_min_est"]:
        settings["drain_pct_per_s"] = round(100.0 / (flight["endurance_min_est"] * 60.0), 4)
    if model["camera"]["reports_attitude"]:
        settings["gimbal_pitch_deg"] = model["camera"]["default_pitch_deg"]
    return settings


def weather_defaults(model: dict[str, Any]) -> dict[str, float]:
    flight = model["flight"]
    return {"max_wind_mps": flight["max_wind_mps"], "min_temperature_c": flight["min_temperature_c"], "max_temperature_c": flight["max_temperature_c"]}


def parameter_rules(model: dict[str, Any], options: dict[str, bool]) -> list[dict[str, Any]]:
    return [rule for rule in model.get("parameters", []) if not rule.get("option") or options.get(rule["option"])]


def rule_passes(rule: dict[str, Any], value: float | None) -> bool:
    if value is None:
        return False
    test, expected = rule["test"], rule["value"]
    if test == "eq":
        return abs(float(value) - float(expected)) < 1e-6
    if test == "not":
        return abs(float(value) - float(expected)) > 1e-6
    if test == "min":
        return float(value) >= float(expected)
    if test == "bit":
        return bool(int(value) & (1 << int(expected)))
    raise ValueError(f"unknown parameter test {test!r}")


def command_lockout(model: dict[str, Any], parameters: dict[str, float], hub_system_id: int,
                    companion: dict[str, Any] | None) -> tuple[str, str]:
    """(state, detail) for the independent layer that stops the Hub from
    commanding a real aircraft. Two ways to have one:
    - a paired companion reporting with the relay's `telemetry` policy, or
    - the flight controller itself accepting commands only from the pilot's
      ground station (ArduCopter 4.7 MAV_OPTIONS bit 0, older SYSID_ENFORCE),
      with that ground station's id different from the Hub's.
    Verified in ArduPilot SITL: with the lockout on, the Hub's arm, mode and
    take-off commands are ignored while configured telemetry still flows."""
    relay = ((companion or {}).get("report") or {}).get("relay") or {}
    if companion and companion.get("state") == "reporting" and relay.get("policy") == "telemetry":
        return "pass", f"companion relay {companion.get('name', '')} drops Hub flight commands".replace("  ", " ")
    if not parameters:
        state = "fail" if model["requires"]["lockout"] else "warn"
        return state, "cannot be checked until the flight controller's parameters have been read"
    enforced = rule_passes({"test": "bit", "value": 0}, parameters.get("MAV_OPTIONS")) or int(parameters.get("SYSID_ENFORCE", 0) or 0) == 1
    gcs = parameters.get("MAV_GCS_SYSID", parameters.get("SYSID_MYGCS"))
    if enforced and gcs is not None and int(gcs) != int(hub_system_id):
        return "pass", f"the flight controller accepts commands only from the pilot's ground station (system id {int(gcs)})"
    if enforced:
        return ("fail" if model["requires"]["lockout"] else "warn",
                f"the flight controller's command lockout trusts system id {gcs:g}, which is the Hub: set MAV_GCS_SYSID to the pilot's ground station")
    detail = ("no companion relay and MAV_OPTIONS bit 0 is off: only the Hub's own refusal stops it from commanding this aircraft. "
              "Set MAV_OPTIONS=1 and MAV_GCS_SYSID to the pilot's ground station, or pair a companion")
    return ("fail" if model["requires"]["lockout"] else "warn"), detail


def estimate_flight_s(model: dict[str, Any], route: dict[str, Any]) -> float:
    """Planned flight time: vertical climb and descent at home, straight transit
    there and back at cruise speed, the hover, and a fixed overhead."""
    flight = model["flight"]
    transit = float(route.get("transit_alt_m", 0))
    horizontal = max(0.0, float(route.get("distance_m", 0)) - 2 * transit)
    return round(2 * transit / flight["climb_mps"] + horizontal / flight["cruise_mps"] + float(route.get("hover_s", 0)) + ROUTE_OVERHEAD_S, 1)


def endurance_check(model: dict[str, Any], route: dict[str, Any], battery_pct: float | None, reserve_pct: float) -> dict[str, Any]:
    """Does the route fit in the battery above the reserve? Uses the model's
    estimated endurance, so it is a planning check, never a guarantee."""
    needed_s = estimate_flight_s(model, route)
    endurance_min = model["flight"]["endurance_min_est"]
    if not endurance_min:
        return {"ok": True, "checked": False, "needed_s": needed_s, "detail": "endurance unknown for this aircraft: no endurance check"}
    if battery_pct is None:
        return {"ok": False, "checked": True, "needed_s": needed_s, "detail": "battery level not reported"}
    usable_s = max(0.0, (float(battery_pct) - reserve_pct) / 100.0 * endurance_min * 60.0)
    ok = needed_s <= usable_s
    detail = (f"route needs about {math.ceil(needed_s / 60)} min; at {battery_pct:.0f}% the {model['name']} has about "
              f"{usable_s / 60:.0f} min before its {reserve_pct:g}% reserve (estimated endurance {endurance_min:g} min)")
    return {"ok": ok, "checked": True, "needed_s": needed_s, "usable_s": round(usable_s, 1), "detail": detail}


def night_check(model: dict[str, Any], conditions: dict[str, Any]) -> tuple[str, str, str]:
    """Daylight matters to what the camera can show; lights matter to the law."""
    daylight = conditions.get("daylight")
    if daylight:
        return "pass", "daylight", ""
    if daylight is None:
        if model["flight"]["night_operations"]:
            return "pass", "daylight not stated (this model can inspect at night)", ""
        return "warn", f"daylight not stated: the {model['name']} is a daytime aircraft, confirm it is light outside", ""
    if not model["flight"]["night_operations"]:
        return "fail", (f"dark: the {model['camera']['name']} cannot show a dark property, so a night inspection with the "
                        f"{model['name']} would show nothing useful (use Sentinel Thermal, or wait for daylight)"), "camera_cannot_see_in_the_dark"
    if model["requires"]["lights_at_night"] and not conditions.get("lights_ok"):
        return "fail", "dark: the anti-collision lights must be confirmed on for a night flight", "night_lights_not_confirmed"
    return "pass", "dark: thermal camera and lights confirmed", ""


# ---- comparison and recommendation -------------------------------------------------------


def public_model(model: dict[str, Any]) -> dict[str, Any]:
    data = copy.deepcopy(model)
    data["support_note"] = SUPPORT_NOTE if model["status"] == "reference_design" else ""
    return data


def comparison() -> dict[str, Any]:
    """Rows of capabilities against the three tiers, for the comparison screen."""
    tiers = [MODEL_INDEX[model_id] for model_id in TIERS]
    rows = [{"id": key, "label": info["label"], "detail": info["detail"], "values": {m["id"]: m["capabilities"][key] for m in tiers}}
            for key, info in CAPABILITIES.items()]
    facts = [
        {"label": "Parts cost", "values": {m["id"]: m["cost_label"] for m in tiers}},
        {"label": "Camera", "values": {m["id"]: f"{m['camera']['name']} ({m['camera']['zoom']})" for m in tiers}},
        {"label": "Hover time (estimate)", "values": {m["id"]: f"about {m['flight']['endurance_min_est']:g} min" for m in tiers}},
        {"label": "Wind limit (estimate)", "values": {m["id"]: f"{m['flight']['max_wind_mps']:g} m/s" for m in tiers}},
        {"label": "Lowest temperature", "values": {m["id"]: f"{m['flight']['min_temperature_c']:g} °C" for m in tiers}},
        {"label": "Take-off mass (estimate)", "values": {m["id"]: f"about {m['flight']['takeoff_mass_kg_est']:g} kg" for m in tiers}},
        {"label": "Companion computer", "values": {m["id"]: m["hardware"]["companion"] for m in tiers}},
    ]
    shared = ["Flight controller firmware: ArduCopter 4.6/4.7 with the same failsafe and fence audit",
              "Radio: SIYI HM30 video and telemetry link to the Hub's network, plus an independent ExpressLRS pilot radio",
              "Camera protocol: SIYI gimbal cameras over Ethernet (RTSP), gimbal angle through ArduPilot",
              "Software: approvals, route export, readiness, drone video, observations and evidence are identical"]
    return {"models": [public_model(m) for m in tiers], "rows": rows, "facts": facts, "shared": shared,
            "values": CAPABILITY_VALUES, "support_note": SUPPORT_NOTE}


def recommend(needs: dict[str, Any]) -> dict[str, Any]:
    """A starting point from a few facts, with the reasons. Never a promise."""
    reasons: list[str] = []
    cautions = [SUPPORT_NOTE, "Every flight needs an operator's approval, a pilot in command, and weather within the aircraft's limits."]
    choice = "sentinel_lite"
    if needs.get("night"):
        choice = "sentinel_thermal"
        reasons.append("Night inspections need a thermal camera: the Lite and Pro cameras show little of a dark property.")
    else:
        if needs.get("windy"):
            choice = "sentinel_pro"
            reasons.append("Frequent wind above 6 m/s is beyond the Lite's estimated limit; the larger Pro is rated (estimate) to 8 m/s.")
        if needs.get("zoom") or needs.get("standoff"):
            choice = "sentinel_pro"
            reasons.append("Optical zoom lets the Pro look from a higher or farther position, which is less intrusive and keeps clear of obstacles.")
        size = float(needs.get("property_size_m") or 0)
        if size > 120:
            choice = "sentinel_pro"
            reasons.append(f"A {size:g} m property means longer transits; the Pro's estimated endurance leaves more margin.")
        if needs.get("obstacles"):
            choice = "sentinel_pro"
            reasons.append("Trees or wires near likely routes: the Pro can carry a scanning lidar for ArduPilot's avoidance "
                           "(routes still avoid obstacles by design first).")
    if choice == "sentinel_lite":
        reasons.append("Daytime inspections of a typical property need a stabilised camera, GPS return-to-home and audited failsafes; "
                       "the Lite has exactly that at the lowest parts cost.")
    if needs.get("budget") == "lowest" and choice != "sentinel_lite":
        cautions.append("You asked for the lowest cost: the Lite would work only for daytime inspections in light wind.")
    model = MODEL_INDEX[choice]
    return {"model": choice, "name": model["name"], "reasons": reasons, "limitations": model["limitations"], "cautions": cautions,
            "alternatives": [m for m in TIERS if m != choice]}


__all__ = [
    "CAPABILITIES", "MODELS", "MODEL_INDEX", "TIERS", "camera_settings", "command_lockout", "comparison", "endurance_check",
    "estimate_flight_s", "get_model", "model_for", "night_check", "options_for", "parameter_rules", "public_model", "recommend",
    "rule_passes", "simulator_settings", "validate_drone_model", "video_settings", "weather_defaults",
]
