"""Drone troubleshooting: what a failing readiness check usually means for this
aircraft model, and what a person should do about it.

Input is `DroneManager.status()`; nothing here talks to the aircraft or
changes a setting. Steps are written for the person at the drone or the
ground station. Sentinel never writes flight-controller parameters, so every
parameter fix is "set X in your ground station", and the audit re-reads the
value by itself. Causes are possibilities unless the status proves them
(`confirmed` only when the telemetry or audit shows the fault directly).
Standard library only."""

from __future__ import annotations

from typing import Any

HM30_TIERS = {"lite", "pro", "thermal"}


def _link_steps(model: dict[str, Any], status: dict[str, Any]) -> list[str]:
    if status.get("adapter") == "simulated":
        return ["The built-in simulator is not connected: restart Sentinel or re-save the drone's settings."]
    if status.get("adapter") == "sitl":
        return ["Start ArduPilot SITL (scripts/sitl/start_sitl.sh) and check the drone's connection setting matches its port (tcp:127.0.0.1:5762)."]
    steps = ["Check the aircraft is powered and its flight controller has booted (status LED)."]
    if model["tier"] in HM30_TIERS:
        steps += ["Check the SIYI HM30 ground unit is on and its network cable is plugged into the same network as the Hub.",
                  "Check the HM30 air and ground units are bound (the ground unit shows the link) and the antennas are attached.",
                  "Sentinel listens for MAVLink on the drone's connection setting (for example udpin:0.0.0.0:14551): the ground unit or the "
                  "companion must send to the Hub's address on that port."]
    if model["requires"]["companion"] == "required":
        steps.append("Check the companion computer has booted and its Sentinel relay is running (the Drone tab shows its last report).")
    steps.append("If the Hub has a firewall, allow incoming UDP on the MAVLink port.")
    return steps


def _video_steps(model: dict[str, Any], status: dict[str, Any]) -> list[str]:
    camera = model["camera"]
    if status.get("adapter") == "simulated" or not status.get("physical"):
        return ["Simulated drones use the simulated aerial camera; re-save the drone's settings if it is missing."]
    steps = [f"Enter the {camera['name']}'s stream address in the drone's settings."]
    if camera.get("default_stream"):
        steps.append(f"The camera manual's default is {camera['default_stream']}; it is reachable only when the Hub has an address on the "
                     "camera's network range (through the HM30 ground unit).")
    steps.append("Test the address with a video player on the Hub first; Sentinel analyses drone video only during an approved inspection.")
    return steps


def _audit_steps(status: dict[str, Any]) -> tuple[list[str], list[str]]:
    audit = status.get("parameter_audit") or {}
    causes, steps = [], []
    for finding in audit.get("blocking", []):
        if finding["param"] == "*":
            causes.append("Sentinel has not received the flight controller's parameters yet.")
            steps.append("Wait a minute after the link comes up. With the flight controller's command lockout on, connect the pilot's ground "
                         "station once: Sentinel reads the parameters it downloads over the shared link.")
            continue
        causes.append(f"{finding['param']} is {finding['actual']} (needs {finding['expected']}).")
        steps.append(f"In the ground station set {finding['param']} to {finding['expected']}: {finding['why']}")
    if steps:
        steps.append("Then record commissioning on the Drone tab so later changes are reported.")
    return causes, steps


