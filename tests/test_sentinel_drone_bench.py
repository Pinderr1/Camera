"""The propeller-free hardware bench test against a scripted flight controller.
tests/test_sentinel_drone_bench_sitl.py runs the same checks against ArduPilot SITL."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sentinel.drone import catalog
from sentinel.drone.base import DroneTelemetry, map_point_to_geo
from sentinel.drone.bench import BenchContext, run_bench, write_bundle

ORIGIN = (45.0, -75.0)
HOME = map_point_to_geo(ORIGIN, 6, 4)
LITE_PARAMS = {"FS_GCS_ENABLE": 1, "FS_THR_ENABLE": 1, "BATT_FS_LOW_ACT": 2, "BATT_FS_CRT_ACT": 1, "FS_EKF_ACTION": 1, "FENCE_ENABLE": 1,
               "FENCE_TYPE": 5, "FENCE_ACTION": 1, "FENCE_ALT_MAX": 25, "RTL_ALT": 1500, "ARMING_CHECK": 1, "BATT_LOW_VOLT": 14.0,
               "BATT_CRT_VOLT": 13.6, "BATT_MONITOR": 4, "MNT1_TYPE": 8, "MAV_OPTIONS": 1, "MAV_GCS_SYSID": 255}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeFC:
    """A real-aircraft adapter as the bench sees it: telemetry, parameters, message counters, the probe."""

    def __init__(self, clock):
        self.clock = clock
        self.t = DroneTelemetry(connected=True, link_ok=True, battery_pct=96, battery_voltage=16.38, battery_current_a=0.9, battery_ms=1,
                                lat=HOME[0], lon=HOME[1], gps_fix=3, satellites=12, hdop=0.9, gps_ms=1, ekf_ok=True, prearm_ok=True,
                                autopilot_version="4.7.2", gimbal_ms=1, gimbal_pitch_deg=-90.0, gimbal_yaw_deg=0.0)
        self.params = dict(LITE_PARAMS)
        self.probe = {"state": "blocked", "detail": "no acknowledgement"}
        self.rates = {"HEARTBEAT": 1, "GLOBAL_POSITION_INT": 4, "SYS_STATUS": 2, "GPS_RAW_INT": 2, "ATTITUDE": 4, "EKF_STATUS_REPORT": 2,
                      "BATTERY_STATUS": 1, "GIMBAL_DEVICE_ATTITUDE_STATUS": 4, "HOME_POSITION": 0.2}
        self.message_counts = {name: 0 for name in self.rates}
        self._exact = {}
        self.autopilot_info = {"version": "4.7.2", "git": "a1b2c3d4"}
        self.reconnects = 0
        self.events = {}  # operator step -> effect on telemetry
        self.arm_at = None
        self.last = clock()

    def advance(self):
        elapsed = self.clock() - self.last
        self.last = self.clock()
        for name, hz in self.rates.items():
            self._exact[name] = self._exact.get(name, 0.0) + hz * elapsed
            self.message_counts[name] = int(self._exact[name])
        if self.arm_at is not None and self.clock() >= self.arm_at:
            self.t.armed = True

    def telemetry(self):
        self.advance()
        return DroneTelemetry(**self.t.as_dict())

    def parameters(self):
        return dict(self.params)

    def probe_command_path(self, timeout_s):
        return dict(self.probe)

    def link_status(self):
        return {"vehicle": "1/1"}


class BenchTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.fc = FakeFC(self.clock)
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)

    def sleep(self, seconds):
        self.clock.now += seconds

    def context(self, **changes):
        values = dict(adapter=self.fc, model=catalog.MODEL_INDEX["sentinel_lite"], measured_voltage=16.40, media_root=Path(self.media.name),
                      sleep=self.sleep, clock=self.clock)
        return BenchContext(**(values | changes))

    def operator(self, script):
        """An operator who performs each physical step; the fake FC shows its effect."""
        def ask(prompt):
            for key, effect in script.items():
                if key in prompt:
                    effect()
            return ""
        return ask

    def by_id(self, report):
        return {check["id"]: check for check in report["checks"]}

    def test_a_good_lite_bench_passes_with_operator_steps(self):
        def unplug():
            self.fc.t.link_ok = False
            self.fc.reconnects += 1
            self.fc.events["relink"] = True

        def power_cycle():
            self.fc.t.fc_reboots += 1

        def transmitter_off():
            self.fc.t.messages = self.fc.t.messages + ["Radio Failsafe"]
            self.fc.t.message_count += 1

        ask = self.operator({"Unplug": unplug, "Disconnect the flight controller": power_cycle, "OFF": transmitter_off})
        original = self.fc.telemetry

        def telemetry():  # the link comes back a few seconds after the unplug
            if self.fc.events.get("relink"):
                self.fc.events["relink_at"] = self.fc.events.get("relink_at", self.clock() + 5)
                if self.clock() >= self.fc.events["relink_at"]:
                    self.fc.t.link_ok = True
                    self.fc.events.clear()
            return original()

        self.fc.telemetry = telemetry
        report = run_bench(self.context(ask=ask), confirmed={item: True for item in ("propellers_off", "compass_calibrated", "latency",
                                                                                     "gimbal_moves", "vtx_gps_interference")})
        checks = self.by_id(report)
        self.assertEqual(report["verdict"], "pass", [c for c in report["checks"] if c["state"] != "pass"])
        self.assertIn("probe ignored", checks["lockout"]["detail"])
        self.assertEqual(checks["camera"]["state"], "skipped")  # no stream given here: the camera check has its own test
        self.assertAlmostEqual(checks["rates"]["evidence"]["rates_hz"]["GLOBAL_POSITION_INT"], 4.0, delta=0.3)
        with tempfile.TemporaryDirectory() as out:
            paths = write_bundle(report, self.fc, out)
            self.assertIn("FS_GCS_ENABLE,1", Path(paths["parameters"]).read_text())
            self.assertIn("Sentinel sent no flight command", Path(paths["markdown"]).read_text(encoding="utf-8"))

    def test_hub_commands_reaching_the_fc_fail_the_bench(self):
        self.fc.probe = {"state": "open", "result": 3, "detail": "acknowledged"}
        report = run_bench(self.context())
        lockout = self.by_id(report)["lockout"]
        self.assertEqual((lockout["state"], report["verdict"]), ("fail", "fail"))
        self.assertIn("MAV_OPTIONS", lockout["how_to_fix"])

    def test_arming_aborts_the_bench(self):
        self.fc.arm_at = self.clock() + 3
        report = run_bench(self.context())
        self.assertEqual(report["verdict"], "fail")
        self.assertIn("armed during the bench test", report["aborted"])

    def test_battery_calibration_rates_and_settings(self):
        self.fc.t.battery_voltage = 15.9  # multimeter says 16.40
        self.fc.rates["GLOBAL_POSITION_INT"] = 0.5
        self.fc.params["FENCE_ENABLE"] = 0
        checks = self.by_id(run_bench(self.context()))
        self.assertEqual(checks["battery"]["state"], "fail")
        self.assertIn("BATT_VOLT_MULT", checks["battery"]["how_to_fix"])
        self.assertEqual(checks["rates"]["state"], "fail")
        self.assertIn("MAV2_", checks["rates"]["how_to_fix"])
        self.assertEqual(checks["parameters"]["state"], "fail")
        self.assertIn("FENCE_ENABLE", checks["parameters"]["detail"])

    def test_no_link_stops_early_and_nothing_is_marked_passed(self):
        self.fc.t.link_ok = False
        report = run_bench(self.context())
        self.assertEqual(report["verdict"], "fail")
        self.assertEqual([c["id"] for c in report["checks"] if c["state"] == "pass"], [])

    def test_indoor_bench_without_gps_and_without_an_operator(self):
        self.fc.t.gps_fix, self.fc.t.satellites, self.fc.t.ekf_ok = 1, 3, False
        checks = self.by_id(run_bench(self.context(measured_voltage=None)))
        self.assertEqual(checks["gps"]["state"], "warn")
        self.assertEqual(checks["battery"]["state"], "manual")
        self.assertEqual(checks["reconnect"]["state"], "manual")

    def test_camera_check_needs_a_live_picture(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        from sentinel.vision import Frame

        rng = np.random.default_rng(3)
        base = np.full((90, 160, 3), 100, np.uint8)
        base[20:50, 40:90] = 180

        def stream(frozen):
            state = {"n": 0}

            def read():
                state["n"] += 1
                image = base if frozen else np.clip(base.astype(np.int16) + rng.integers(-2, 3, base.shape), 0, 255).astype(np.uint8)
                self.clock.now += 0.2
                return Frame(image.copy(), int(self.clock() * 1000))
            return lambda: SimpleNamespace(read=read, close=lambda: None)

        live = run_bench(self.context(video_source=stream(False)))
        frozen = run_bench(self.context(video_source=stream(True)))
        self.assertEqual(self.by_id(live)["camera"]["state"], "pass")
        self.assertEqual(self.by_id(frozen)["camera"]["state"], "fail")
        self.assertIn("no live picture", self.by_id(frozen)["camera"]["detail"])


if __name__ == "__main__":
    unittest.main()
