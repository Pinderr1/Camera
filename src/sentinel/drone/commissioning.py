"""How far a drone installation really is, from evidence only.

Ten setup steps a homeowner can follow, and one readiness level that never
claims more than Sentinel has seen or a named person has recorded:

    planned < simulated < hardware_detected < connected < partially_configured
            < configured < bench_verified < flight_test_verified < operationally_authorized

- `simulated` is a separate track: a simulator can exercise every step, but
  its level never rises above `simulated` (simulation is not physical readiness).
- `hardware_detected` / `connected` come from telemetry Sentinel received.
- `configured` needs the parameter audit, the Hub command lockout, a live
  camera check and a recorded commissioning.
- `bench_verified` needs a passing bench record (`sentinel.drone.bench`) whose
  parameter fingerprint matches the aircraft's current configuration: change
  a setting and the bench must be repeated.
- `flight_test_verified` and `operationally_authorized` are records made by a
  named person (pilot, owner). Sentinel cannot observe them; it shows who
  recorded them and when. None of these levels enables Sentinel to fly a real
  aircraft: physical aircraft stay telemetry-only.

Input is `DroneManager.status()` plus the maintenance records. Standard library only."""

from __future__ import annotations

from typing import Any

LEVELS = ("planned", "simulated", "hardware_detected", "connected", "partially_configured", "configured", "bench_verified",
          "flight_test_verified", "operationally_authorized")
LEVEL_TEXT = {
    "planned": "Registered, not connected",
    "simulated": "Simulated (not physical readiness)",
    "hardware_detected": "Aircraft detected",
    "connected": "Connected",
    "partially_configured": "Partially configured",
    "configured": "Configured",
    "bench_verified": "Bench verified (propellers off)",
    "flight_test_verified": "Flight-test verified (recorded by the pilot)",
    "operationally_authorized": "Operationally authorized (recorded by the owner)",
}
RECORD_KINDS = {"bench_test": "Bench test", "flight_test": "Flight test", "authorization": "Operational authorization"}


def _step(step_id: str, title: str, state: str, detail: str, action: str = "") -> dict[str, Any]:
    return {"id": step_id, "title": title, "state": state, "detail": detail, "action": action if state != "done" else ""}


