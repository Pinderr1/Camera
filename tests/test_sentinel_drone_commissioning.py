"""The drone setup journey and its readiness level: evidence only, simulation
never counted as physical readiness, bench results tied to the settings."""

import unittest

from sentinel.drone.commissioning import LEVELS, bench_record, journey


def status(**changes):
    base = {
        "physical": True, "model": {"id": "sentinel_lite", "name": "Sentinel Lite"},
        "telemetry": {"connected": True, "link_ok": True, "last_heartbeat_ms": 1, "autopilot_version": "4.7.2", "battery_voltage": 16.4,
                      "gps_ms": 1},
        "readiness": {"checks": [{"id": "gps", "state": "pass"}, {"id": "lockout", "state": "pass", "detail": "MAV_OPTIONS"}], "blockers": []},
        "parameter_audit": {"ok": True, "blocking": [], "fingerprint": "abc123"},
        "video": {"state": "ready", "detail": "live picture verified"},
        "maintenance": {"commissioned": True},
    }
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = base[key] | value
        else:
            base[key] = value
    return base


class JourneyTests(unittest.TestCase):
    def test_levels_rise_only_with_evidence(self):
        self.assertEqual(journey(None, [], device={"drone_model": "sentinel_lite"})["level"], "planned")
        silent = status(telemetry={"last_heartbeat_ms": 0, "link_ok": False})
        self.assertEqual(journey(silent, [])["level"], "planned")
        lost = status(telemetry={"link_ok": False})
        self.assertEqual(journey(lost, [])["level"], "hardware_detected")
        bare = status(video=None, parameter_audit={"ok": False, "blocking": [{"param": "FENCE_ENABLE", "actual": "0"}], "fingerprint": "x"},
                      maintenance={"commissioned": False}, telemetry={"battery_voltage": None})
        self.assertEqual(journey(bare, [])["level"], "connected")
        partial = status(video=None)
        self.assertEqual(journey(partial, [])["level"], "partially_configured")
        self.assertEqual(journey(status(), [])["level"], "configured")

    def test_bench_record_must_match_the_current_settings(self):
        bench = {"kind": "bench_test", "recorded_ms": 10, "by": "tech", "verdict": "pass", "fingerprint": "abc123"}
        self.assertEqual(journey(status(), [bench])["level"], "bench_verified")
        changed = journey(status(parameter_audit={"fingerprint": "def456"}), [bench])
        self.assertEqual(changed["level"], "configured", "settings changed after the bench test")
        self.assertIn("repeat it", next(s for s in changed["steps"] if s["id"] == "diagnostic")["detail"])
        failed = bench | {"verdict": "fail"}
        self.assertEqual(journey(status(), [failed])["level"], "configured")

    def test_people_record_flight_tests_and_authorization_in_order(self):
        bench = {"kind": "bench_test", "recorded_ms": 10, "verdict": "pass", "fingerprint": "abc123"}
        flight = {"kind": "flight_test", "recorded_ms": 20, "verdict": "pass", "by": "pilot (Advanced certificate)"}
        authorization = {"kind": "authorization", "recorded_ms": 30, "by": "owner"}
        self.assertEqual(journey(status(), [bench, flight])["level"], "flight_test_verified")
        result = journey(status(), [bench, flight, authorization])
        self.assertEqual(result["level"], "operationally_authorized")
        self.assertIn("Recorded by a person", " ".join(result["notes"]))
        self.assertIn("telemetry-only", " ".join(result["notes"]))
        stale = journey(status(), [bench | {"recorded_ms": 40}, flight])
        self.assertEqual(stale["level"], "bench_verified", "a flight test before the latest bench run does not count")

    def test_a_simulator_never_reaches_physical_levels(self):
        bench = {"kind": "bench_test", "recorded_ms": 10, "verdict": "pass", "fingerprint": "abc123"}
        result = journey(status(physical=False), [bench, {"kind": "flight_test", "recorded_ms": 20, "verdict": "pass"}])
        self.assertEqual(result["level"], "simulated")
        self.assertTrue(result["simulated"])

    def test_next_step_is_the_first_open_one_and_is_actionable(self):
        result = journey(status(video=None), [])
        self.assertEqual(result["next"]["id"], "equipment")
        result = journey(status(video=None), [], equipment_confirmed=True)
        self.assertEqual(result["next"]["id"], "camera")
        self.assertIn("stream address", result["next"]["action"])
        self.assertEqual(LEVELS.index("bench_verified") > LEVELS.index("configured"), True)

    def test_bench_record_from_a_report(self):
        record = bench_record({"verdict": "fail", "failed": ["lockout"], "manual_open": ["latency"]}, "abc123", "tech")
        self.assertEqual((record["kind"], record["verdict"], record["fingerprint"]), ("bench_test", "fail", "abc123"))
        self.assertIn("lockout", record["note"])


if __name__ == "__main__":
    unittest.main()
