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


def diagnose(status: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    """Findings for every failing or warning readiness check, most important first."""
    readiness = status.get("readiness") or {}
    findings = []
    for check in readiness.get("checks", []):
        if check["state"] not in ("fail", "warn"):
            continue
        causes, steps = _check_guide(check, model, status)
        findings.append({"check": check["id"], "label": check["label"], "state": check["state"], "detail": check["detail"],
                         "blocks_approval": check["state"] == "fail", "causes": [c for c in causes if c], "steps": steps})
    findings.sort(key=lambda f: (not f["blocks_approval"], f["check"] != "link"))
    blocking = [f["label"] for f in findings if f["blocks_approval"]]
    if not readiness:
        summary = "No readiness information yet."
    elif blocking:
        summary = f"{len(blocking)} problem(s) block approval of an inspection: {', '.join(blocking)}."
    elif findings:
        summary = f"Ready for operator-approved inspections, with {len(findings)} warning(s)."
    else:
        summary = "All readiness checks pass. Every flight still needs an operator's approval."
    return {"drone_id": status.get("drone_id"), "model": model["id"], "model_name": model["name"], "physical": bool(status.get("physical")),
            "summary": summary, "findings": findings, "retest": "Readiness is re-checked live: fix an item and check again."}


__all__ = ["diagnose"]