def _check_guide(check: dict[str, Any], model: dict[str, Any], status: dict[str, Any]) -> tuple[list[str], list[str]]:
    telemetry = status.get("telemetry") or {}
    name = model["name"]
    check_id = check["id"]
    if check_id == "link":
        return (["The aircraft is off, the radio link is down, or the Hub is listening on a different address or port."], _link_steps(model, status))
    if check_id == "battery":
        return ([f"Battery at {telemetry.get('battery_pct')}%."], [f"Fit a charged pack. The {name} needs the launch minimum before Sentinel offers a flight."])
    if check_id == "gps":
        causes = ["No satellite fix yet (indoors, under a roof, or just powered up)."] if telemetry.get("gps_fix", 0) < 3 else \
                 ["Poor GPS geometry near buildings or trees, or interference from the camera or radio."]
        steps = ["Move the aircraft to open sky and wait a few minutes after power-up."]
        if status.get("parameter_audit") and telemetry.get("link_ok") and not telemetry.get("satellites") and not telemetry.get("gps_fix"):
            causes.append("With the flight controller's command lockout on, it ignores the Hub's telemetry requests: GPS data reaches "
                          "Sentinel only if commissioning set the stream rates on the radio port.")
            steps.append("In the ground station set the telemetry stream rates (2-4 Hz) on the port the HM30 is wired to. ArduCopter 4.7 "
                         "numbers MAVLink ports from 1, so TELEM1 (SERIAL1) is MAV2_POSITION / MAV2_EXT_STAT / MAV2_EXTRA1-3; "
                         "ArduCopter 4.6 calls the same port SR1_*.")
        if model["capabilities"].get("dual_gps") == "yes":
            steps.append("Check both GPS receivers are connected (GPS1 and GPS2 ports).")
        return causes, steps
    if check_id == "ekf":
        return (["The flight controller's position estimate has not converged (GPS, compass or vibration)."],
                ["Wait for GPS lock in open sky; if it persists, check the compass calibration and vibration levels in the ground station."])
    if check_id == "prearm":
        return ([check.get("detail") or "A flight-controller pre-arm check is failing."],
                ["Read the PreArm message in the ground station and fix that item; Sentinel never bypasses pre-arm checks."])
    if check_id == "fc_settings":
        return _audit_steps(status)
    if check_id == "lockout":
        if model["requires"]["companion"] == "required":
            return (["The companion relay is not paired or not reporting with its telemetry policy."],
                    ["Pair the companion with the one-time code on the Drone tab and check its relay runs with --policy telemetry."])
        return (["Neither a companion relay nor the flight controller's own command lockout is in place."],
                ["In the ground station set MAV_GCS_SYSID to the pilot's ground station id (usually 255) and MAV_OPTIONS bit 0 (value 1): the "
                 "flight controller then ignores commands from the Hub (Sentinel uses id 245).",
                 "Set the telemetry stream rates on the radio port, because the Hub's own stream requests are ignored too "
                 "(ArduCopter 4.7: MAV2_* for TELEM1; 4.6: SR1_*).",
                 "Or pair a companion computer running Sentinel's relay."])
    if check_id == "companion":
        return ([check.get("detail", "")], ["Pair the companion on the Drone tab (Show pairing code), then run the pairing command on it."])
    if check_id == "video":
        return ([check.get("detail", "")], _video_steps(model, status))
    if check_id == "conditions":
        return ([check.get("detail", "")], ["Confirm today's conditions on the Drone tab: precipitation, visibility, wind, temperature and daylight. "
                                            f"The {name}'s limits: wind {model['flight']['max_wind_mps']:g} m/s, "
                                            f"{model['flight']['min_temperature_c']:g} to {model['flight']['max_temperature_c']:g} °C, no rain."])
    if check_id == "daylight":
        if not model["flight"]["night_operations"]:
            return ([f"The {model['camera']['name']} has no thermal channel."], ["Wait for daylight, or use a Sentinel Thermal for night inspections."])
        return (["Anti-collision lights not confirmed."], ["Switch the strobe and position lights on and confirm it with the conditions."])
    if check_id == "home_fence":
        return (["The take-off point is outside the approved flight area."], ["Move the drone's home position inside the geofence in the drone setup."])
    if check_id == "on_ground":
        return (["An inspection is approved or the aircraft is airborne."], ["Finish or cancel the open inspection first."])
    if check_id == "safety":
        return ([f"Safety monitor state: {check.get('detail')}"], ["Land and check the aircraft; the state clears when telemetry is normal again."])
    if check_id == "launch_point":
        return (["The aircraft is not where the drone's home is configured."], ["Place the aircraft at its home or dock, or update the home position."])
    if check_id == "firmware":
        return ([f"Firmware {telemetry.get('autopilot_version')} is not a tested version."], [f"Use ArduCopter {' or '.join(model['firmware_tested'])}."])
    if check_id == "maintenance":
        return (["The inspection interval has passed."], ["Inspect the aircraft (propellers, arms, battery) and record it in the maintenance log."])
    return ([check.get("detail", "")], [])


