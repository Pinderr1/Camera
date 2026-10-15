"""The propeller-free bench test against ArduPilot SITL (real ArduCopter firmware)
with the Lite commissioning, standing in for the first hardware bench.

Skipped unless SENTINEL_SITL=bench. Start a separate simulator instance:

    bash scripts/sitl/start_sitl_instance.sh 2 3 config/drone/sentinel-lite-sitl.parm     # ports 5780-5783
    $env:SENTINEL_SITL='bench'; python -m unittest tests.test_sentinel_drone_bench_sitl

The operator's physical steps are scripted: unplugging the link is a cut in
the emulated radio, a power cycle is a reboot command from the pilot's ground
station (SITL re-executes itself), switching the transmitter off is SITL's
SIM_RC_FAIL."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "sitl"))

ENABLED = os.environ.get("SENTINEL_SITL") == "bench"
FC = os.environ.get("SENTINEL_SITL_FC", "tcp:127.0.0.1:5782")
HUB_UDP, PILOT_UDP = 14591, 14592
ORIGIN = (45.0, -75.0)


def _deps() -> bool:
    try:
        import cv2  # noqa: F401
        import pymavlink  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(ENABLED and _deps(), "set SENTINEL_SITL=bench with a SITL instance on the Lite commissioning (pymavlink, OpenCV)")
class BenchOnSitlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gcs_standin import SitlGroundStation

        from sentinel.drone import Geofence
        from sentinel.drone.base import map_point_to_geo
        from sentinel.drone.faults import FaultLink
        from sentinel.drone.mission import plan_route, to_qgc_plan

        cls.radio = FaultLink(FC, f"udp:127.0.0.1:{HUB_UDP}", [f"udp:127.0.0.1:{PILOT_UDP}"]).start()
        cls.pilot = SitlGroundStation(f"udpin:127.0.0.1:{PILOT_UDP}")
        cls.pilot.start_heartbeats()
        fence = Geofence([list(map_point_to_geo(ORIGIN, x, y)) for x, y in [[0, 0], [40, 0], [40, 28], [0, 28]]], 25, 5)
        home = map_point_to_geo(ORIGIN, 6, 4)
        cls.plan = to_qgc_plan(plan_route(home, map_point_to_geo(ORIGIN, 35.5, 3), fence, safe_transit_altitude_m=12, inspection_altitude_m=12,
                                          hover_s=4), fence)
        cls.pilot.load_fence(cls.plan)

    @classmethod
    def tearDownClass(cls):
        cls.pilot.set_param("SIM_RC_FAIL", 0)
        cls.pilot.close()
        cls.radio.stop()

    def test_lite_bench_passes_on_real_firmware(self):
        from sentinel.drone import catalog
        from sentinel.drone.bench import BenchContext, run_bench, write_bundle
        from sentinel.drone.mavlink import MavlinkTelemetryAdapter
        from sentinel.drone.observation import CameraModel
        from sentinel.drone.video import SimulatedAerialSource

        adapter = MavlinkTelemetryAdapter(f"udpin:127.0.0.1:{HUB_UDP}")
        adapter.connect()
        self.addCleanup(adapter.disconnect)
        deadline = time.time() + 30
        while not adapter.telemetry().link_ok and time.time() < deadline:
            time.sleep(0.2)
        # What the pilot's ground station does on connect; the Hub overhears it on the shared radio.
        self.pilot.download_parameters()
        self.pilot.download_fence()
        self.pilot.request_message(148)  # AUTOPILOT_VERSION
        model = catalog.MODEL_INDEX["sentinel_lite"]
        camera = CameraModel.from_dict(catalog.camera_settings(model))

        def operator(prompt: str) -> str:
            if "Unplug" in prompt:
                self.radio.set_hub_link(False)
                time.sleep(6)
                self.radio.set_hub_link(True)
            elif "Disconnect the flight controller" in prompt:
                self.pilot.reboot()
                time.sleep(8)
                self.pilot.link.wait_heartbeat(timeout=30)
                self.pilot.load_fence(self.plan)  # SITL's restart wipes stored settings (-w); a real FC keeps them
                self.pilot.download_parameters()
            elif "OFF" in prompt:
                self.pilot.set_param("SIM_RC_FAIL", 1)
                time.sleep(4)
            elif "back ON" in prompt:
                self.pilot.set_param("SIM_RC_FAIL", 0)
            return ""

        with tempfile.TemporaryDirectory() as directory:
            ctx = BenchContext(adapter, model, measured_voltage=12.6, cells=3, media_root=Path(directory) / "media", ask=operator,
                               video_source=lambda: SimulatedAerialSource([], ORIGIN, camera, adapter.telemetry, fps=5))
            confirmed = {item: True for item in ("propellers_off", "compass_calibrated", "latency", "gimbal_moves", "vtx_gps_interference")}
            report = run_bench(ctx, expected_firmware="4.7", confirmed=confirmed,
                               on_check=lambda c: print(f"{c.state.upper():8s} {c.title}: {c.detail}"[:240], flush=True))
            write_bundle(report, adapter, Path("bench_runs") / "bench_sitl")
        checks = {check["id"]: check for check in report["checks"]}
        self.assertEqual(checks["lockout"]["state"], "pass", checks["lockout"])
        self.assertEqual(checks["lockout"]["evidence"]["probe"]["state"], "blocked")
        self.assertEqual(checks["power_cycle"]["state"], "pass", checks["power_cycle"])
        self.assertEqual(checks["reconnect"]["state"], "pass", checks["reconnect"])
        self.assertEqual(checks["camera"]["state"], "pass", checks["camera"])
        self.assertEqual(checks["gimbal"]["state"], "fail", "SITL has no SIYI gimbal attached: the bench must say so")
        not_passing = {cid: c["state"] for cid, c in checks.items() if c["state"] != "pass"}
        self.assertEqual(set(not_passing) - {"gimbal", "rc_failsafe"}, set(), not_passing)
        self.assertEqual(report["verdict"], "fail", "honest verdict: no gimbal")
