"""Drone failure scenarios against ArduPilot SITL (real ArduCopter firmware).

    bash scripts/sitl/start_sitl_instance.sh 2 3 config/drone/sentinel-lite-sitl.parm     # WSL; ports 5780-5783
    python scripts/sitl/fault_scenarios.py --fc tcp:127.0.0.1:5782 --out bench_runs/faults_lite [--only gps_loss_in_flight,...]

Sentinel Lite's operating model: the aircraft's one radio (emulated by
`FaultLink`) carries the Hub's telemetry and the pilot's ground station (the
scripted stand-in, system id 255, the only one the flight controller obeys);
Sentinel proposes, the pilot flies, Sentinel follows from telemetry. Faults
are injected through SITL's SIM_* parameters (sensors, battery, wind), the
radio (loss, delay, filtering, cuts) and the camera (freeze, blank, drop).

Not part of the product. Simulators only: the stand-in refuses a vehicle
without SIM_* parameters and every endpoint here is loopback."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gcs_standin import SitlGroundStation  # noqa: E402

from sentinel.drone import catalog  # noqa: E402
from sentinel.drone.ardupilot import version_text  # noqa: E402
from sentinel.drone.base import FlightCommandRejected, distance_m, geo_to_map_point, map_point_to_geo  # noqa: E402
from sentinel.drone.faults import FaultLink, FaultyVideoSource, Scenario, VideoFault, run_scenarios, write_report  # noqa: E402
from sentinel.drone.flight_health import FlightHealthMonitor  # noqa: E402
from sentinel.drone.geofence import Geofence  # noqa: E402
from sentinel.drone.mission import plan_route, to_qgc_plan  # noqa: E402

ORIGIN = (45.0, -75.0)
FENCE_MAP = [[0, 0], [40, 0], [40, 28], [0, 28]]
HOME_MAP = (6.0, 4.0)
SHED = (35.5, 3.0)
HOME_AMSL_M = 80.0
SITL_DEFAULTS = {"SIM_GPS1_ENABLE": 1, "SIM_GPS1_NUMSATS": 10, "SIM_GPS1_GLTCH_X": 0, "SIM_GPS1_GLTCH_Y": 0, "SIM_BATT_VOLTAGE": 12.6,
                 "SIM_WIND_SPD": 0, "SIM_WIND_TURB": 0, "BATT_CAPACITY": 3300, "BATT_LOW_MAH": 0, "FENCE_ENABLE": 1, "MAV_OPTIONS": 1,
                 "SIM_SPEEDUP": 3}
SIGNING_KEY = bytes(range(1, 33))  # test key for the simulator only


class Lab:
    """The simulated world shared by the scenarios: SITL, the faulty radio, the
    pilot's ground station. Each scenario gets a fresh Sentinel."""

    def __init__(self, fc: str, hub_port: int, pilot_port: int, model_id: str) -> None:
        self.model = catalog.MODEL_INDEX[model_id]
        self.hub_port, self.pilot_port = hub_port, pilot_port
        self.fence = Geofence([list(map_point_to_geo(ORIGIN, x, y)) for x, y in FENCE_MAP], 25, 5)
        self.home = map_point_to_geo(ORIGIN, *HOME_MAP)
        self.link = FaultLink(fc, f"udp:127.0.0.1:{hub_port}", [f"udp:127.0.0.1:{pilot_port}"]).start()
        self.pilot = SitlGroundStation(f"udpin:127.0.0.1:{pilot_port}")
        if self.pilot.get_param("MAV_OPTIONS") != 1:
            raise SystemExit("SITL must run the Lite commissioning (config/drone/sentinel-lite-sitl.parm): MAV_OPTIONS is not 1")
        self.pilot.start_heartbeats()
        self.firmware = self.read_firmware()
        self.changed: dict[str, float] = {}
        self.needs_reboot = False
        self.commission()

    def read_firmware(self) -> str:
        self.pilot.link.mav.command_long_send(*self.pilot.target, 512, 0, 148, 0, 0, 0, 0, 0, 0)
        message = self.pilot.link.recv_match(type="AUTOPILOT_VERSION", blocking=True, timeout=5)
        return version_text(int(message.flight_sw_version)) if message else "unknown"

    def commission(self) -> None:
        route = plan_route(self.home, map_point_to_geo(ORIGIN, *SHED), self.fence, safe_transit_altitude_m=12, inspection_altitude_m=12, hover_s=4)
        self.pilot.load_fence(to_qgc_plan(route, self.fence))
        for name in ("BATT_CAPACITY", "BATT_LOW_MAH"):
            self.pilot.set_param(name, SITL_DEFAULTS[name])

    def set(self, name: str, value: float) -> None:
        """Change a parameter for this scenario; `restore` puts it back."""
        if name not in self.changed:
            current = self.pilot.get_param(name)
            self.changed[name] = SITL_DEFAULTS.get(name, current if current is not None else value)
        self.pilot.set_param(name, value)

    # ---- one Sentinel ---------------------------------------------------------------

    def sentinel(self, *, adapter: str = "telemetry", video: dict[str, Any] | None = None, video_faults: list[VideoFault] | None = None,
                 record_evidence: Any = None, **safety: Any) -> dict[str, Any]:
        from sentinel.drone.manager import DroneManager, DroneSafetyConfig, WeatherLimits
        from sentinel.drone.mavlink import MavlinkTelemetryAdapter, SitlMavlinkAdapter
        from sentinel.store import EventStore

        endpoint = f"udpin:127.0.0.1:{self.hub_port}"
        hub = (SitlMavlinkAdapter(endpoint, allow_flight_commands=True, command_timeout_s=3) if adapter == "sitl_commands"
               else MavlinkTelemetryAdapter(endpoint))
        hub.connect()
        directory = tempfile.mkdtemp(prefix="sentinel-faults-")
        store = EventStore(Path(directory) / "sentinel.db")
        settings = {"inspection_altitude_m": 12, "hover_s": 6} | safety
        manager = DroneManager(hub, self.fence, ORIGIN, DroneSafetyConfig(**settings), records=store, drone_id="lite", home=self.home,
                               audit=store.audit, model=self.model, weather=WeatherLimits.from_dict(catalog.weather_defaults(self.model)))
        health = FlightHealthMonitor(endurance_min_est=self.model["flight"]["endurance_min_est"], cruise_mps=self.model["flight"]["cruise_mps"],
                                     configured_home=self.home, reserve_pct=manager.safety.rth_battery_pct,
                                     max_wind_mps=self.model["flight"]["max_wind_mps"])
        world: dict[str, Any] = {"adapter": hub, "manager": manager, "store": store, "health": health, "directory": directory, "worker": None,
                                 "video_source": {}}
        if video is not None:
            from sentinel.drone.observation import CameraModel
            from sentinel.drone.video import DroneVideoWorker, video_source_for

            settings_v = catalog.video_settings(self.model, {"video": video}, simulated=True) | {"fps": 5, "check_every_s": 3600}
            camera = CameraModel.from_dict(settings_v["camera"])

            def source():
                inner = video_source_for(settings_v, origin=ORIGIN, camera=camera, telemetry=hub.telemetry, config={}, secrets=None)
                wrapped = FaultyVideoSource(inner, list(video_faults or []))
                world["video_source"]["latest"] = wrapped
                return wrapped

            worker = DroneVideoWorker(manager, settings_v, config={}, media_root=Path(directory) / "media",
                                      record_evidence=record_evidence or store.record_evidence, source_factory=source)
            manager.video_status = worker.status
            worker.start()
            world["worker"] = worker
        else:
            manager.video_status = lambda: {"state": "ready", "detail": "camera stream configured (not part of this scenario)"}
        manager.set_conditions("tester", precipitation=False, visibility_ok=True, wind_mps=2, temperature_c=12, daylight=True)
        return world

    def snapshot(self, world: dict[str, Any]) -> dict[str, Any]:
        manager, health = world["manager"], world["health"]
        telemetry = world["adapter"].telemetry()
        request = next(iter(sorted(manager.requests.values(), key=lambda r: -r.created_ms)), None)
        return {
            "link_ok": telemetry.link_ok, "flight_state": manager.flight_state, "mode": telemetry.flight_mode, "armed": telemetry.armed,
            "alt_band_m": int(telemetry.alt_m // 5 * 5), "safety_state": manager.safety_state, "gps_fix": telemetry.gps_fix,
            "ekf_ok": telemetry.ekf_ok, "battery_state": telemetry.battery_state,
            "request": f"{request.status}/{request.phase}" if request else "", "health": sorted(c["id"] for c in health.conditions),
            "advisories": [a["reason"] for a in manager.advisories], "fc_reboots": telemetry.fc_reboots,
            "video": (world["worker"].state if world["worker"] else ""), "blockers": sorted(manager.readiness()["blockers"]),
            "prearm": next((m for m in reversed(telemetry.messages) if m.lower().startswith("prearm")), "") if telemetry.prearm_ok is False else "",
        }

    def tick(self, world: dict[str, Any]) -> None:
        world["manager"].step(0.5)
        world["health"].update(world["adapter"].telemetry(), int(time.time() * 1000))
        if getattr(self, "trace", False) and time.monotonic() >= world.get("trace_next", 0):
            world["trace_next"] = time.monotonic() + 15
            print("TRACE " + json.dumps(self.snapshot(world)), flush=True)

    def close_sentinel(self, world: dict[str, Any]) -> None:
        if world.get("worker"):
            world["worker"].stop()
            world["worker"].join(5)
        world["adapter"].disconnect()
        try:
            world["store"].close()
        except Exception:
            pass

    # ---- restoring the simulator between scenarios ---------------------------------------

    def restore(self, world: dict[str, Any]) -> None:
        """Put the simulator back as the next scenario expects it: on the ground at
        home, healthy, commissioned. A vehicle away from home or after a sensor
        fault is reset by rebooting SITL (it restarts at --home with its defaults)."""
        if world:
            self.close_sentinel(world)
        self.link.reset_faults()
        if getattr(self.pilot.link.mav.signing, "sign_outgoing", False):
            self.pilot.setup_signing(None)
        for name in ("SIM_WIND_SPD", "SIM_WIND_TURB"):
            if name in self.changed:  # a copter on the ground in strong gusts may never detect its landing
                self.pilot.set_param(name, self.changed[name])
        self.land_and_disarm()
        # Parameters first: SITL keeps SIM_* values across a reboot (found when a 10.3 V battery survived one).
        for name, value in list(self.changed.items()):
            self.pilot.set_param(name, value)
        state = self.vehicle()
        away = bool(state.get("pos")) and distance_m(state["pos"][:2], self.home) > 3
        sensor_fault = any(name.startswith("SIM_") and name != "SIM_SPEEDUP" for name in self.changed)
        if away or sensor_fault or self.needs_reboot:
            self.reboot_sitl()  # back at --home, and latched failsafes (battery, EKF) cleared
        else:
            self.pilot.set_home(self.home[0], self.home[1], HOME_AMSL_M)
            self.pilot.swap_battery()
            self.commission()
        # A disarmed vehicle can still be in RTL/LAND after the previous fault.
        # Leave a known non-failsafe mode so the next case can detect a new one.
        if self.pilot.set_mode("STABILIZE") != 0:
            raise RuntimeError("restoration could not reset the ground flight mode")
        self.changed.clear()
        self.needs_reboot = False

    def reboot_sitl(self, timeout: float = 120) -> None:
        self.pilot.reboot()
        deadline = time.time() + timeout
        time.sleep(5)
        while time.time() < deadline:  # back at home, with a position
            state = self.vehicle()
            if state.get("pos") and abs(state["pos"][0]) > 1 and distance_m(state["pos"][:2], self.home) < 3:
                break
            time.sleep(1)
        else:
            raise RuntimeError("SITL did not come back at home after a reboot")
        self.commission()

    def vehicle(self, timeout: float = 3.0) -> dict[str, Any]:
        """Position and state as the pilot's ground station sees it."""
        state = {}
        deadline = time.time() + timeout
        while time.time() < deadline and len(state) < 2:
            message = self.pilot.link.recv_match(type=["GLOBAL_POSITION_INT", "HEARTBEAT"], blocking=True, timeout=0.5)
            if message is None:
                continue
            if message.get_type() == "GLOBAL_POSITION_INT":
                state["pos"] = (message.lat / 1e7, message.lon / 1e7, message.relative_alt / 1000)
            elif message.get_srcComponent() == 1 and message.type not in (6, 18, 26):
                state["armed"] = bool(message.base_mode & 128)
        return state

    def land_and_disarm(self, timeout: float = 120) -> None:
        state = self.vehicle()
        if "armed" not in state:
            raise RuntimeError("restoration has no current aircraft state")
        if not state.get("armed"):
            return
        if state.get("pos", (0, 0, 0))[2] > 0.5:
            self.pilot.set_mode("LAND")
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.vehicle()
            if state.get("armed") is False:
                return
            time.sleep(1)
        raise TimeoutError("restoration could not confirm landing and disarming")


# ---- helpers for scenarios ----------------------------------------------------------------


def ready(ctx, timeout: float = 40) -> bool:
    manager = ctx.world["manager"]
    lab = ctx.world["lab"]
    ctx.wait_until(lambda s: s["link_ok"], 30, "Hub hears the aircraft")
    lab.pilot.download_parameters()  # the pilot's ground station on connect: Sentinel overhears it on the shared radio
    lab.pilot.download_fence()
    lab.pilot.request_message(242)  # HOME_POSITION, as QGroundControl asks for it
    if not ctx.wait_until(lambda s: manager.readiness()["ready"], timeout, "ready for approval"):
        raise RuntimeError(f"precondition failed: aircraft not ready: {manager.readiness()['blockers']}")
    return True


def fly_inspection(ctx, *, target=SHED, label="Shed", incident_id="inc_faults"):
    manager, lab = ctx.world["manager"], ctx.world["lab"]
    request = manager.request_inspection(requested_by="sentinel", map_point=target, label=label, source="incident", incident_id=incident_id,
                                         reason="Person entered Shed")
    manager.approve(request.request_id, "operator")
    _, plan = manager.mission_export(request.request_id)
    lab.pilot.load_mission(json.loads(plan))
    lab.pilot.start_mission()
    ctx.note("pilot started the approved mission", request=request.request_id)
    return request


def landed(ctx, timeout: float = 180) -> bool:
    manager = ctx.world["manager"]
    return ctx.wait_until(lambda s: s["flight_state"] == "on_ground" and manager._flight is None, timeout, "landed and disarmed")


def health_ids(ctx) -> set[str]:
    return {c["id"] for c in ctx.world["health"].conditions}


def ever_health(ctx, condition: str) -> bool:
    return ctx.seen("health", predicate=lambda value: condition in value)


def statustexts(ctx) -> list[str]:
    return list(ctx.world["adapter"].telemetry().messages)


def lab_fence(ctx) -> Geofence:
    return ctx.world["lab"].fence


def guided_flight(ctx, altitude: float = 12) -> None:
    lab = ctx.world["lab"]
    lab.pilot.guided_takeoff(altitude)
    adapter = ctx.world["adapter"]
    ctx.wait_until(lambda s: adapter.telemetry().alt_m >= altitude - 1, 60, f"climbed to {altitude:g} m")
    ctx.run_for(3)  # ArduCopter ignores GUIDED position targets until its take-off has finished


def fly_to(ctx, lat: float, lon: float, altitude: float) -> None:
    """The pilot clicks a point on the map: the target is sent a few times, as ground stations do."""
    for _ in range(3):
        ctx.world["lab"].pilot.goto(lat, lon, altitude)
        ctx.run_for(1)


# ---- GPS and navigation --------------------------------------------------------------------


def gps_none_before_flight(ctx):
    manager, lab = ctx.world["manager"], ctx.world["lab"]
    ctx.expect("ready before the fault", ready(ctx))
    lab.set("SIM_GPS1_ENABLE", 0)
    blocked = ctx.wait_until(lambda s: any(b.startswith(("no_3d_gps", "gps")) for b in manager.readiness()["blockers"]), 40, "GPS blocks readiness")
    ctx.expect("readiness blocks approval without GPS", blocked, str(manager.readiness()["blockers"]))
    ctx.expect("health reports the GPS problem", bool(health_ids(ctx) & {"gps_no_fix", "gps_stale"}), str(sorted(health_ids(ctx))))
    request = manager.request_inspection(requested_by="op", map_point=SHED, label="Shed")
    try:
        manager.approve(request.request_id, "operator")
        refused = ""
    except FlightCommandRejected as error:
        refused = str(error)
    ctx.expect("approval refused", "gps" in refused, refused)
    arm = lab.pilot.set_mode("LOITER"), lab.pilot.command(400, 1)
    ctx.observed["pilot arming without GPS (MAV_RESULT)"] = arm[1]
    ctx.observed["flight controller messages"] = [m for m in statustexts(ctx) if "PreArm" in m or "GPS" in m][-3:]
    ctx.expect("flight controller refuses to arm in LOITER without GPS", arm[1] != 0, f"result {arm[1]}")
    lab.pilot.set_param("SIM_GPS1_ENABLE", 1)
    ctx.recovery = "GPS re-enabled; " + ("readiness passed again" if ctx.wait_until(lambda s: manager.readiness()["ready"], 90, "ready again") else "readiness did NOT recover")
    ctx.expect("recovers when GPS returns", manager.readiness()["ready"], str(manager.readiness()["blockers"]))


def gps_low_satellites(ctx):
    manager, lab = ctx.world["manager"], ctx.world["lab"]
    ctx.expect("ready before the fault", ready(ctx))
    lab.set("SIM_GPS1_NUMSATS", 4)
    ctx.expect("readiness blocks on poor accuracy",
               ctx.wait_until(lambda s: "gps_poor_accuracy" in manager.readiness()["blockers"] or "no_3d_gps_fix" in manager.readiness()["blockers"], 30, "GPS accuracy blocks"),
               str(manager.readiness()["blockers"]))
    ctx.expect("health reports degraded GPS", bool(health_ids(ctx) & {"gps_degraded", "gps_no_fix"}), str(sorted(health_ids(ctx))))
    telemetry = ctx.world["adapter"].telemetry()
    ctx.observed["satellites, fix, hdop"] = (telemetry.satellites, telemetry.gps_fix, telemetry.hdop)
    lab.pilot.set_param("SIM_GPS1_NUMSATS", 10)
    ctx.expect("recovers", ctx.wait_until(lambda s: manager.readiness()["ready"], 90, "ready again"))
    ctx.recovery = "satellite count restored; readiness passed again"


def gps_loss_in_flight(ctx):
    manager, lab, health = ctx.world["manager"], ctx.world["lab"], ctx.world["health"]
    ctx.expect("ready", ready(ctx))
    request = fly_inspection(ctx)
    ctx.wait_until(lambda s: request.phase == "enroute", 90, "en route")
    lab.set("SIM_GPS1_ENABLE", 0)
    ctx.note("GPS disabled in flight")
    critical = ctx.wait_until(lambda s: bool(health_ids(ctx) & {"gps_no_fix", "gps_stale", "ekf_unhealthy", "position_stale"}), 30, "Sentinel sees the GPS loss")
    ctx.expect("Sentinel reports the GPS loss as critical", critical and health.worst() == "critical", str(health.conditions)[:300])
    possible, why = health.return_home_possible(ctx.world["adapter"].telemetry(), int(time.time() * 1000))
    ctx.expect("Sentinel does not claim return-to-home is possible", not possible, why)
    ctx.wait_until(lambda s: s["mode"] in ("LAND", "ALT_HOLD") or not s["armed"], 60, "flight controller failsafe")
    ctx.observed["flight controller mode after GPS loss"] = ctx.world["adapter"].telemetry().flight_mode
    ctx.observed["flight controller messages"] = statustexts(ctx)[-5:]
    landed(ctx)
    ctx.expect("the inspection is not reported as completed", request.status in ("aborted", "interrupted"), request.status + ": " + request.note)
    ctx.expect("no 'returning home' advisory while navigation had failed",
               not any("returning home" in a["text"] for a in manager.advisories), str(manager.advisories))
    lab.pilot.set_param("SIM_GPS1_ENABLE", 1)
    ctx.recovery = "landed in place; GPS re-enabled; the pilot carries the aircraft home (scripted re-flight)"


def gps_glitch_in_flight(ctx):
    manager, lab, health = ctx.world["manager"], ctx.world["lab"], ctx.world["health"]
    ctx.expect("ready", ready(ctx))
    guided_flight(ctx, 12)
    target = map_point_to_geo(ORIGIN, 20, 14)
    fly_to(ctx, target[0], target[1], 12)
    ctx.run_for(8)
    before = ctx.world["adapter"].telemetry()
    lab.set("SIM_GPS1_GLTCH_X", 0.0004)  # about 44 m north
    ctx.note("GPS glitch injected")
    ctx.run_for(20)
    after = ctx.world["adapter"].telemetry()
    messages = statustexts(ctx)
    glitch_reported = any("glitch" in m.lower() or "gps" in m.lower() for m in messages)
    ctx.observed["position jumps seen by Sentinel"] = after.position_jumps - before.position_jumps
    ctx.observed["flight controller messages"] = messages[-5:]
    ctx.observed["mode"] = after.flight_mode
    moved = distance_m((before.lat, before.lon), (after.lat, after.lon)) if before.lat and after.lat else None
    ctx.observed["reported position moved (m)"] = round(moved, 1) if moved is not None else None
    flagged = ever_health(ctx, "position_jump") or ever_health(ctx, "ekf_unhealthy") or ever_health(ctx, "gps_no_fix")
    ctx.expect("a glitch the flight controller reports is never silent in Sentinel", flagged or not glitch_reported,
               f"FC messages {messages[-3:]}, health ever flagged: {flagged}")
    ctx.expect("a large jump in the reported position is flagged", (moved or 0) < 30 or flagged, f"moved {moved} m, flagged {flagged}")
    lab.pilot.set_param("SIM_GPS1_GLTCH_X", 0)
    lab.changed.pop("SIM_GPS1_GLTCH_X", None)
    ctx.run_for(10)
    ctx.recovery = "glitch removed; landed by the pilot"


def gps_reports_missing(ctx):
    manager, lab = ctx.world["manager"], ctx.world["lab"]
    ctx.expect("ready", ready(ctx))
    lab.link.drop_ids = {24}  # GPS_RAW_INT never reaches the Hub
    stale = ctx.wait_until(lambda s: "gps_stale" in s["health"], 20, "GPS reports flagged as stale")
    ctx.expect("health flags the missing GPS reports", stale, str(sorted(health_ids(ctx))))
    ctx.expect("readiness does not pass GPS on a stale report", "gps" in " ".join(manager.readiness()["blockers"]),
               f"blockers {manager.readiness()['blockers']}, GPS check: {next(c for c in manager.readiness()['checks'] if c['id'] == 'gps')}")
    lab.link.drop_ids = set()
    ctx.expect("recovers", ctx.wait_until(lambda s: manager.readiness()["ready"] and "gps_stale" not in s["health"], 30, "ready again"))
    ctx.recovery = "GPS reports flow again"


def telemetry_delayed(ctx):
    lab = ctx.world["lab"]
    ctx.expect("ready", ready(ctx))
    lab.set("SIM_SPEEDUP", 1)  # the boot clock must run at wall-clock speed for a latency measurement
    ctx.run_for(5)
    lab.link.delay_s = 2.5
    delayed = ctx.wait_until(lambda s: "link_delayed" in s["health"], 20, "added delay detected")
    telemetry = ctx.world["adapter"].telemetry()
    ctx.observed["estimated added delay (ms)"] = telemetry.link_delay_ms
    ctx.expect("a 2.5 s radio delay is reported", delayed and 2000 <= (telemetry.link_delay_ms or 0) <= 3500, str(telemetry.link_delay_ms))
    lab.link.delay_s = 0
    ctx.expect("recovers", ctx.wait_until(lambda s: "link_delayed" not in s["health"], 20, "delay cleared"))
    ctx.recovery = "delay removed; speed-up restored"


def home_position_wrong(ctx):
    manager, lab = ctx.world["manager"], ctx.world["lab"]
    ctx.expect("ready", ready(ctx))
    inside = map_point_to_geo(ORIGIN, 34, 24)
    lab.pilot.set_home(inside[0], inside[1], HOME_AMSL_M)
    ctx.expect("a home far from the configured one is reported", ctx.wait_until(lambda s: "home_mismatch" in s["health"], 20, "home mismatch"),
               str(sorted(health_ids(ctx))))
    outside = map_point_to_geo(ORIGIN, 70, 4)
    lab.pilot.set_home(outside[0], outside[1], HOME_AMSL_M)
    blocked = ctx.wait_until(lambda s: "home_outside_geofence" in manager.readiness()["blockers"], 20, "home outside the fence blocks")
    ctx.expect("a home outside the geofence blocks approval", blocked, str(manager.readiness()["blockers"]))
    lab.pilot.set_home(lab.home[0], lab.home[1], HOME_AMSL_M)
    ctx.expect("recovers", ctx.wait_until(lambda s: manager.readiness()["ready"] and "home_mismatch" not in s["health"], 20, "home restored"))
    ctx.recovery = "home set back by the pilot"


# ---- geofence --------------------------------------------------------------------------------


def route_crosses_boundary(ctx):
    manager = ctx.world["manager"]
    ctx.expect("ready", ready(ctx))
    try:
        manager.request_inspection(requested_by="op", map_point=(60, 3), label="Outside")
        refused = ""
    except FlightCommandRejected as error:
        refused = str(error)
    ctx.expect("a target outside the geofence is refused", "outside_geofence" in refused, refused)
    try:
        excluded = Geofence(lab_fence(ctx).polygon, 25, 5, exclusions=[[list(map_point_to_geo(ORIGIN, x, y)) for x, y in [[18, 0], [22, 0], [22, 28], [18, 28]]]])
        crossing = plan_route(ctx.world["lab"].home, map_point_to_geo(ORIGIN, *SHED), excluded, safe_transit_altitude_m=12, inspection_altitude_m=12, hover_s=4)
        problems = crossing.problems
    except FlightCommandRejected as error:
        problems = [str(error)]
    ctx.expect("a route through an exclusion zone is rejected", "route_inside_exclusion_0" in problems, str(problems))
    normal = manager.plan(manager.request_inspection(requested_by="op", map_point=(20, 14), label="Middle"))
    ctx.expect("a normal route inside the fence has no problems", normal.problems == [], str(normal.problems))
    ctx.recovery = "nothing flew"


def fence_guided_target_outside(ctx):
    """ArduCopter refuses a GUIDED destination outside its fence: the aircraft stays inside, and Sentinel must not cry wolf."""
    lab = ctx.world["lab"]
    ctx.expect("ready", ready(ctx))
    guided_flight(ctx, 12)
    outside = map_point_to_geo(ORIGIN, 60, 14)
    fly_to(ctx, outside[0], outside[1], 12)
    ctx.note("pilot clicks a point 20 m outside the fence")
    state = ctx.run_for(15)
    telemetry = ctx.world["adapter"].telemetry()
    east, _ = geo_to_map_point(ORIGIN, telemetry.lat, telemetry.lon)
    ctx.observed["position east (m), fence edge at 40"] = round(east, 1)
    ctx.observed["flight controller messages"] = statustexts(ctx)[-3:]
    ctx.expect("the flight controller keeps the aircraft inside its fence", east < 40, f"east {east:.1f} m")
    ctx.expect("no breach is reported when there is none", not ctx.seen("safety_state", "geofence_breach"), str(state["safety_state"]))
    lab.pilot.set_mode("RTL")
    landed(ctx)
    ctx.recovery = "returned by RTL"


def map_east(ctx) -> float:
    telemetry = ctx.world["adapter"].telemetry()
    return geo_to_map_point(ORIGIN, telemetry.lat, telemetry.lon)[0] if telemetry.lat is not None else 0.0


def _push_out(ctx, *, pitch: int = 1500, throttle: int = 1500, seconds: float = 40) -> bool:
    """The pilot holds the sticks in LOITER until the flight controller takes over (or time runs out)."""
    lab = ctx.world["lab"]
    lab.pilot.set_mode("LOITER")
    deadline = time.time() + seconds
    acted = False
    while time.time() < deadline and not acted:
        lab.pilot.sticks(pitch=pitch, throttle=throttle, seconds=0.5)
        state = ctx.sample()
        acted = state["mode"] in ("RTL", "LAND", "BRAKE", "SMART_RTL")
    lab.pilot.sticks(seconds=0.1)  # release the sticks to the radio
    ctx.note("flight controller took over" if acted else "flight controller did not take over")
    return acted


def fence_breach_polygon(ctx):
    manager, lab = ctx.world["manager"], ctx.world["lab"]
    ctx.expect("ready", ready(ctx))
    lab.set("AVOID_ENABLE", 0)  # with avoidance on, ArduCopter stops a pilot at the fence: switch it off to cause a real breach
    guided_flight(ctx, 12)
    edge = map_point_to_geo(ORIGIN, 36, 14)
    fly_to(ctx, edge[0], edge[1], 12)
    ctx.wait_until(lambda s: map_east(ctx) > 33, 30, "near the east edge, facing east")
    ctx.note("pilot pushes forward across the east edge (LOITER, avoidance off)")
    returned = _push_out(ctx, pitch=1250)
    ctx.observed["fence action"] = ctx.world["adapter"].telemetry().flight_mode
    ctx.observed["flight controller messages"] = statustexts(ctx)[-4:]
    ctx.expect("the flight controller acts on the breach", returned, ctx.observed["fence action"])
    ctx.expect("Sentinel reports the breach", ctx.seen("safety_state", "geofence_breach") or ever_health(ctx, "fence_breach"),
               f"safety states: {[e['value'] for e in ctx.timeline if e.get('field') == 'safety_state']}")
    ctx.expect("Sentinel never commands the pilot-flown aircraft", "drone_auto_return_home" not in [r["action"] for r in ctx.world["store"].list_audit(500)])
    landed(ctx)
    ctx.expect("ready again after landing", ctx.wait_until(lambda s: s["safety_state"] == "ok", 30, "safety state clear"))
    ctx.recovery = "the flight controller returned and landed; Sentinel's safety state cleared on the ground"


def fence_breach_altitude(ctx):
    manager, lab = ctx.world["manager"], ctx.world["lab"]
    ctx.expect("ready", ready(ctx))
    lab.set("AVOID_ENABLE", 0)  # avoidance would stop the climb at the ceiling
    guided_flight(ctx, 12)
    ctx.note("pilot holds full throttle in LOITER towards the 25 m ceiling (avoidance off)")
    acted = _push_out(ctx, throttle=1900)
    ctx.observed["fence action"] = ctx.world["adapter"].telemetry().flight_mode
    ctx.observed["highest altitude band (m)"] = max((e["value"] for e in ctx.timeline if e.get("field") == "alt_band_m"), default=None)
    ctx.expect("the flight controller acts on the altitude breach", acted, ctx.observed["fence action"])
    ctx.expect("Sentinel reports the altitude breach", ctx.seen("safety_state", "geofence_breach"),
               f"safety states: {[e['value'] for e in ctx.timeline if e.get('field') == 'safety_state']}")
    landed(ctx)
    ctx.recovery = "returned and landed by the flight controller"


def fence_disabled_on_fc(ctx):
    manager, lab = ctx.world["manager"], ctx.world["lab"]
    ctx.expect("ready", ready(ctx))
    lab.set("FENCE_ENABLE", 0)
    blocked = ctx.wait_until(lambda s: "flight_controller_settings" in manager.readiness()["blockers"], 20, "audit blocks")
    finding = next((f for f in (manager.parameter_audit() or {}).get("blocking", []) if f["param"] == "FENCE_ENABLE"), None)
    ctx.expect("the audit blocks approval with FENCE_ENABLE named", blocked and finding is not None, str(finding))
    lab.pilot.set_param("FENCE_ENABLE", 1)
    ctx.expect("recovers", ctx.wait_until(lambda s: manager.readiness()["ready"], 20, "audit passes"))
    ctx.recovery = "FENCE_ENABLE restored by the pilot"


def fence_polygon_missing(ctx):
    manager, lab = ctx.world["manager"], ctx.world["lab"]
    ctx.expect("ready", ready(ctx))
    lab.pilot.clear_fence()
    try:
        lab.pilot.download_fence()
    except TimeoutError:
        pass
    blocked = ctx.wait_until(lambda s: "flight_controller_settings" in manager.readiness()["blockers"], 30, "audit blocks")
    finding = next((f for f in (manager.parameter_audit() or {}).get("blocking", []) if f["param"] == "fence polygon"), None)
    ctx.expect("a missing polygon blocks approval", blocked and finding is not None and finding["actual"] == "none loaded", str(finding))
    lab.commission()
    lab.pilot.download_fence()
    ctx.expect("recovers", ctx.wait_until(lambda s: manager.readiness()["ready"], 30, "fence restored"))
    ctx.recovery = "fence re-loaded by the commissioning engineer"


def fence_polygon_mismatch(ctx):
    manager, lab = ctx.world["manager"], ctx.world["lab"]
    ctx.expect("ready", ready(ctx))
    loose = Geofence([list(map_point_to_geo(ORIGIN, x, y)) for x, y in [[0, 0], [70, 0], [70, 28], [0, 28]]], 25, 5)
    route = plan_route(lab.home, map_point_to_geo(ORIGIN, *SHED), loose, safe_transit_altitude_m=12, inspection_altitude_m=12, hover_s=4)
    lab.pilot.load_fence(to_qgc_plan(route, loose))
    lab.pilot.download_fence()
    blocked = ctx.wait_until(lambda s: "flight_controller_settings" in manager.readiness()["blockers"], 30, "audit blocks")
    finding = next((f for f in (manager.parameter_audit() or {}).get("blocking", []) if f["param"] == "fence polygon"), None)
    ctx.expect("a flight-controller fence larger than Sentinel's blocks approval", blocked and finding is not None and "outside" in finding["actual"], str(finding))
    lab.commission()
    lab.pilot.download_fence()
    ctx.expect("recovers", ctx.wait_until(lambda s: manager.readiness()["ready"], 30, "fence restored"))
    ctx.recovery = "the correct fence re-loaded"


def fence_breach_without_link(ctx):
    manager, lab = ctx.world["manager"], ctx.world["lab"]
    ctx.expect("ready", ready(ctx))
    guided_flight(ctx, 12)
    outside = map_point_to_geo(ORIGIN, 60, 14)
    fly_to(ctx, outside[0], outside[1], 12)
    time.sleep(1.0)
    lab.link.set_hub_link(False)
    lab.link.set_gcs_link(False)
    ctx.note("radio lost while flying towards the boundary")
    ctx.run_for(20)
    lab.link.set_hub_link(True)
    lab.link.set_gcs_link(True)
    ctx.expect("Sentinel reported the lost link", ctx.seen("flight_state", "link_lost"))
    ctx.wait_until(lambda s: s["link_ok"], 20, "link back")
    telemetry = ctx.world["adapter"].telemetry()
    ctx.observed["mode after reconnect"] = telemetry.flight_mode
    ctx.observed["position (map m) after reconnect"] = tuple(round(v, 1) for v in geo_to_map_point(ORIGIN, telemetry.lat, telemetry.lon)) if telemetry.lat else None
    ctx.expect("the flight controller handled it without the link (returning, landing or landed)",
               telemetry.flight_mode in ("RTL", "LAND", "SMART_RTL") or not telemetry.in_air, telemetry.flight_mode)
    landed(ctx)
    flights = ctx.world["store"].list_drone_flights()
    ctx.expect("the flight log is closed", bool(flights) and flights[0].get("ended_ms"), str(flights[:1])[:200])
    ctx.recovery = "link restored; the aircraft had returned on the flight controller's failsafes"


# ---- battery and power --------------------------------------------------------------------------


def battery_low_before_launch(ctx):
    manager, lab = ctx.world["manager"], ctx.world["lab"]
    ctx.expect("ready", ready(ctx))
    lab.pilot.command(42651, 255, 40)  # the pack reads 40 %
    blocked = ctx.wait_until(lambda s: "battery_below_launch_minimum" in manager.readiness()["blockers"], 20, "battery blocks")
    ctx.expect("a 40 % pack blocks approval (launch minimum 60 %)", blocked, str(manager.readiness()["blockers"]))
    lab.pilot.swap_battery()
    ctx.expect("a charged pack clears it", ctx.wait_until(lambda s: manager.readiness()["ready"], 20, "ready"))
    ctx.recovery = "battery swapped"


def battery_insufficient_for_route(ctx):
    manager, lab = ctx.world["manager"], ctx.world["lab"]
    ctx.expect("ready", ready(ctx))
    lab.pilot.command(42651, 255, 61)
    ctx.wait_until(lambda s: (ctx.world["adapter"].telemetry().battery_pct or 100) <= 61, 15, "61 %")
    manager.safety.rth_battery_pct = 57  # a site with a high reserve: 4 % of a 15 min estimate is about 36 s of flying
    request = manager.request_inspection(requested_by="op", map_point=SHED, label="Shed")
    try:
        manager.approve(request.request_id, "operator")
        refused = ""
    except FlightCommandRejected as error:
        refused = str(error)
    ctx.expect("the endurance check refuses a route the battery cannot fly", "route_exceeds_endurance" in refused, refused)
    lab.pilot.swap_battery()
    ctx.recovery = "battery swapped; nothing flew"


def battery_drain_and_estimate(ctx):
    manager, lab, health = ctx.world["manager"], ctx.world["lab"], ctx.world["health"]
    ctx.expect("ready", ready(ctx))
    guided_flight(ctx, 12)
    point = map_point_to_geo(ORIGIN, 20, 14)
    fly_to(ctx, point[0], point[1], 12)
    ctx.run_for(45)
    baseline = health.drain()
    ctx.observed["SITL drain (%/min), normal hover"] = baseline["observed_pct_per_min"]
    ctx.observed["Lite estimate (%/min)"] = baseline["expected_pct_per_min"]
    lab.set("BATT_CAPACITY", 1100)  # the pack holds a third of what was declared: three times the drain from here on
    ctx.note("battery capacity cut to a third")
    flagged = ctx.wait_until(lambda s: "battery_fast_drain" in s["health"], 120, "fast drain flagged")
    ctx.observed["drain after the fault (%/min)"] = health.drain()["observed_pct_per_min"]
    ctx.expect("Sentinel flags a drain far above the estimate", flagged, str(health.drain()))
    lab.pilot.set_mode("RTL")
    landed(ctx)
    ctx.recovery = "the pilot returned home; battery swapped"


def battery_telemetry_frozen(ctx):
    manager, lab, health = ctx.world["manager"], ctx.world["lab"], ctx.world["health"]
    ctx.expect("ready", ready(ctx))
    guided_flight(ctx, 12)
    lab.link.drop_ids = {1, 147}  # SYS_STATUS and BATTERY_STATUS stop reaching the Hub
    ctx.note("battery reports stopped")
    stale = ctx.wait_until(lambda s: "battery_stale" in s["health"], 25, "battery reports stale")
    ctx.expect("a frozen battery reading is reported as stale, critical in flight", stale and health.worst() == "critical", str(health.conditions)[:300])
    ctx.expect("the readiness battery check does not pass a stale reading", "battery" in " ".join(manager.readiness()["blockers"]),
               str(next(c for c in manager.readiness()["checks"] if c["id"] == "battery")))
    lab.link.drop_ids = set()
    ctx.wait_until(lambda s: "battery_stale" not in s["health"], 20, "battery reports back")
    lab.pilot.set_mode("LAND")
    landed(ctx)
    ctx.recovery = "battery reports restored; landed"


def low_voltage_failsafe(ctx):
    manager, lab, health = ctx.world["manager"], ctx.world["lab"], ctx.world["health"]
    ctx.expect("ready", ready(ctx))
    request = fly_inspection(ctx)
    ctx.wait_until(lambda s: request.phase == "enroute", 90, "en route")
    lab.set("SIM_BATT_VOLTAGE", 10.3)  # below BATT_LOW_VOLT 10.5 while the capacity count still shows a full pack
    acted = ctx.wait_until(lambda s: s["mode"] in ("RTL", "LAND"), 60, "flight controller low-battery failsafe")
    telemetry = ctx.world["adapter"].telemetry()
    ctx.observed["mode"], ctx.observed["battery % shown"], ctx.observed["voltage"] = telemetry.flight_mode, telemetry.battery_pct, telemetry.battery_voltage
    ctx.observed["flight controller battery state"] = telemetry.battery_state
    ctx.expect("the flight controller's voltage failsafe acts", acted, telemetry.flight_mode)
    ctx.expect("Sentinel shows the flight controller's battery verdict, not just the %", ever_health(ctx, "battery_failsafe"), str(sorted(health_ids(ctx))))
    ctx.expect("the operator is told why the aircraft returns", any(a["reason"] in ("low_battery", "battery_failsafe") for a in manager.advisories),
               str(manager.advisories))
    landed(ctx)
    ctx.expect("the inspection ends as not completed", request.status == "aborted", f"{request.status}: {request.note}")
    ctx.recovery = "voltage restored; battery swapped"


def return_interrupted_by_battery(ctx):
    manager, lab = ctx.world["manager"], ctx.world["lab"]
    ctx.expect("ready", ready(ctx))
    request = fly_inspection(ctx)
    ctx.wait_until(lambda s: request.phase in ("inspecting", "returning"), 120, "at the inspection point")
    lab.set("SIM_BATT_VOLTAGE", 10.3)
    ctx.wait_until(lambda s: s["mode"] == "RTL", 40, "low-battery return")
    lab.set("SIM_BATT_VOLTAGE", 9.6)  # below BATT_CRT_VOLT 10.0 on the way home
    ctx.note("battery critical during the return")
    landing = ctx.wait_until(lambda s: s["mode"] == "LAND", 40, "critical-battery landing")
    ctx.expect("the flight controller lands where it is on a critical battery", landing, ctx.world["adapter"].telemetry().flight_mode)
    landed(ctx)
    telemetry = ctx.world["adapter"].telemetry()
    gap = distance_m((telemetry.lat, telemetry.lon), lab.home) if telemetry.lat else None
    ctx.observed["landed this far from home (m)"] = round(gap, 1) if gap is not None else None
    ctx.expect("the inspection is aborted, not completed", request.status == "aborted", request.status)
    flights = ctx.world["store"].list_drone_flights()
    ctx.expect("the flight log records the safety events", bool(flights) and bool(flights[0].get("safety_events")), str(flights[:1])[:300])
    ctx.recovery = "landed away from home; voltage restored; the pilot carries it back (scripted re-flight)"


# ---- communication and control -------------------------------------------------------------------


def radio_lost_in_flight(ctx):
    manager, lab = ctx.world["manager"], ctx.world["lab"]
    ctx.expect("ready", ready(ctx))
    request = fly_inspection(ctx)
    ctx.wait_until(lambda s: request.phase == "enroute", 90, "en route")
    lab.link.set_hub_link(False)
    lab.link.set_gcs_link(False)
    ctx.expect("link loss detected", ctx.wait_until(lambda s: s["flight_state"] == "link_lost", 15, "link lost"))
    time.sleep(4)
    lab.link.set_hub_link(True)
    lab.link.set_gcs_link(True)
    ctx.expect("the flight controller's GCS failsafe returns it", ctx.wait_until(lambda s: s["mode"] in ("RTL", "LAND") or not s["armed"], 30, "failsafe"))
    landed(ctx)
    ctx.expect("the inspection is aborted after reconnection", request.status == "aborted", request.status)
    ctx.expect("ready again after landing and a battery swap", (lab.pilot.swap_battery() or True) and ctx.wait_until(lambda s: manager.readiness()["ready"], 60, "ready"),
               str(manager.readiness()["blockers"]))
    ctx.recovery = "link restored; consistent final state (aborted), ready again"


def link_intermittent(ctx):
    lab = ctx.world["lab"]
    ctx.expect("ready", ready(ctx))
    for _ in range(4):
        lab.link.set_hub_link(False)
        ctx.run_for(4)
        lab.link.set_hub_link(True)
        ctx.run_for(3)
    ctx.expect("repeated drops are reported as an unstable link", ever_health(ctx, "link_unstable"), str(sorted(health_ids(ctx))))
    ctx.expect("the flight controller did not fail safe (the pilot's link was fine)", ctx.world["adapter"].telemetry().flight_mode not in ("RTL", "LAND"))
    ctx.recovery = "link stable again"


def packet_loss(ctx):
    lab = ctx.world["lab"]
    ctx.expect("ready", ready(ctx))
    before = ctx.world["adapter"].messages_received
    lab.link.loss = 0.3
    state = ctx.run_for(20)
    received = ctx.world["adapter"].messages_received - before
    ctx.observed["messages in 20 s at 30 % loss"] = received
    ctx.observed["frames lost"] = lab.link.counts["lost"]
    ctx.expect("telemetry keeps flowing at 30 % loss", received > 50 and state["link_ok"], f"{received} messages")
    ctx.expect("no false critical condition on the ground", ctx.world["health"].worst() != "critical", str(ctx.world["health"].conditions)[:200])
    lab.link.loss = 0
    ctx.recovery = "loss removed"


def duplicate_messages(ctx):
    lab, adapter = ctx.world["lab"], ctx.world["adapter"]
    ctx.expect("ready", ready(ctx))
    before = adapter.telemetry()
    lab.link.duplicate = True
    ctx.run_for(15)
    after = adapter.telemetry()
    ctx.observed["duplicated frames"] = lab.link.counts["duplicated"]
    ctx.expect("duplicates cause no false restart, jump or state change",
               after.fc_reboots == before.fc_reboots and after.position_jumps == before.position_jumps and ctx.world["manager"].readiness()["ready"],
               f"reboots {after.fc_reboots}, jumps {after.position_jumps}")
    lab.link.duplicate = False
    ctx.recovery = "duplication off"


def hub_restart_in_flight(ctx):
    lab = ctx.world["lab"]
    ctx.expect("ready", ready(ctx))
    request = fly_inspection(ctx)
    ctx.wait_until(lambda s: request.phase == "enroute", 90, "en route")
    old = ctx.world
    old["adapter"].disconnect()
    ctx.note("Sentinel stops (crash) mid-flight")
    time.sleep(2)
    from sentinel.drone.manager import DroneManager
    from sentinel.drone.mavlink import MavlinkTelemetryAdapter

    adapter = MavlinkTelemetryAdapter(f"udpin:127.0.0.1:{lab.hub_port}")
    adapter.connect()
    manager = DroneManager(adapter, lab.fence, ORIGIN, old["manager"].safety, records=old["store"], drone_id="lite", home=lab.home,
                           audit=old["store"].audit, model=lab.model, weather=old["manager"].weather)
    manager.video_status = old["manager"].video_status
    ctx.world.update({"adapter": adapter, "manager": manager})
    ctx.note("Sentinel restarted")
    recovered = manager.requests.get(request.request_id)
    ctx.expect("the inspection is marked interrupted, never resumed", recovered is not None and recovered.status == "interrupted",
               recovered.note if recovered else "missing")
    ctx.expect("telemetry resumes after the restart", ctx.wait_until(lambda s: s["link_ok"], 20, "telemetry"))
    landed(ctx)
    ctx.expect("the pilot's mission continued and landed without Sentinel", ctx.world["adapter"].telemetry().flight_mode in ("AUTO", "RTL", "LAND", "LOITER"))
    flights = old["store"].list_drone_flights()
    ctx.expect("the flight before the restart is closed as interrupted", any(f.get("interrupted") for f in flights), str(flights)[:300])
    ctx.recovery = "restarted Sentinel followed the rest of the flight; the request stays interrupted"


def pilot_gcs_lost(ctx):
    manager, lab = ctx.world["manager"], ctx.world["lab"]
    ctx.expect("ready", ready(ctx))
    request = fly_inspection(ctx)
    ctx.wait_until(lambda s: request.phase == "enroute", 90, "en route")
    lab.link.set_gcs_link(False)
    ctx.note("the pilot's ground station loses the link; the Hub still hears everything")
    acted = ctx.wait_until(lambda s: s["mode"] in ("RTL", "LAND"), 30, "GCS failsafe")
    ctx.expect("the flight controller's GCS failsafe acts on the pilot's link", acted, ctx.world["adapter"].telemetry().flight_mode)
    ctx.observed["flight controller messages"] = statustexts(ctx)[-4:]
    lab.link.set_gcs_link(True)
    landed(ctx)
    ctx.expect("the inspection ends aborted with the reason recorded", request.status == "aborted" and bool(request.note), f"{request.status}: {request.note}")
    flights = ctx.world["store"].list_drone_flights()
    ctx.expect("the failsafe message is in the flight log", any("ailsafe" in m for m in (flights[0].get("messages") or [])) if flights else False,
               str((flights[0].get("messages") if flights else [])[-5:]))
    ctx.recovery = "pilot link restored after landing"


def fc_restart_on_ground(ctx):
    manager, lab = ctx.world["manager"], ctx.world["lab"]
    ctx.expect("ready", ready(ctx))
    lab.pilot.reboot()
    ctx.note("flight controller restarted (SITL re-executes itself)")
    restarted = ctx.wait_until(lambda s: s["fc_reboots"] >= 1, 30, "confirmed flight-controller restart")
    ctx.wait_until(lambda s: s["link_ok"], 60, "telemetry back")
    ctx.expect("Sentinel notices the restart", restarted and ctx.world["adapter"].telemetry().fc_reboots >= 1,
               f"fc_reboots {ctx.world['adapter'].telemetry().fc_reboots}")
    ctx.expect("nothing is approved on settings from before the restart",
               ctx.wait_until(lambda s: "flight_controller_settings" in manager.readiness()["blockers"], 15, "settings re-read required"),
               str(manager.readiness()["blockers"]))
    time.sleep(8)
    lab.pilot.link.wait_heartbeat(timeout=30)
    lab.commission()  # SITL's restart wiped the fence (-w); a real FC keeps it
    ctx.expect("ready again once the ground station re-reads the settings", ready(ctx), str(manager.readiness()["blockers"]))
    ctx.recovery = "the pilot's ground station reconnected and downloaded the parameters; Sentinel overheard them"


def second_autopilot_on_link(ctx):
    import os as _os

    _os.environ.setdefault("MAVLINK20", "1")
    from pymavlink.dialects.v20 import ardupilotmega as mavlink2

    lab, adapter = ctx.world["lab"], ctx.world["adapter"]
    ctx.expect("ready", ready(ctx))
    before = adapter.telemetry()
    fake = mavlink2.MAVLink(None, srcSystem=7, srcComponent=1)
    for _ in range(5):
        lab.link.inject_to_hub(mavlink2.MAVLink_heartbeat_message(2, 3, 128 | 1, 4, 4, 3).pack(fake)
                               + mavlink2.MAVLink_global_position_int_message(0, 450100000, -750100000, 0, 90000, 0, 0, 0, 0).pack(fake))
        ctx.run_for(1)
    after = adapter.telemetry()
    ctx.expect("the other autopilot is reported", "7/1" in after.other_vehicles, str(after.other_vehicles))
    ctx.expect("its telemetry is never mixed in", not after.armed and abs((after.lat or 0) - (before.lat or 0)) < 1e-4 and after.alt_m < 2,
               f"armed {after.armed}, lat {after.lat}, alt {after.alt_m}")
    ctx.recovery = "nothing to restore"


def unauthorized_hub_commands(ctx):
    manager, lab = ctx.world["manager"], ctx.world["lab"]
    ctx.expect("ready", ready(ctx))
    adapter = ctx.world["adapter"]
    probe = adapter.probe_command_path(3.0)
    ctx.observed["probe"] = probe
    ctx.expect("the Hub's read-only probe gets no answer (lockout)", probe["state"] == "blocked", probe["detail"])
    ctx.expect("the pilot's ground station is answered (the uplink works)", lab.pilot.request_message(148) == 0)
    ctx.expect("an adapter that believes it may command is ignored by the flight controller", hasattr(adapter, "takeoff"))
    try:
        adapter.takeoff(10)
        outcome = "sent"
    except FlightCommandRejected as error:
        outcome = str(error)
    ctx.run_for(3)
    ctx.expect("the take-off had no effect", not adapter.telemetry().armed, outcome)
    ctx.recovery = "nothing changed on the aircraft"


def signing_lockout(ctx):
    from sentinel.drone import catalog as _catalog

    manager, lab, adapter = ctx.world["manager"], ctx.world["lab"], ctx.world["adapter"]
    ctx.expect("ready", ready(ctx))
    lab.set("MAV_OPTIONS", 0)
    ctx.run_for(3)
    open_probe = adapter.probe_command_path(3.0)
    ctx.observed["probe without any lockout"] = open_probe["state"]
    ctx.expect("negative control: without a lockout the Hub's request is answered", open_probe["state"] == "open", open_probe["detail"])
    lab.pilot.setup_signing(SIGNING_KEY)
    ctx.note("the pilot's ground station enabled MAVLink signing (the Hub has no key)")
    signed = ctx.wait_until(lambda s: adapter.telemetry().link_signed is True, 20, "signed frames seen")
    ctx.expect("Sentinel sees that the aircraft signs its link", signed)
    ctx.expect("telemetry still flows to the Hub without the key", ctx.wait_until(lambda s: s["link_ok"], 10, "telemetry"))
    blocked = adapter.probe_command_path(3.0)
    ctx.observed["probe with signing only"] = blocked["state"]
    ctx.expect("with signing alone, the Hub's unsigned request is ignored", blocked["state"] == "blocked", blocked["detail"])
    state, detail = _catalog.command_lockout(lab.model, adapter.parameters(), 245, None, signed=adapter.telemetry().link_signed)
    ctx.expect("the lockout check recognises signing", state == "pass" and "signs" in detail, detail)
    lab.pilot.setup_signing(None)
    lab.pilot.set_param("MAV_OPTIONS", 1)
    lab.changed.pop("MAV_OPTIONS", None)
    ctx.expect("signing removed again", ctx.wait_until(lambda s: adapter.telemetry().link_signed is False, 20, "unsigned frames"))
    ctx.recovery = "signing removed by the pilot; MAV_OPTIONS lockout restored"


# ---- video and camera --------------------------------------------------------------------------------

PERSON = {"source": {"type": "simulated_aerial", "objects": [
    {"label": "person", "path": [{"t": 0, "map": [34.6, 4.0]}, {"t": 20, "map": [35.4, 4.4]}, {"t": 40, "map": [34.6, 4.0]}]}]}}


def video_inspection(ctx, faults_when_on_station: list[VideoFault] | None = None):
    request = fly_inspection(ctx)
    ctx.wait_until(lambda s: request.phase == "inspecting", 120, "on station")
    if faults_when_on_station is not None:
        ctx.world["video_source"]["latest"].faults = faults_when_on_station
        ctx.world["video_source"]["latest"].started = time.monotonic()
        ctx.note("camera fault starts over the inspection point", faults=[f.kind for f in faults_when_on_station])
    ctx.wait_until(lambda s: request.status in ("completed", "aborted"), 180, "inspection over")
    landed(ctx)
    deadline = time.time() + 10
    while not request.observations.get("final") and time.time() < deadline:
        time.sleep(0.2)
    return request


def camera_unreachable_before_flight(ctx):
    manager, lab = ctx.world["manager"], ctx.world["lab"]
    from sentinel.drone.video import DroneVideoWorker

    ctx.expect("ready before disconnecting the camera", ready(ctx))

    def unreachable():
        raise ConnectionRefusedError("rtsp://192.168.144.25:8554/main.264: connection refused")

    worker = DroneVideoWorker(manager, {"source": {"type": "rtsp"}, "fps": 5, "check_every_s": 5}, config={}, media_root=Path(ctx.world["directory"]),
                              record_evidence=lambda *a: None, source_factory=unreachable)
    manager.video_status = worker.status
    worker.start()
    ctx.world["worker"] = worker
    ctx.wait_until(lambda s: s["video"] == "unreachable", 30, "camera unreachable")
    check = next(c for c in manager.readiness()["checks"] if c["id"] == "video")
    ctx.observed["video readiness check"] = check
    ctx.expect("an unreachable camera is reported before the flight", worker.state == "unreachable", worker.detail)
    ctx.expect("the Lite (camera required) is not approved with an unreachable camera", check["state"] == "fail", str(check))
    ctx.recovery = "nothing flew"


def camera_freezes_on_station(ctx):
    ctx.expect("ready", ready(ctx))
    request = video_inspection(ctx, [VideoFault("freeze", 0, 1e9)])
    video = request.observations.get("video", {})
    ctx.observed["video summary"] = video
    text = " ".join(request.observations.get("text", []))
    ctx.expect("frozen frames are not analysed", (video.get("frames_rejected") or {}).get("frozen", 0) > 0, str(video))
    ctx.expect("the summary does not claim the frozen time as coverage", video.get("coverage") in ("none", "partial"), str(video.get("coverage")))
    ctx.expect("the text says what was not observed", "not analysed" in text or "nothing can be said" in text, text[:300])
    ctx.recovery = "camera restored for the next scenario"


def camera_disconnects_on_station(ctx):
    ctx.expect("ready", ready(ctx))
    request = video_inspection(ctx, [VideoFault("disconnect", 0, 1e9)])
    video = request.observations.get("video", {})
    ctx.observed["video summary"] = video
    text = " ".join(request.observations.get("text", []))
    ctx.expect("no coverage is claimed for the disconnected time", video.get("coverage") in ("none", "partial") and (video.get("on_station_s") or 0) < 2,
               str(video))
    ctx.expect("the summary never says the area showed nothing", "did not detect any person, vehicle or animal. This does not prove" not in text or "nothing can be said" in text, text[:300])


def camera_blank_frames(ctx):
    ctx.expect("ready", ready(ctx))
    request = video_inspection(ctx, [VideoFault("blank", 0, 3)])
    video = request.observations.get("video", {})
    ctx.observed["video summary"] = video
    ctx.expect("blank frames are rejected and reported", (video.get("frames_rejected") or {}).get("blank", 0) > 0, str(video))
    ctx.expect("the person is still observed after the camera recovers", request.observations.get("max_visible", {}).get("person") == 1,
               str(request.observations.get("max_visible")))


def camera_low_framerate(ctx):
    ctx.expect("ready", ready(ctx))
    request = video_inspection(ctx, [VideoFault("slow", 0, 1e9, factor=5)])
    video = request.observations.get("video", {})
    ctx.observed["video summary"] = video
    ctx.expect("a low frame rate is recorded as a quality note", "low frame rate" in (video.get("quality_notes") or {}), str(video))


def gimbal_angle(ctx):
    """With SITL's simulated SIYI gimbal (overlay sentinel-lite-gimbal-sitl.parm) positions use the reported angle;
    without it, the configured angle, and the summary says so."""
    ctx.expect("ready", ready(ctx))
    simulated_gimbal = ctx.world["adapter"].parameters().get("MNT1_TYPE") == 8
    ctx.observed["simulated SIYI gimbal"] = simulated_gimbal
    request = video_inspection(ctx)
    summary = request.observations
    ctx.observed["pose sources"] = summary.get("pose_sources")
    if simulated_gimbal:
        telemetry = ctx.world["adapter"].telemetry()
        ctx.observed["gimbal pitch reported"] = telemetry.gimbal_pitch_deg
        ctx.expect("the gimbal's own angle (ArduPilot SIYI driver) is used", "gimbal_reported" in (summary.get("pose_sources") or []),
                   str(summary.get("pose_sources")))
        ctx.expect("the audit is satisfied about the gimbal driver",
                   not any(f["param"] == "MNT1_TYPE" and not f["ok"] for f in ctx.world["manager"].parameter_audit()["findings"]))
    else:
        ctx.expect("without gimbal reports, positions use the configured angle and say so",
                   summary.get("pose_sources") == ["configured"] and any("configured camera angle" in line for line in summary.get("text", [])),
                   str(summary.get("pose_sources")))
        ctx.expect("the audit flags the missing gimbal driver (non-blocking)",
                   any(f["param"] == "MNT1_TYPE" and not f["ok"] for f in ctx.world["manager"].parameter_audit()["findings"]))


def evidence_storage_fails(ctx):
    ctx.expect("ready", ready(ctx))
    request = video_inspection(ctx)
    summary = request.observations
    video = summary.get("video", {})
    ctx.observed["video summary"] = video
    ctx.expect("the inspection completes despite the storage failure", request.status == "completed", request.status)
    ctx.expect("no unrecorded evidence path is attached", request.evidence_paths == [], str(request.evidence_paths))
    ctx.expect("the failure is reported in the summary", video.get("evidence_failures", 0) >= 1, str(video))


# ---- operations ------------------------------------------------------------------------------------


def pilot_cancels(ctx):
    lab = ctx.world["lab"]
    ctx.expect("ready", ready(ctx))
    request = fly_inspection(ctx)
    ctx.wait_until(lambda s: request.phase == "enroute", 90, "en route")
    lab.pilot.set_mode("RTL")
    ctx.note("the pilot cancels and returns home")
    landed(ctx)
    ctx.expect("the inspection ends aborted with a note", request.status == "aborted" and "before the inspection finished" in request.note,
               f"{request.status}: {request.note}")


def approval_expires_unflown(ctx):
    manager = ctx.world["manager"]
    manager.safety.approval_window_s = 15
    ctx.expect("ready", ready(ctx))
    request = manager.request_inspection(requested_by="op", map_point=SHED, label="Shed")
    manager.approve(request.request_id, "operator")
    ctx.expect("an approved flight the pilot never starts lapses", ctx.wait_until(lambda s: request.status == "not_flown", 40, "not flown"),
               request.status)
    ctx.expect("the next request can be approved", ctx.wait_until(lambda s: manager.readiness()["ready"], 10, "ready"))


def drone_unavailable_after_approval(ctx):
    manager, lab = ctx.world["manager"], ctx.world["lab"]
    manager.safety.approval_window_s = 20
    ctx.expect("ready", ready(ctx))
    request = manager.request_inspection(requested_by="op", map_point=SHED, label="Shed")
    manager.approve(request.request_id, "operator")
    lab.link.set_hub_link(False)
    lab.link.set_gcs_link(False)
    ctx.note("the aircraft goes silent after approval")
    lapsed = ctx.wait_until(lambda s: request.status == "not_flown", 45, "approval lapses")
    lab.link.set_hub_link(True)
    lab.link.set_gcs_link(True)
    ctx.expect("the approval lapses instead of waiting forever", lapsed, request.status)
    ctx.expect("consistent once the aircraft is back", ctx.wait_until(lambda s: manager.readiness()["ready"], 60, "ready"), str(manager.readiness()["blockers"]))


def overlapping_requests(ctx):
    manager = ctx.world["manager"]
    ctx.expect("ready", ready(ctx))
    first = manager.request_inspection(requested_by="op", map_point=SHED, label="Shed", incident_id="inc_a")
    duplicate = manager.request_inspection(requested_by="op", map_point=SHED, label="Shed", incident_id="inc_a")
    second = manager.request_inspection(requested_by="op", map_point=(20, 14), label="Middle", incident_id="inc_b")
    ctx.expect("the same incident does not create a second request", duplicate is first)
    manager.approve(first.request_id, "operator")
    for request, why in ((first, "approved twice"), (second, "while another is open")):
        try:
            manager.approve(request.request_id, "operator")
            ctx.expect(f"refused: {why}", False, "approved")
        except FlightCommandRejected as error:
            ctx.expect(f"refused: {why}", True, str(error))
    manager._abort_mission("operator cancelled")  # the operator cancels the approved flight before the pilot starts
    ctx.expect("the cancelled approval is final", first.status == "aborted", first.status)
    ctx.expect("the waiting request can be approved afterwards", (manager.approve(second.request_id, "operator").status == "approved"))
    manager._abort_mission("test cleanup")


# The drag coefficients ArduPilot's own wind-estimation autotests use for SITL's quad
# (Copter-4.7.2 Tools/autotest/arducopter.py: GPSForYawWindEstimation, DeadReckoningInWind).
# A real airframe needs coefficients measured for it; these only let SITL's EKF estimate wind.
SITL_DRAG_FUSION = {"EK3_DRAG_BCOEF_X": 9.5, "EK3_DRAG_BCOEF_Y": 9.5, "EK3_DRAG_MCOEF": 0.082}


def strong_wind(ctx):
    lab, adapter, manager = ctx.world["lab"], ctx.world["adapter"], ctx.world["manager"]
    ctx.expect("ready", ready(ctx))
    # SITL's default wind profile scales SIM_WIND_SPD by sqrt(height / SIM_WIND_T_ALT): 11 m/s at
    # 60 m is about 4.9 m/s at 12 m. A constant profile (as ArduPilot's wind tests use) gives 11 m/s here.
    ctx.observed["SITL wind profile before"] = {name: lab.pilot.get_param(name) for name in ("SIM_WIND_T", "SIM_WIND_T_ALT")}
    lab.set("SIM_WIND_T", 1)
    lab.set("SIM_WIND_SPD", 4)
    lab.set("SIM_WIND_TURB", 1)
    ctx.note("4 m/s wind at every height (within the limit): is there an estimate at all?")
    # 1. Lite commissioning has no drag fusion: ArduCopter cannot estimate wind and streams zeros.
    guided_flight(ctx, 12)
    ctx.run_for(30)
    ctx.observed["wind estimate without drag fusion"] = adapter.telemetry().wind_mps
    ctx.expect("without drag fusion Sentinel shows no wind estimate rather than calm air", adapter.telemetry().wind_mps is None,
               str(adapter.telemetry().wind_mps))
    ctx.expect("no wind warning is invented without an estimate", not ctx.seen("health", predicate=lambda v: "wind_over_limit" in v))
    lab.pilot.set_mode("LAND")
    ctx.expect("first flight lands", landed(ctx, 120))
    # 2. Drag fusion configured on the ground and the flight controller restarted, as ArduPilot's own tests do.
    for name, value in SITL_DRAG_FUSION.items():
        lab.set(name, value)
    lab.reboot_sitl()
    ctx.observed["drag coefficients after restart"] = {name: lab.pilot.get_param(name) for name in SITL_DRAG_FUSION}
    ctx.note("SITL drag-fusion coefficients set and the FC restarted")
    ctx.expect("ready after the restart", ready(ctx, 90))
    guided_flight(ctx, 12)
    lab.set("SIM_WIND_SPD", 8)
    lab.set("SIM_WIND_TURB", 2)
    ctx.note("8 m/s wind at every height, with gusts (Lite limit 6 m/s; operator had attested 2 m/s)")
    samples: list[tuple[float, float | None]] = []

    def track(state):
        if not samples or ctx.t() - samples[-1][0] >= 10:
            samples.append((ctx.t(), adapter.telemetry().wind_mps))
        return "wind_over_limit" in state["health"]

    lab.pilot.command(115, 180, 20, 1, 1)  # MAV_CMD_CONDITION_YAW: turn, so drag is seen on both body axes
    flagged = ctx.wait_until(track, 120, "wind estimate above the Lite limit")
    estimate = adapter.telemetry().wind_mps
    ctx.observed["flight controller wind estimate (m/s) every 10 s"] = samples + [(ctx.t(), estimate)]
    ctx.observed["simulated wind (m/s, constant profile)"] = 8
    ctx.expect("the flight controller produces a wind estimate with drag fusion", estimate is not None, str(estimate))
    ctx.expect("Sentinel warns when the flight controller's estimate exceeds the model limit", flagged, f"estimate {estimate}")
    if flagged:
        text = next(c["text"] for c in ctx.world["health"].conditions if c["id"] == "wind_over_limit")
        ctx.expect("the warning calls it an estimate", "estimate" in text, text)
    lab.pilot.set_param("SIM_WIND_SPD", 0)
    lab.pilot.set_param("SIM_WIND_TURB", 0)
    lab.pilot.set_mode("RTL")
    ctx.expect("lands by RTL once the wind is removed", landed(ctx))
    ctx.recovery = "wind removed; landed by RTL; drag coefficients restored and SITL restarted"


def emergency_landing_midway(ctx):
    lab = ctx.world["lab"]
    ctx.expect("ready", ready(ctx))
    request = fly_inspection(ctx)
    ctx.wait_until(lambda s: request.phase == "enroute", 90, "en route")
    # Halfway by position, not by a pause (SIM_SPEEDUP makes wall-clock pauses fly further).
    shed, adapter = map_point_to_geo(ORIGIN, *SHED), ctx.world["adapter"]
    halfway = distance_m(lab.home, shed) / 2

    def from_home(t):
        return distance_m((t.lat, t.lon), lab.home) if t.lat is not None else 0.0

    ctx.wait_until(lambda s: from_home(adapter.telemetry()) >= halfway - 8, 60, "approaching halfway (telemetry lags, LAND decelerates)")
    lab.pilot.set_mode("LAND")
    t = adapter.telemetry()
    ctx.observed["distance from home / to the shed when LAND was selected (m)"] = (round(from_home(t), 1), round(distance_m((t.lat, t.lon), shed), 1))
    ctx.note("the pilot lands immediately, halfway")
    landed(ctx)
    ctx.observed["distance to the shed after landing (m)"] = round(distance_m((adapter.telemetry().lat, adapter.telemetry().lon), shed), 1)
    ctx.expect("LAND was selected en route, before the inspection point", ctx.observed["distance from home / to the shed when LAND was selected (m)"][1] > 8,
               str(ctx.observed))
    ctx.expect("Sentinel says it landed before reaching the inspection point", "before reaching" in (request.note or ""), request.note or "")
    ctx.expect("the inspection is aborted", request.status == "aborted", f"{request.status}: {request.note}")
    flights = ctx.world["store"].list_drone_flights()
    ctx.expect("the flight is logged and closed", bool(flights) and flights[0].get("ended_ms"))


def database_interrupted_in_flight(ctx):
    manager, lab, store = ctx.world["manager"], ctx.world["lab"], ctx.world["store"]
    ctx.expect("ready", ready(ctx))
    request = fly_inspection(ctx)
    ctx.wait_until(lambda s: request.phase == "enroute", 90, "en route")
    store.close()
    ctx.note("database closed under Sentinel")
    errors = []
    for _ in range(40):
        try:
            manager.step(0.5)
        except Exception as error:  # what the service's drone loop would log
            errors.append(f"{type(error).__name__}: {error}")
        time.sleep(0.5)
    ctx.observed["errors raised by the safety loop"] = len(errors)
    ctx.observed["first error"] = errors[0] if errors else ""
    ctx.expect("the safety loop keeps running through a database outage", not errors, errors[0] if errors else "")
    ctx.expect("the flight is still followed", request.phase in ("inspecting", "returning", "done"), request.phase)
    landed_ok = False
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            manager.step(0.5)
        except Exception:
            pass
        telemetry = ctx.world["adapter"].telemetry()
        if not telemetry.armed and not telemetry.in_air:
            landed_ok = True
            break
        time.sleep(0.5)
    ctx.expect("the inspection reaches a final state", request.status in ("completed", "aborted"), request.status)
    ctx.recovery = "landed; a new database is opened for the next scenario"


SCENARIOS = [
    Scenario("gps_none_before_flight", "gps", "No GPS before flight", "Lite on the ground, ready", "SIM_GPS1_ENABLE 0",
             "pre-arm checks refuse arming", "readiness blocks, approval refused, recovers when GPS returns", gps_none_before_flight),
    Scenario("gps_low_satellites", "gps", "Low satellite count", "ready", "SIM_GPS1_NUMSATS 4",
             "fix may degrade; arming may still be refused by HDOP checks", "readiness blocks on poor accuracy", gps_low_satellites),
    Scenario("gps_loss_in_flight", "gps", "GPS lost in flight", "pilot-flown inspection en route", "SIM_GPS1_ENABLE 0 in flight",
             "EKF/GPS failsafe: lands (FS_EKF_ACTION 1) instead of returning", "critical GPS/navigation condition; never claims return-to-home; inspection aborted",
             gps_loss_in_flight),
    Scenario("gps_glitch_in_flight", "gps", "GPS glitch (jump) in flight", "hovering in GUIDED", "SIM_GPS1_GLTCH_X 0.0004 (about 44 m)",
             "EKF glitch protection rejects or absorbs the jump", "any jump in the reported position, or a glitch the FC reports, is flagged", gps_glitch_in_flight),
    Scenario("gps_reports_missing", "gps", "GPS messages missing", "ready", "GPS_RAW_INT filtered out on the radio", "none (it still has GPS)",
             "GPS reported stale; readiness does not pass GPS on a stale report", gps_reports_missing),
    Scenario("telemetry_delayed", "comms", "Telemetry delayed 2.5 s", "ready, SIM_SPEEDUP 1", "2.5 s delay on the radio towards the Hub", "none",
             "the added delay is measured and reported", telemetry_delayed),
    Scenario("home_position_wrong", "gps", "Incorrect home position", "ready", "pilot moves home 33 m, then outside the fence",
             "home moves (return-to-home goes there)", "home mismatch warned; home outside the fence blocks approval", home_position_wrong),
    Scenario("route_crosses_boundary", "geofence", "Route crossing the boundary", "ready", "target 20 m outside the fence", "nothing flies",
             "request refused before approval", route_crosses_boundary),
    Scenario("fence_guided_target_outside", "geofence", "Pilot targets a point outside the fence", "hovering in GUIDED",
             "GUIDED destination 20 m outside the polygon", "refuses the destination; stays inside", "no false breach reported",
             fence_guided_target_outside),
    Scenario("fence_breach_polygon", "geofence", "Pilot flies out of the fence", "hovering near the east edge, AVOID_ENABLE 0",
             "pilot pushes forward across the edge in LOITER", "fence breach: RTL (FENCE_ACTION 1)",
             "geofence breach reported (own polygon or the FC's FENCE_STATUS); no Hub command; clears after landing", fence_breach_polygon),
    Scenario("fence_breach_altitude", "geofence", "Altitude ceiling breach", "hovering at 12 m, AVOID_ENABLE 0", "pilot holds full throttle",
             "altitude fence at 25 m: RTL", "altitude breach reported by Sentinel's monitor", fence_breach_altitude),
    Scenario("fence_disabled_on_fc", "geofence", "Fence disabled on the flight controller", "ready", "FENCE_ENABLE 0", "no fence",
             "audit blocks approval, names FENCE_ENABLE", fence_disabled_on_fc),
    Scenario("fence_polygon_missing", "geofence", "Fence polygon missing", "ready", "fence cleared on the FC", "only the altitude fence",
             "audit blocks: none loaded", fence_polygon_missing),
    Scenario("fence_polygon_mismatch", "geofence", "FC fence differs from Sentinel's", "ready", "a polygon 30 m larger loaded", "looser fence",
             "audit blocks: vertices outside", fence_polygon_mismatch),
    Scenario("fence_breach_without_link", "geofence", "Fence breach during a radio loss", "flying towards the boundary", "radio cut for 20 s",
             "fence and GCS failsafes act without any link", "link loss reported; consistent state and closed flight log after reconnecting", fence_breach_without_link),
    Scenario("battery_low_before_launch", "battery", "Battery too low before launch", "ready", "pack reads 40 %", "none",
             "readiness blocks below the 60 % launch minimum", battery_low_before_launch),
    Scenario("battery_insufficient_for_route", "battery", "Not enough battery for the route", "61 %, reserve 57 %", "route longer than the usable charge",
             "none", "endurance check refuses approval", battery_insufficient_for_route),
    Scenario("battery_drain_and_estimate", "battery", "Rapid drain; estimate against SITL", "hovering", "BATT_CAPACITY cut to a third",
             "drains three times faster from then on", "observed drain measured against the model estimate; fast drain flagged", battery_drain_and_estimate),
    Scenario("battery_telemetry_frozen", "battery", "Battery telemetry frozen", "hovering", "SYS_STATUS and BATTERY_STATUS filtered", "none",
             "battery reported stale (critical in flight); readiness does not use it", battery_telemetry_frozen),
    Scenario("low_voltage_failsafe", "battery", "Low-voltage failsafe", "inspection en route", "SIM_BATT_VOLTAGE 10.3 (BATT_LOW_VOLT 10.5)",
             "battery failsafe: RTL", "the FC's battery verdict shown although % looks fine; operator told why; inspection aborted", low_voltage_failsafe),
    Scenario("return_interrupted_by_battery", "battery", "Return interrupted by a critical battery", "at the inspection point",
             "low voltage, then 9.6 V during the return", "RTL, then LAND where it is", "aborted; landing position and safety events logged",
             return_interrupted_by_battery),
    Scenario("companion_power_loss", "battery", "Companion computer loses power", "", "", "", "",
             None, limitation="The Lite has no companion computer. For Pro/Thermal the relay carries the Hub's telemetry: losing it is the "
                              "radio-loss scenario for the Hub, while the pilot's link (HM30 to the FC) is unaffected. Needs the V1 commissioning SITL."),
    Scenario("radio_lost_in_flight", "comms", "Complete radio loss", "inspection en route", "both directions of the one radio cut for 4 s",
             "GCS failsafe: RTL", "link loss reported; inspection aborted; ready again after landing", radio_lost_in_flight),
    Scenario("link_intermittent", "comms", "Intermittent Hub link", "ready", "Hub side cut 4 s on / 3 s off, four times", "nothing (pilot link fine)",
             "unstable link reported", link_intermittent),
    Scenario("packet_loss", "comms", "30 % packet loss", "ready", "30 % of frames to the Hub lost", "nothing", "telemetry continues; no false alarms", packet_loss),
    Scenario("duplicate_messages", "comms", "Duplicated messages", "ready", "every frame to the Hub sent twice", "nothing",
             "no false restart, jump or state change", duplicate_messages),
    Scenario("hub_restart_in_flight", "comms", "Sentinel restarts during a flight", "inspection en route", "Sentinel process restarted",
             "the pilot's mission continues", "request marked interrupted (never resumed); telemetry resumes; flight log closed", hub_restart_in_flight),
    Scenario("pilot_gcs_lost", "comms", "Pilot's ground station disconnects", "inspection en route", "pilot side of the radio cut",
             "GCS failsafe on the pilot's heartbeats: RTL", "aborted with the failsafe recorded in the flight log", pilot_gcs_lost),
    Scenario("fc_restart_on_ground", "comms", "Flight controller restarts", "ready", "MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN",
             "reboots (SITL wipes and reloads its defaults)", "restart detected; settings re-read before approval", fc_restart_on_ground),
    Scenario("fc_restart_in_flight", "comms", "Flight controller restarts in flight", "", "", "", "", None,
             limitation="ArduCopter refuses a reboot command while armed, and SITL cannot keep the vehicle airborne across a process restart; "
                        "a brown-out in flight is a hardware bench item (power-cycle test with propellers off)."),
    Scenario("second_autopilot_on_link", "comms", "Conflicting telemetry", "ready", "an injected autopilot (system 7) claims to be armed at 90 m",
             "none", "reported as another autopilot; never mixed into the aircraft's state", second_autopilot_on_link),
    Scenario("unauthorized_hub_commands", "security", "Hub tries to command the aircraft", "ready, MAV_OPTIONS lockout", "probe and a take-off from the Hub",
             "ignores everything from system 245", "probe blocked; take-off has no effect; the pilot's requests are answered", unauthorized_hub_commands),
    Scenario("signing_lockout", "security", "MAVLink signing as the lockout", "ready", "MAV_OPTIONS off, then signing on (key only at the pilot)",
             "accepts only signed packets on the radio", "probe open without a lockout, blocked with signing alone; signing detected", signing_lockout),
    Scenario("camera_unreachable_before_flight", "video", "Camera disconnected before the inspection", "ready", "camera refuses connections", "none",
             "camera reported unreachable; approval blocked for a camera-required model", camera_unreachable_before_flight),
    Scenario("camera_freezes_on_station", "video", "Frozen video over the inspection point", "pilot-flown inspection with the camera",
             "picture frozen from arrival", "none", "frozen frames not analysed; no coverage claimed", camera_freezes_on_station),
    Scenario("camera_disconnects_on_station", "video", "Camera disconnects during the inspection", "inspection with the camera",
             "no frames from arrival", "none", "no coverage claimed; never 'nothing detected'", camera_disconnects_on_station),
    Scenario("camera_blank_frames", "video", "Corrupted (blank) frames", "inspection with the camera", "3 s of blank frames on arrival", "none",
             "blank frames rejected; observation continues after", camera_blank_frames),
    Scenario("camera_low_framerate", "video", "Low frame rate", "inspection with the camera", "one frame in five delivered", "none",
             "low frame rate recorded", camera_low_framerate),
    Scenario("gimbal_angle", "video", "Gimbal angle reported or unavailable", "SITL with or without the simulated SIYI gimbal",
             "none (or no gimbal at all)", "the SIYI driver reports the angle when a gimbal is attached",
             "reported angle used when present; otherwise the configured angle, stated, and the audit warns", gimbal_angle),
    Scenario("evidence_storage_fails", "video", "Evidence storage fails", "inspection with the camera", "every evidence write fails", "none",
             "inspection completes; failure reported; no empty evidence paths", evidence_storage_fails),
    Scenario("camera_latency", "video", "High video latency", "", "", "", "", None,
             limitation="OpenCV's RTSP reader exposes no capture time, so latency cannot be measured on the Hub from the stream alone; "
                        "the bench procedure measures glass-to-glass latency with a clock in view (docs/drone-bench-testing.md)."),
    Scenario("pilot_cancels", "operations", "Pilot cancels the inspection", "en route", "pilot switches to RTL", "RTL",
             "aborted, 'returned before the inspection finished'", pilot_cancels),
    Scenario("approval_expires_unflown", "operations", "Approval expires unflown", "approved, 15 s window", "pilot never starts", "none",
             "not_flown; the next request can be approved", approval_expires_unflown),
    Scenario("drone_unavailable_after_approval", "operations", "Drone unavailable after approval", "approved", "aircraft silent for 45 s", "none",
             "approval lapses; consistent when it is back", drone_unavailable_after_approval),
    Scenario("overlapping_requests", "operations", "Overlapping and duplicate requests", "ready", "duplicate incident request, second approval",
             "none", "one request per incident; second approval refused; next one approvable later", overlapping_requests),
    Scenario("strong_wind", "operations", "Strong wind", "hovering, operator attested 2 m/s",
             "constant SIM_WIND_SPD 8, turbulence 2 (first without, then with drag fusion)", "holds position or drifts, drains faster",
             "no estimate shown without drag fusion; with it, the FC's estimate is compared with the Lite's 6 m/s limit", strong_wind),
    Scenario("emergency_landing_midway", "operations", "Emergency landing halfway", "en route", "pilot switches to LAND", "lands in place",
             "aborted; flight logged and closed", emergency_landing_midway),
    Scenario("database_interrupted_in_flight", "operations", "Database interrupted in flight", "en route", "the event database is closed",
             "nothing", "the safety loop keeps running; the inspection reaches a final state", database_interrupted_in_flight),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fc", default="tcp:127.0.0.1:5782")
    parser.add_argument("--hub-port", type=int, default=14571)
    parser.add_argument("--pilot-port", type=int, default=14581)
    parser.add_argument("--model", default="sentinel_lite")
    parser.add_argument("--out", default="bench_runs/faults")
    parser.add_argument("--only", default="")
    parser.add_argument("--skip", default="")
    parser.add_argument("--trace", action="store_true", help="log state and readiness every 15 seconds")
    parser.add_argument("--scenario-timeout", type=float, default=600, help="seconds before a scenario is stopped as an error")
    args = parser.parse_args()
    chosen = [s for s in SCENARIOS if (not args.only or s.id in args.only.split(",")) and s.id not in args.skip.split(",")]
    unknown = set(filter(None, args.only.split(","))) - {s.id for s in SCENARIOS}
    if unknown or not chosen:
        parser.error(f"no valid scenarios selected, or unknown ids: {sorted(unknown)}")
    lab = Lab(args.fc, args.hub_port, args.pilot_port, args.model)
    lab.trace = args.trace
    worlds: dict[str, dict[str, Any]] = {}
    completed = []
    environment = {"firmware": f"ArduCopter {lab.firmware} SITL (official prebuilt stable)",
                   "commissioning": "config/drone/sentinel-lite-sitl.parm", "model": args.model, "date": time.strftime("%Y-%m-%d %H:%M"),
                   "radio": "FaultLink (Sentinel relay in pass-through mode with fault knobs)", "pilot": "scripted ground station, system id 255",
                   "selected": [s.id for s in chosen]}

    def save_results():
        environment["not_run"] = [s.id for s in chosen if s.id not in {r.id for r in completed}]
        return write_report(completed, args.out, title=f"Drone failure scenarios: {catalog.MODEL_INDEX[args.model]['name']} on ArduPilot SITL",
                            environment=environment)

    def prepare(scenario):
        kwargs: dict[str, Any] = {}
        if scenario.group == "video" and scenario.id not in ("camera_unreachable_before_flight",):
            kwargs["video"] = PERSON
        if scenario.id == "evidence_storage_fails":
            def broken(*args):
                raise OSError("No space left on device")
            kwargs["record_evidence"] = broken
        world = lab.sentinel(**kwargs)
        world["lab"] = lab
        worlds[scenario.id] = world
        return world, lambda: lab.snapshot(world), lambda: lab.tick(world)

    def finish(ctx):
        lab.restore(ctx.world)

    def report(result):
        completed.append(result)
        save_results()  # retain completed evidence if a later scenario times out
        print(f"{result.id:36s} {result.status.upper():13s} {result.duration_s:6.1f}s  " +
              "; ".join(f"{'ok' if c.passed else 'FAILED'}: {c.name}" for c in result.checks if not c.passed)[:300]
              + (f"  [{result.error.strip().splitlines()[0][:200]}]" if result.error else ""), flush=True)

    save_results()
    try:
        results = run_scenarios(chosen, model=args.model, prepare=prepare, finish=finish, on_result=report,
                                deadline_s=args.scenario_timeout)
        print(json.dumps(save_results()))
        return 0 if len(results) == len(chosen) and all(r.status in ("pass", "not_modelled") for r in results) else 1
    finally:
        for world in worlds.values():
            lab.close_sentinel(world)
        lab.link.stop()
        lab.pilot.close()


if __name__ == "__main__":
    raise SystemExit(main())