# What a flight-health condition (flight_health.py) usually means and what to do, one step per line, most likely first.
HEALTH_GUIDE: dict[str, tuple[str, list[str]]] = {
    "link_lost": ("No telemetry reaches the Hub: the radio link, the aircraft's power, or the Hub's network.",
                  ["Look at the aircraft: is it powered (flight-controller LED)?", "Check the radio ground unit is on and plugged into the Hub's network.",
                   "If the aircraft is flying, the pilot is in charge: the flight controller's failsafe returns or lands it on its own."]),
    "link_unstable": ("The link keeps dropping: range, antennas, interference, or a loose cable.",
                      ["Check the ground unit's antennas are attached and upright, away from metal.", "Move the ground unit closer to where the drone flies, or higher.",
                       "Check the network cable between the ground unit and the Hub."]),
    "link_delayed": ("Telemetry arrives late: a congested radio or a buffering network.",
                     ["Reduce the video bitrate on the radio if it is shared with telemetry.", "Check nothing else on the Hub's network is saturating it."]),
    "position_unknown": ("The flight controller has no position yet (it reports 0,0 until it has one).",
                         ["Take the aircraft outside with open sky and wait a few minutes after power-up."]),
    "position_stale": ("Position reports stopped while other telemetry continues.",
                       ["Check the radio port's position stream rate (ArduCopter 4.7: MAV2_POSITION).", "If it persists, restart the aircraft on the ground."]),
    "gps_no_fix": ("No 3D GPS fix: indoors, under a roof or trees, just powered up, or a GPS cable problem.",
                   ["Move the aircraft to open sky and wait 2-5 minutes.", "Check the GPS cable and that the GPS sits on its mast away from the video radio.",
                    "In flight: the pilot flies manually if the flight controller cannot hold position; return-to-home needs GPS."]),
    "gps_degraded": ("Few satellites or poor accuracy: buildings, trees, or interference from the video radio or camera.",
                     ["Wait in open sky for more satellites.", "Check the GPS mast is upright and away from the HM30 antennas."]),
    "gps_stale": ("GPS reports stopped arriving at the Hub.", ["Check the radio port's EXTRA/EXT_STAT stream rates.", "Check the GPS cable."]),
    "ekf_unhealthy": ("The flight controller's position estimate is not healthy (GPS, compass, vibration).",
                      ["Wait for GPS lock in open sky.", "Recalibrate the compass away from metal and cars.", "Check the vibration levels in the ground station."]),
    "ekf_stale": ("Navigation status reports stopped arriving.", ["Check the EXTRA3 stream rate on the radio port."]),
    "position_jump": ("The position jumped further than the aircraft can fly: a GPS glitch or an estimator reset.",
                      ["Treat positions and distances from that moment with care.", "Check for GPS interference; repeated jumps mean the aircraft should not fly."]),
    "home_mismatch": ("The flight controller set home where it was armed or first got a fix, not at the configured home.",
                      ["Place the aircraft at its configured home before power-up, or update the configured home on the Drone tab."]),
    "home_unknown": ("The flight controller has not reported its home yet.", ["Connect the pilot's ground station once (it requests home), or wait for a GPS fix."]),
    "battery_unknown": ("No battery reading: the power module or BATT_MONITOR.", ["Check BATT_MONITOR (4 for an analog power module) and the power-module cable."]),
    "battery_stale": ("Battery reports stopped arriving: the reading shown is old.", ["Check the EXT_STAT stream rate.", "In flight, judge time by the clock: land soon."]),
    "battery_frozen": ("The battery level has not changed in flight: the monitor may be stuck.", ["Land and check the power-module calibration (BATT_AMP_PERVLT)."]),
    "battery_fast_drain": ("The battery drains faster than the estimate: wind, payload, a weak or cold pack.",
                           ["Plan shorter inspections in this weather.", "Check the pack's age and internal resistance; replace if it sags."]),
    "battery_return_now": ("At the observed drain the battery reaches the reserve before the aircraft could be home.", ["The pilot should return home now."]),
    "battery_failsafe": ("The flight controller's own battery check (voltage or capacity) has triggered.",
                         ["Expect the aircraft to return or land on its own.", "After landing: charge or replace the pack; check BATT_LOW_VOLT for the cell count."]),
    "fence_breach": ("The aircraft left the area its flight controller allows (polygon or height).",
                     ["In flight the flight controller's fence action returns or lands it; the pilot stays ready to take over.",
                      "On the ground: move the aircraft inside the fence, or check the fence loaded on the flight controller matches Sentinel's."]),
    "vehicle_emergency": ("The flight controller reports a critical or emergency state.", ["Read the latest messages in the ground station; the pilot takes over."]),
    "fc_restarted": ("The flight controller restarted: power or a crash of its software.",
                     ["On the ground: check the power connections and BEC.", "Sentinel re-reads the settings before allowing approval again."]),
    "other_vehicle": ("Another autopilot is on the same radio link.", ["Give each aircraft its own system id (MAV_SYSID), or switch the other one off."]),
    "gimbal_stale": ("The gimbal stopped reporting its angle.", ["Check the gimbal's serial cable to the flight controller and MNT1_TYPE 8."]),
    "wind_over_limit": ("Stronger wind than this aircraft's limit (the flight controller's estimate).", ["Return home and wait for calmer weather."]),
}
VIDEO_GUIDE: dict[str, tuple[str, list[str]]] = {
    "unreachable": ("The camera stream cannot be opened: address, network or power.",
                    ["Check the camera has power (its gimbal moves at power-up).", "Check the stream address in the drone's settings.",
                     "Check the Hub has an address on the camera's network range (through the radio ground unit)."]),
    "frozen": ("The camera sends the same picture over and over: a stalled encoder or stream.", ["Restart the camera (power-cycle the aircraft on the ground)."]),
    "blank": ("The camera sends a blank or corrupted picture.", ["Check the lens cap and the camera's Ethernet cable; restart the camera."]),
    "no_video": ("No frames arrive from the camera.", ["Check the radio link's video, then the camera's power and cable."]),
    "unverified": ("The camera stream has not been checked recently.", ["Power the aircraft (propellers off): Sentinel checks the stream by itself."]),
}


