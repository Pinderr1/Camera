"""Hardware-in-the-loop bench test for a Sentinel drone, with the propellers off.

    python -m sentinel.drone.bench run --connection udpin:0.0.0.0:14551 --model sentinel_lite \
        --video rtsp://192.168.144.25:8554/main.264 --measured-voltage 16.42 --interactive --out bench_runs/bench_1

Proves the chain Hub -> link -> flight controller -> camera on real hardware
before anything flies, and leaves a report and a log bundle as evidence.

Safety rules, enforced in code:
- the propellers are off (the operator confirms before anything starts);
- nothing is armed: the aircraft arming at any point aborts the run as a failure;
- Sentinel sends no flight command. It sends its ground-station heartbeat and
  telemetry requests (as in normal operation), plus one read-only request
  (`probe_command_path`) whose answer shows whether the Hub's commands reach
  the flight controller at all;
- parameters are read, never written.

Checks are `pass`, `fail`, `warn`, `manual` (a person must check and record
it), or `skipped` (needs something not given, such as a stream address).
A run passes only if no check failed and the operator confirmed every
manual item. Steps that need a person (unplug the link, power-cycle the
flight controller, switch off the pilot's transmitter) are observed
automatically: the operator acts, Sentinel watches the telemetry.

Standard library apart from the optional camera check (OpenCV)."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import catalog
from .ardupilot import audit_parameters
from .base import valid_coordinates

SUPPORTED_FIRMWARE = ("4.6", "4.7")
# Minimum message rates (Hz) the Lite's commissioned stream rates must deliver to the Hub.
REQUIRED_RATES = {"HEARTBEAT": 0.8, "GLOBAL_POSITION_INT": 1.5, "SYS_STATUS": 0.8, "GPS_RAW_INT": 0.8, "ATTITUDE": 1.5}
USEFUL_RATES = {"EKF_STATUS_REPORT": 0.3, "BATTERY_STATUS": 0.3, "GIMBAL_DEVICE_ATTITUDE_STATUS": 1.0, "HOME_POSITION": 0.0}
CELL_RANGE_V = (3.3, 4.25)  # a LiPo cell between storage-empty and full


@dataclass
class BenchCheck:
    id: str
    title: str
    state: str  # pass | fail | warn | manual | skipped
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)
    how_to_fix: str = ""


@dataclass
class BenchContext:
    """What the bench run talks to. The CLI builds the real ones; tests pass fakes."""

    adapter: Any  # MavlinkTelemetryAdapter (physical: telemetry only)
    model: dict[str, Any]
    hub_system_id: int = 245
    video_source: Callable[[], Any] | None = None  # opens the drone camera stream (closed after the check)
    measured_voltage: float | None = None  # what the person's multimeter shows on the pack
    cells: int | None = None
    media_root: Path | None = None
    ask: Callable[[str], str] | None = None  # interactive prompt; None = no operator present
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    armed_seen: bool = False

    def telemetry(self) -> Any:
        telemetry = self.adapter.telemetry()
        if telemetry.armed:
            self.armed_seen = True
        return telemetry

    def wait(self, predicate: Callable[[], bool], timeout_s: float, step_s: float = 0.25) -> bool:
        deadline = self.clock() + timeout_s
        while self.clock() < deadline:
            self.telemetry()
            if predicate():
                return True
            self.sleep(step_s)
        return False


class ArmedDuringBench(RuntimeError):
    pass


def _guard(ctx: BenchContext) -> None:
    ctx.telemetry()
    if ctx.armed_seen:
        raise ArmedDuringBench("the aircraft armed during the bench test: stop, disconnect the battery, remove the propellers")


# ---- automatic checks ------------------------------------------------------------------------------


def check_link(ctx: BenchContext) -> BenchCheck:
    heard = ctx.wait(lambda: ctx.adapter.telemetry().link_ok, 20)
    status = ctx.adapter.link_status() if hasattr(ctx.adapter, "link_status") else {}
    if not heard:
        return BenchCheck("link", "Telemetry link", "fail", "no heartbeat from a flight controller within 20 s", status,
                          "Check the radio or USB link, the Hub's MAVLink address and port, and the flight controller's power.")
    others = ctx.adapter.telemetry().other_vehicles
    if others:
        return BenchCheck("link", "Telemetry link", "warn", f"heartbeats from the aircraft and from other autopilots: {others}", status,
                          "Only one autopilot should be on the bench link; give each aircraft its own system id.")
    return BenchCheck("link", "Telemetry link", "pass", f"heartbeats from vehicle {status.get('vehicle', '?')}", status)


def check_firmware(ctx: BenchContext, expected_prefix: str = "") -> BenchCheck:
    ctx.wait(lambda: bool(ctx.adapter.telemetry().autopilot_version), 10)
    info = dict(getattr(ctx.adapter, "autopilot_info", {}) or {})
    version = ctx.adapter.telemetry().autopilot_version or info.get("version", "")
    if not version:
        return BenchCheck("firmware", "Firmware version", "manual", "the flight controller did not report its version to the Hub", info,
                          "With the command lockout on, the Hub cannot ask: connect the pilot's ground station once (it asks), or read "
                          "the version in the ground station and record it.")
    supported = any(version.startswith(prefix) for prefix in SUPPORTED_FIRMWARE)
    if expected_prefix and not version.startswith(expected_prefix):
        return BenchCheck("firmware", "Firmware version", "fail", f"{version}, expected {expected_prefix}", info,
                          f"Flash the pinned ArduCopter {expected_prefix} release and re-load the commissioning parameters.")
    return BenchCheck("firmware", "Firmware version", "pass" if supported else "fail",
                      f"ArduCopter {version}" + (f" (git {info['git']})" if info.get("git") else ""), info,
                      "" if supported else f"Use ArduCopter {' or '.join(SUPPORTED_FIRMWARE)}.")


def check_parameters(ctx: BenchContext, max_altitude_m: float = 25, transit_altitude_m: float = 12) -> BenchCheck:
    ctx.wait(lambda: bool(ctx.adapter.parameters()), 90, step_s=1.0)
    parameters = ctx.adapter.parameters()
    if not parameters:
        return BenchCheck("parameters", "Flight-controller parameters", "fail", "parameters not received within 90 s",
                          ctx.adapter.link_status() if hasattr(ctx.adapter, "link_status") else {},
                          "With the lockout on, connect the pilot's ground station: Sentinel reads the parameters it downloads.")
    audit = audit_parameters(parameters, max_altitude_m=max_altitude_m, safe_transit_altitude_m=transit_altitude_m,
                             extra_rules=catalog.parameter_rules(ctx.model, {}), extra_label=ctx.model["name"])
    failing = [f for f in audit["findings"] if not f["ok"]]
    blocking = [f for f in failing if f["blocking"]]
    detail = f"{len(parameters)} parameters read; fingerprint {audit['fingerprint']}"
    if blocking:
        detail += "; blocking: " + "; ".join(f"{f['param']} {f['actual']} (needs {f['expected']})" for f in blocking[:6])
    elif failing:
        detail += "; warnings: " + ", ".join(f["param"] for f in failing)
    return BenchCheck("parameters", "Failsafe, fence and model parameters", "fail" if blocking else "warn" if failing else "pass", detail,
                      {"count": len(parameters), "fingerprint": audit["fingerprint"], "findings": failing},
                      "Set the named parameters in the ground station (Sentinel never writes them), then run the bench test again.")


def check_lockout(ctx: BenchContext) -> BenchCheck:
    parameters = ctx.adapter.parameters()
    telemetry = ctx.adapter.telemetry()
    state, detail = catalog.command_lockout(ctx.model, parameters, ctx.hub_system_id, None, signed=telemetry.link_signed)
    probe = ctx.adapter.probe_command_path(3.0) if hasattr(ctx.adapter, "probe_command_path") else {"state": "unknown", "detail": "no probe"}
    evidence = {"configured": detail, "probe": probe}
    if probe["state"] == "open":
        return BenchCheck("lockout", "Hub command lockout", "fail", "the flight controller ANSWERED a request from the Hub: " + probe["detail"],
                          evidence, "Set MAV_OPTIONS bit 0 and MAV_GCS_SYSID to the pilot's ground station (or enable MAVLink signing, "
                                    "or pair a companion relay), then repeat. The real aircraft is never flown before this passes.")
    if probe["state"] == "unknown":
        return BenchCheck("lockout", "Hub command lockout", "fail", "the probe could not run: " + probe["detail"], evidence)
    if state != "pass":
        return BenchCheck("lockout", "Hub command lockout", "warn", f"the probe was ignored, but the configuration is not a recognised lockout: {detail}",
                          evidence, "A receive-only link also ignores the probe: make the lockout explicit in the parameters.")
    return BenchCheck("lockout", "Hub command lockout", "pass", f"probe ignored by the flight controller; {detail}", evidence)


def check_rates(ctx: BenchContext, seconds: float = 10.0) -> BenchCheck:
    counts = getattr(ctx.adapter, "message_counts", None)
    if counts is None:
        return BenchCheck("rates", "Telemetry rates", "skipped", "this adapter does not count messages")
    before = dict(counts)
    started = ctx.clock()
    deadline = started + seconds
    while ctx.clock() < deadline:
        _guard(ctx)
        ctx.sleep(0.5)
    elapsed = max(0.1, ctx.clock() - started)
    rates = {name: round((counts.get(name, 0) - before.get(name, 0)) / elapsed, 2) for name in sorted(set(counts) | set(REQUIRED_RATES))}
    missing = [f"{name} {rates.get(name, 0):g} Hz (needs {need:g})" for name, need in REQUIRED_RATES.items() if rates.get(name, 0) < need]
    absent = [name for name in USEFUL_RATES if name not in counts]
    state = "fail" if missing else "warn" if absent else "pass"
    detail = ("too slow: " + ", ".join(missing)) if missing else "required streams arriving" + (f"; never seen: {', '.join(absent)}" if absent else "")
    return BenchCheck("rates", "Telemetry rates", state, detail, {"rates_hz": rates},
                      "Set the stream rates on the radio port in the ground station (ArduCopter 4.7: MAV2_* for TELEM1; 4.6: SR1_*)."
                      if missing else "")


def check_gps(ctx: BenchContext) -> BenchCheck:
    telemetry = ctx.adapter.telemetry()
    evidence = {"fix": telemetry.gps_fix, "satellites": telemetry.satellites, "hdop": telemetry.hdop, "gps_reports": bool(telemetry.gps_ms)}
    if not telemetry.gps_ms:
        return BenchCheck("gps", "GPS detected", "fail", "no GPS reports (GPS_RAW_INT) from the flight controller", evidence,
                          "Check the GPS cable and the GPS port settings (GPS1_TYPE / SERIALn_PROTOCOL 5).")
    if telemetry.gps_fix < 3:
        return BenchCheck("gps", "GPS detected", "warn", f"GPS present, no 3D fix ({telemetry.satellites} satellites): expected indoors", evidence,
                          "Repeat outdoors with open sky before the flight stage; a 3D fix within a few minutes is required.")
    if not valid_coordinates(telemetry.lat, telemetry.lon):
        return BenchCheck("gps", "GPS detected", "fail", "3D fix but no valid position from the estimator", evidence)
    return BenchCheck("gps", "GPS detected", "pass", f"3D fix, {telemetry.satellites} satellites, HDOP {telemetry.hdop}", evidence)


def check_battery(ctx: BenchContext) -> BenchCheck:
    telemetry = ctx.adapter.telemetry()
    volts, amps = telemetry.battery_voltage, telemetry.battery_current_a
    evidence = {"reported_v": volts, "measured_v": ctx.measured_voltage, "current_a": amps, "pct": telemetry.battery_pct,
                "rejected_readings": telemetry.battery_rejected, "fc_state": telemetry.battery_state}
    if volts is None:
        return BenchCheck("battery", "Battery monitor", "fail", "no battery voltage reported", evidence,
                          "Set BATT_MONITOR (4 for an analog power module) and check the power-module cable.")
    battery_text = ctx.model["hardware"].get("battery", "")  # e.g. "4S 5000 mAh LiPo, two packs"
    cells = ctx.cells or (int(battery_text[0]) if battery_text[:1].isdigit() and battery_text[1:2] == "S" else None)
    problems = []
    if cells and not cells * CELL_RANGE_V[0] <= volts <= cells * CELL_RANGE_V[1]:
        problems.append(f"{volts:.2f} V is implausible for {cells}S")
    if ctx.measured_voltage is not None and abs(volts - ctx.measured_voltage) > 0.15:
        problems.append(f"reports {volts:.2f} V, multimeter {ctx.measured_voltage:.2f} V (more than 0.15 V apart)")
    if amps is not None and not 0.05 <= amps <= 8.0:
        problems.append(f"current {amps:g} A with the propellers off is implausible")
    if problems:
        return BenchCheck("battery", "Battery monitor", "fail", "; ".join(problems), evidence,
                          "Calibrate BATT_VOLT_MULT (and BATT_AMP_PERVLT) in the ground station against the multimeter.")
    if ctx.measured_voltage is None:
        return BenchCheck("battery", "Battery monitor", "manual", f"{volts:.2f} V reported; compare with a multimeter on the pack", evidence,
                          "Run again with --measured-voltage.")
    return BenchCheck("battery", "Battery monitor", "pass", f"{volts:.2f} V (multimeter {ctx.measured_voltage:.2f} V)"
                      + (f", {amps:g} A" if amps is not None else ""), evidence)


def check_navigation(ctx: BenchContext) -> BenchCheck:
    telemetry = ctx.adapter.telemetry()
    prearm = [m for m in telemetry.messages if m.lower().startswith("prearm")][-3:]
    evidence = {"ekf_ok": telemetry.ekf_ok, "prearm_ok": telemetry.prearm_ok, "prearm_messages": prearm}
    if telemetry.ekf_ok is None:
        return BenchCheck("navigation", "Navigation (EKF) and pre-arm", "warn", "EKF status not reported (EKF_STATUS_REPORT missing)", evidence,
                          "Include EXTRA3 in the radio port's stream rates.")
    if telemetry.prearm_ok is False:
        return BenchCheck("navigation", "Navigation (EKF) and pre-arm", "warn", "pre-arm checks failing: " + ("; ".join(prearm) or "see the ground station"),
                          evidence, "Indoors, GPS-related pre-arm failures are expected; anything else must be fixed.")
    return BenchCheck("navigation", "Navigation (EKF) and pre-arm", "pass" if telemetry.ekf_ok else "warn",
                      "EKF healthy" if telemetry.ekf_ok else "EKF not healthy yet (indoors without GPS this is expected)", evidence)


def check_gimbal(ctx: BenchContext) -> BenchCheck:
    ctx.wait(lambda: bool(ctx.adapter.telemetry().gimbal_ms), 5)
    telemetry = ctx.adapter.telemetry()
    parameters = ctx.adapter.parameters()
    mount = parameters.get("MNT1_TYPE")
    evidence = {"MNT1_TYPE": mount, "pitch": telemetry.gimbal_pitch_deg, "yaw": telemetry.gimbal_yaw_deg}
    if not ctx.model["camera"].get("reports_attitude"):
        return BenchCheck("gimbal", "Gimbal angle", "skipped", "this model's camera does not report its angle", evidence)
    if not telemetry.gimbal_ms:
        return BenchCheck("gimbal", "Gimbal angle", "fail", f"no gimbal attitude reports (MNT1_TYPE {mount})", evidence,
                          "Set MNT1_TYPE 8 and the gimbal's serial port to protocol 8 (SIYI), reboot, and add GIMBAL_DEVICE_ATTITUDE_STATUS "
                          "to the radio port's streams if it is missing.")
    return BenchCheck("gimbal", "Gimbal angle", "pass", f"pitch {telemetry.gimbal_pitch_deg}°, yaw {telemetry.gimbal_yaw_deg}°", evidence)


def check_camera(ctx: BenchContext, seconds: float = 10.0) -> BenchCheck:
    if ctx.video_source is None:
        return BenchCheck("camera", "Camera stream", "skipped", "no stream address given (--video)")
    from .video import VideoQualityMonitor

    source = None
    started = ctx.clock()
    try:
        source = ctx.video_source()
        quality = VideoQualityMonitor(5.0)
        frames = live = 0
        rejected: dict[str, int] = {}
        resolution = None
        first_frame_s = None
        while ctx.clock() - started < seconds:
            frame = source.read()
            if frame is None:
                ctx.sleep(0.05)
                continue
            frames += 1
            first_frame_s = first_frame_s if first_frame_s is not None else round(ctx.clock() - started, 2)
            resolution = resolution or [int(frame.image.shape[1]), int(frame.image.shape[0])]
            ok, why, _, is_live = quality.check(frame.image, frame.timestamp_ms)
            if ok and is_live:
                live += 1
            elif not ok:
                rejected[why] = rejected.get(why, 0) + 1
    except Exception as error:
        return BenchCheck("camera", "Camera stream", "fail", f"cannot open the stream: {type(error).__name__}: {error}"[:300], {},
                          "Check the camera's address, that the Hub has an address on the camera's network, and the Ethernet lead.")
    finally:
        if source is not None:
            try:
                source.close()
            except Exception:
                pass
    elapsed = max(0.1, ctx.clock() - started)
    evidence = {"frames": frames, "live_frames": live, "fps": round(frames / elapsed, 1), "resolution": resolution, "rejected": rejected,
                "first_frame_s": first_frame_s}
    if live < 3:
        return BenchCheck("camera", "Camera stream", "fail", f"no live picture: {frames} frames, rejected {rejected or 'none'}", evidence,
                          "A frozen or blank picture is never counted as video. Check the camera and the stream.")
    state = "pass" if frames / elapsed >= 2.0 else "warn"
    return BenchCheck("camera", "Camera stream", state, f"live picture, {evidence['fps']} fps at {resolution}; "
                      "latency: measure glass-to-glass with a clock in view (manual item)", evidence)


def check_storage(ctx: BenchContext, minimum_free_gb: float = 2.0) -> BenchCheck:
    root = ctx.media_root or Path(tempfile.gettempdir())
    try:
        root.mkdir(parents=True, exist_ok=True)
        payload = b"sentinel bench evidence check " + str(time.time()).encode()
        digest = hashlib.sha256(payload).hexdigest()
        target = root / f"bench_evidence_check_{int(time.time() * 1000)}.bin"
        target.write_bytes(payload)
        ok = hashlib.sha256(target.read_bytes()).hexdigest() == digest
        target.unlink()
        free_gb = shutil.disk_usage(root).free / 1e9
    except OSError as error:
        return BenchCheck("storage", "Evidence storage", "fail", f"cannot write evidence under {root}: {error}", {"root": str(root)},
                          "Fix the media folder's permissions or the disk.")
    evidence = {"root": str(root), "free_gb": round(free_gb, 1), "hash_verified": ok}
    if not ok:
        return BenchCheck("storage", "Evidence storage", "fail", "a written file read back with a different hash", evidence)
    if free_gb < minimum_free_gb:
        return BenchCheck("storage", "Evidence storage", "warn", f"only {free_gb:.1f} GB free", evidence, "Free disk space before flights.")
    return BenchCheck("storage", "Evidence storage", "pass", f"write, read-back and hash verified; {free_gb:.0f} GB free", evidence)


# ---- operator-guided steps (observed automatically) -------------------------------------------------------


def step_reconnect(ctx: BenchContext, timeout_s: float = 60) -> BenchCheck:
    if ctx.ask is None:
        return BenchCheck("reconnect", "Link reconnection", "manual", "needs an operator: unplug the link for 5 s and plug it back")
    ctx.ask("Unplug the telemetry link (radio ground unit or USB) for about 5 seconds, plug it back in, then press Enter.")
    lost = ctx.wait(lambda: not ctx.adapter.telemetry().link_ok, 10)
    back = ctx.wait(lambda: ctx.adapter.telemetry().link_ok, timeout_s)
    evidence = {"loss_seen": lost, "recovered": back, "reconnects": getattr(ctx.adapter, "reconnects", None)}
    if not back:
        return BenchCheck("reconnect", "Link reconnection", "fail", f"telemetry did not come back within {timeout_s:g} s", evidence)
    return BenchCheck("reconnect", "Link reconnection", "pass" if lost else "warn",
                      "link loss seen and telemetry recovered" if lost else "telemetry flowing, but no loss was seen (unplugged long enough?)", evidence)


def step_power_cycle(ctx: BenchContext, timeout_s: float = 90) -> BenchCheck:
    if ctx.ask is None:
        return BenchCheck("power_cycle", "Flight-controller power cycle", "manual", "needs an operator: power-cycle the flight controller")
    before = ctx.adapter.telemetry().fc_reboots
    ctx.ask("Disconnect the flight controller's power (battery or bench supply) for 5 seconds, reconnect it, then press Enter.")
    seen = ctx.wait(lambda: ctx.adapter.telemetry().fc_reboots > before or not ctx.adapter.telemetry().link_ok, 20)
    back = ctx.wait(lambda: ctx.adapter.telemetry().link_ok, timeout_s)
    restarted = ctx.adapter.telemetry().fc_reboots > before
    evidence = {"restart_detected": restarted, "telemetry_back": back}
    if not back:
        return BenchCheck("power_cycle", "Flight-controller power cycle", "fail", "telemetry did not return after the power cycle", evidence)
    return BenchCheck("power_cycle", "Flight-controller power cycle", "pass" if restarted else "warn",
                      "restart detected; Sentinel discarded the old settings and re-reads them" if restarted
                      else "telemetry back, but the restart itself was not detected (boot clock not seen going back)", evidence)


def step_rc_failsafe(ctx: BenchContext, timeout_s: float = 20) -> BenchCheck:
    if ctx.ask is None:
        return BenchCheck("rc_failsafe", "Pilot radio failsafe", "manual", "needs an operator: switch the pilot's transmitter off")
    seen_before = ctx.adapter.telemetry().message_count
    ctx.ask("Switch the pilot's transmitter OFF (propellers stay off), wait for the prompt, then press Enter.")
    noticed = ctx.wait(lambda: any(("radio" in m.lower() or "rc " in m.lower() or "throttle" in m.lower()) and "failsafe" in m.lower()
                                   for m in ctx.adapter.telemetry().messages[-10:]) and ctx.adapter.telemetry().message_count > seen_before, timeout_s)
    ctx.ask("Switch the transmitter back ON, then press Enter.")
    return BenchCheck("rc_failsafe", "Pilot radio failsafe", "pass" if noticed else "manual",
                      "the flight controller reported a radio failsafe" if noticed else
                      "no failsafe message seen: confirm in the ground station that the radio failsafe triggers (FS_THR_ENABLE)",
                      {"messages": ctx.adapter.telemetry().messages[-5:]})


MANUAL_ITEMS = [
    ("propellers_off", "Propellers removed for the whole bench test"),
    ("compass_calibrated", "Compass and accelerometer calibrated after final assembly (recorded as maintenance 'calibration')"),
    ("latency", "Video latency measured glass-to-glass with a stopwatch or clock in view (record the value)"),
    ("gimbal_moves", "Gimbal follows the pilot's pitch control and returns to its default angle"),
    ("vtx_gps_interference", "With the video radio transmitting, the GPS keeps its satellite count (no interference)"),
]


def run_bench(ctx: BenchContext, *, expected_firmware: str = "", confirmed: dict[str, bool] | None = None,
              on_check: Callable[[BenchCheck], None] | None = None) -> dict[str, Any]:
    """Run every check in order and return the report. Aborts on arming."""
    checks: list[BenchCheck] = []

    def add(check: BenchCheck) -> None:
        checks.append(check)
        if on_check:
            on_check(check)

    aborted = ""
    steps: list[Callable[[], BenchCheck]] = [
        lambda: check_link(ctx), lambda: check_firmware(ctx, expected_firmware), lambda: check_parameters(ctx), lambda: check_lockout(ctx),
        lambda: check_rates(ctx), lambda: check_gps(ctx), lambda: check_battery(ctx), lambda: check_navigation(ctx), lambda: check_gimbal(ctx),
        lambda: check_camera(ctx), lambda: check_storage(ctx), lambda: step_reconnect(ctx), lambda: step_power_cycle(ctx), lambda: step_rc_failsafe(ctx),
    ]
    try:
        for step in steps:
            _guard(ctx)
            add(step())
            if checks[0].state == "fail":
                aborted = "no telemetry link: the other checks need one"
                break
        _guard(ctx)
    except ArmedDuringBench as error:
        aborted = str(error)
        add(BenchCheck("not_armed", "Never armed during the bench test", "fail", aborted))
    confirmed = confirmed or {}
    for item_id, text in MANUAL_ITEMS:
        state = "pass" if confirmed.get(item_id) else "manual"
        add(BenchCheck(item_id, text, state, "confirmed by the operator" if state == "pass" else "to be checked and recorded by a person"))
    failed = [c.id for c in checks if c.state == "fail"]
    open_items = [c.id for c in checks if c.state == "manual"]
    verdict = "fail" if failed or aborted else "incomplete" if open_items else "pass"
    return {"verdict": verdict, "aborted": aborted, "failed": failed, "manual_open": open_items, "model": ctx.model["id"],
            "checks": [asdict(c) for c in checks], "finished": time.strftime("%Y-%m-%d %H:%M:%S")}


def write_bundle(report: dict[str, Any], adapter: Any, out_dir: str | Path) -> dict[str, str]:
    """report.json, report.md, parameters (.param, Mission Planner format) and a telemetry snapshot: the bench evidence."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    telemetry = adapter.telemetry().as_dict()
    parameters = adapter.parameters()
    (out / "report.json").write_text(json.dumps(report | {"telemetry": telemetry, "link": adapter.link_status() if hasattr(adapter, "link_status") else {},
                                                          "autopilot": getattr(adapter, "autopilot_info", {})}, indent=2, default=str))
    (out / "parameters.param").write_text("".join(f"{name},{value:g}\n" for name, value in sorted(parameters.items())))
    lines = [f"# Sentinel bench test: {report['model']}", "", f"**Verdict: {report['verdict'].upper()}** ({report['finished']})", ""]
    if report["aborted"]:
        lines += [f"**Aborted:** {report['aborted']}", ""]
    lines += ["| Check | Result | Detail | Fix |", "| --- | --- | --- | --- |"]
    for check in report["checks"]:
        lines.append(f"| {check['title']} | {check['state'].upper()} | {check['detail'].replace('|', '/')} | {check['how_to_fix'].replace('|', '/')} |")
    lines += ["", "Sentinel sent no flight command during this test and wrote no parameter. "
              "A bench pass is not a flight test: it does not make the aircraft flight-ready."]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": str(out / "report.json"), "markdown": str(out / "report.md"), "parameters": str(out / "parameters.param")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sentinel.drone.bench", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run the propeller-free bench test")
    run.add_argument("--connection", required=True, help="the Hub's MAVLink endpoint, e.g. udpin:0.0.0.0:14551 or COM5")
    run.add_argument("--model", default="sentinel_lite", choices=list(catalog.MODEL_INDEX))
    run.add_argument("--video", default="", help="camera stream URL (rtsp://...) for the camera check")
    run.add_argument("--measured-voltage", type=float, default=None, help="pack voltage measured with a multimeter")
    run.add_argument("--cells", type=int, default=None)
    run.add_argument("--expected-firmware", default="", help="e.g. 4.7.2")
    run.add_argument("--media-root", default="")
    run.add_argument("--interactive", action="store_true", help="guide the operator through the unplug / power-cycle / transmitter steps")
    run.add_argument("--out", default=f"bench_runs/drone_bench_{time.strftime('%Y%m%d_%H%M%S')}")
    args = parser.parse_args(argv)

    if input("Are the propellers OFF and the aircraft restrained? Type 'props off' to continue: ").strip().lower() != "props off":
        print("Stopped: the bench test runs only with the propellers removed.")
        return 2
    from .mavlink import MavlinkTelemetryAdapter

    adapter = MavlinkTelemetryAdapter(args.connection)  # telemetry only: flight commands are refused in code
    adapter.connect()
    video = None
    if args.video:
        from ..sources import open_source

        video = lambda: open_source({"id": "bench_camera", "name": "Drone camera", "source": {"type": "rtsp", "url": args.video}, "fps": 5}, {}, None)  # noqa: E731
    ctx = BenchContext(adapter, catalog.MODEL_INDEX[args.model], video_source=video, measured_voltage=args.measured_voltage, cells=args.cells,
                       media_root=Path(args.media_root) if args.media_root else None, ask=input if args.interactive else None)
    confirmed = {}
    if args.interactive:
        for item_id, text in MANUAL_ITEMS:
            confirmed[item_id] = input(f"{text}? [y/N] ").strip().lower() == "y"
    report = run_bench(ctx, expected_firmware=args.expected_firmware, confirmed=confirmed,
                       on_check=lambda c: print(f"{c.state.upper():8s} {c.title}: {c.detail}", flush=True))
    paths = write_bundle(report, adapter, args.out)
    adapter.disconnect()
    print(f"\nVerdict: {report['verdict'].upper()}  ({paths['markdown']})")
    return 0 if report["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