def journey(status: dict[str, Any] | None, records: list[dict[str, Any]], *, device: dict[str, Any] | None = None,
            equipment_confirmed: bool = False) -> dict[str, Any]:
    """The setup steps, the current level, and the one next thing to do."""
    device = device or {}
    if status is None:
        steps = [_step("select_model", "Choose your Sentinel drone", "done" if device.get("drone_model") else "todo",
                       device.get("drone_model", "not chosen"), "Pick Lite, Pro or Thermal in the drone's registration.")]
        return {"level": "planned", "level_text": LEVEL_TEXT["planned"], "steps": steps, "next": steps[0] if steps[0]["state"] != "done" else None,
                "simulated": False, "notes": ["The drone is registered but has no connection yet: nothing about it has been verified."]}
    telemetry = status.get("telemetry") or {}
    physical = bool(status.get("physical"))
    model = status.get("model") or {}
    readiness = {check["id"]: check for check in (status.get("readiness") or {}).get("checks", [])}
    audit = status.get("parameter_audit")
    video = status.get("video")
    maintenance = status.get("maintenance") or {}
    by_kind = _latest_by_kind(records)
    fingerprint = (audit or {}).get("fingerprint", "")
    heard = bool(telemetry.get("last_heartbeat_ms"))
    linked = bool(telemetry.get("link_ok")) and bool(telemetry.get("connected"))
    steps = []

    steps.append(_step("select_model", "Choose your Sentinel drone", "done" if model.get("id") and model["id"] != "generic_ardupilot" or device.get("drone_model") else "todo",
                       model.get("name", "not chosen"), "Pick Lite, Pro or Thermal in the drone's registration."))
    steps.append(_step("equipment", "Check the equipment", "done" if equipment_confirmed else "todo",
                       "confirmed by the installer" if equipment_confirmed else "aircraft, radio, camera, pilot transmitter, batteries and charger",
                       "Go through the model's equipment list and confirm every item is there."))
    steps.append(_step("connect_link", "Connect the drone's radio to the Hub", "done" if heard else "todo",
                       "the Hub hears the aircraft" if heard else "nothing heard yet",
                       "Plug the radio ground unit into the Hub's network and power the aircraft (propellers off)."))
    steps.append(_step("discover", "Find the aircraft", "done" if heard else "todo",
                       f"flight controller {telemetry.get('autopilot_version') or 'version not reported'}" if heard else "no heartbeat",
                       "Check the radio is bound and the Hub's MAVLink address matches the radio's."))
    gps_ok = (readiness.get("gps") or {}).get("state") == "pass"
    battery_reported = telemetry.get("battery_voltage") is not None
    steps.append(_step("telemetry", "Check telemetry", "done" if linked and battery_reported and telemetry.get("gps_ms") else "attention" if linked else "todo",
                       ", ".join(filter(None, ["live" if linked else "no live telemetry", "battery reported" if battery_reported else "no battery reading",
                                               "GPS fix" if gps_ok else "no GPS fix yet"])),
                       "Fix the item named here (the troubleshooter explains each one)."))
    camera_ok = bool(video) and video.get("state") in ("ready", "streaming")
    steps.append(_step("camera", "Connect the camera stream", "done" if camera_ok else "attention" if video else "todo",
                       (video or {}).get("detail", "no stream configured"), "Enter the camera's stream address in the drone's settings and check it."))
    lockout = readiness.get("lockout") or {}
    safety_ok = bool(audit) and audit.get("ok") and lockout.get("state") in ("pass", None, "")
    steps.append(_step("safety", "Check the safety settings", "done" if safety_ok else "attention" if audit else "todo",
                       "failsafes, fence and command lockout pass" if safety_ok else
                       "; ".join(f"{f['param']}: {f['actual']}" for f in (audit or {}).get("blocking", [])[:3]) or lockout.get("detail", "not read yet"),
                       "A commissioning engineer sets the named items in the ground station; Sentinel re-reads them."))
    commissioned = bool(maintenance.get("commissioned"))
    steps.append(_step("commissioning", "Record the commissioning", "done" if commissioned else "todo",
                       "recorded" if commissioned else "not recorded",
                       "After the engineer's checks, record 'commissioning' on the Drone tab (it stores the settings fingerprint)."))
    bench = by_kind.get("bench_test")
    bench_current = bool(bench) and bench.get("verdict") == "pass" and (not fingerprint or bench.get("fingerprint") in ("", fingerprint))
    steps.append(_step("diagnostic", "Run the bench diagnostic (propellers off)", "done" if bench_current else "attention" if bench else "todo",
                       (f"passed {bench.get('by', '')}" if bench_current else "the settings changed since the bench test: repeat it" if bench and bench.get("verdict") == "pass"
                        else f"last result: {bench.get('verdict')}" if bench else "not run"),
                       "Run `python -m sentinel.drone.bench run ...` with the propellers off and record the result."))
    blockers = (status.get("readiness") or {}).get("blockers", [])
    steps.append(_step("ready", "Ready for operator-approved inspections", "done" if not blockers else "attention",
                       "all readiness checks pass" if not blockers else f"{len(blockers)} item(s) block approval: {', '.join(blockers[:4])}",
                       "Work through the blocking items; every flight still needs an operator's approval and a pilot."))

    level = _level(physical, heard, linked, steps, bench_current, by_kind)
    notes = []
    if not physical:
        notes.append("Simulated aircraft: every step can be practised, but nothing here is physical readiness.")
    if level in ("flight_test_verified", "operationally_authorized"):
        notes.append("Recorded by a person, not observed by Sentinel.")
    notes.append("Physical aircraft stay telemetry-only: Sentinel proposes inspections, the pilot flies them.")
    upcoming = next((step for step in steps if step["state"] != "done"), None)
    return {"level": level, "level_text": LEVEL_TEXT[level], "level_index": LEVELS.index(level), "steps": steps, "next": upcoming,
            "simulated": not physical, "records": {kind: _public(record) for kind, record in by_kind.items()}, "notes": notes,
            "model": model.get("id", "")}


def _level(physical: bool, heard: bool, linked: bool, steps: list[dict[str, Any]], bench_current: bool, by_kind: dict[str, dict[str, Any]]) -> str:
    if not physical:
        return "simulated"
    if not heard:
        return "planned"
    if not linked:
        return "hardware_detected"
    done = {step["id"] for step in steps if step["state"] == "done"}
    configured = {"telemetry", "camera", "safety", "commissioning"} <= done
    if not configured:
        return "connected" if not ({"telemetry", "camera", "safety", "commissioning"} & done) else "partially_configured"
    if not bench_current:
        return "configured"
    flight = by_kind.get("flight_test")
    if not flight or flight.get("verdict") != "pass" or flight.get("recorded_ms", 0) < by_kind["bench_test"].get("recorded_ms", 0):
        return "bench_verified"
    authorization = by_kind.get("authorization")
    if not authorization or authorization.get("recorded_ms", 0) < flight.get("recorded_ms", 0):
        return "flight_test_verified"
    return "operationally_authorized"


def _latest_by_kind(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        kind = record.get("kind")
        if kind in RECORD_KINDS and record.get("recorded_ms", 0) >= latest.get(kind, {}).get("recorded_ms", -1):
            latest[kind] = record
    return latest


def _public(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in ("kind", "recorded_ms", "by", "verdict", "note", "fingerprint")}


def bench_record(report: dict[str, Any], fingerprint: str, operator: str) -> dict[str, Any]:
    """A maintenance record for a bench run (kept with the drone's logs)."""
    failed = ", ".join(report.get("failed", [])) or "none"
    return {"kind": "bench_test", "by": operator, "verdict": report.get("verdict", "fail"), "fingerprint": fingerprint,
            "note": f"bench {report.get('verdict')}: failed {failed}; open manual items {len(report.get('manual_open', []))}"[:1000]}


__all__ = ["LEVELS", "LEVEL_TEXT", "RECORD_KINDS", "bench_record", "journey"]