# A failing readiness check already explains these; the health finding would repeat it.
COVERED_BY = {"link_lost": "link", "battery_stale": "battery", "battery_unknown": "battery", "battery_failsafe": "battery", "gps_no_fix": "gps",
              "gps_degraded": "gps", "gps_stale": "gps", "ekf_unhealthy": "ekf", "position_unknown": "telemetry", "position_stale": "telemetry",
              "vehicle_emergency": "telemetry", "fence_breach": "telemetry", "home_mismatch": "home_position", **{state: "video" for state in VIDEO_GUIDE}}


def diagnose_health(health: dict[str, Any] | None, video: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Findings for the live flight-health conditions and the drone camera."""
    findings = []
    for condition in (health or {}).get("conditions", []):
        cause, steps = HEALTH_GUIDE.get(condition["id"], (condition["text"], []))
        findings.append({"check": f"health:{condition['id']}", "label": condition["text"], "state": "fail" if condition["severity"] == "critical" else "warn",
                         "detail": condition["text"], "blocks_approval": condition["severity"] == "critical", "causes": [cause], "steps": steps,
                         "evidence": condition.get("evidence", {})})
    state = (video or {}).get("state", "")
    if state in VIDEO_GUIDE:
        cause, steps = VIDEO_GUIDE[state]
        findings.append({"check": f"video:{state}", "label": "Drone camera", "state": "warn", "detail": (video or {}).get("detail", ""),
                         "blocks_approval": False, "causes": [cause], "steps": steps})
    return findings


def diagnose(status: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    """Findings for every failing or warning readiness check and live health
    condition, most important first, and the single next step to try."""
    readiness = status.get("readiness") or {}
    findings = []
    for check in readiness.get("checks", []):
        if check["state"] not in ("fail", "warn"):
            continue
        causes, steps = _check_guide(check, model, status)
        findings.append({"check": check["id"], "label": check["label"], "state": check["state"], "detail": check["detail"],
                         "blocks_approval": check["state"] == "fail", "causes": [c for c in causes if c], "steps": steps})
    known = {f["check"] for f in findings}
    findings += [f for f in diagnose_health(status.get("health"), status.get("video")) if COVERED_BY.get(f["check"].split(":", 1)[-1], f["check"]) not in known]
    findings.sort(key=lambda f: (not f["blocks_approval"], f["check"] != "link"))
    blocking = [f["label"] for f in findings if f["blocks_approval"]]
    if not readiness:
        summary = "No readiness information yet."
    elif blocking:
        summary = f"{len(blocking)} problem(s) block approval of an inspection: {', '.join(blocking)}."
    elif findings:
        summary = f"Ready for operator-approved inspections, with {len(findings)} warning(s). Every flight still needs an operator's approval."
    else:
        summary = "All readiness checks pass. Every flight still needs an operator's approval."
    first = next((f for f in findings if f["steps"]), None)
    next_step = ({"check": first["check"], "label": first["label"], "step": first["steps"][0], "cause": (first["causes"] or [""])[0],
                  "remaining_steps": len(first["steps"]) - 1} if first else None)
    return {"drone_id": status.get("drone_id"), "model": model["id"], "model_name": model["name"], "physical": bool(status.get("physical")),
            "summary": summary, "findings": findings, "next_step": next_step,
            "retest": "Readiness is re-checked live: fix an item and check again."}


__all__ = ["diagnose"]
