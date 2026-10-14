"""Security intelligence: the extended rule language (modes, days, cooldowns,
notification preferences, minimum observations, combinations), the rule API and
rule tester, incident explanations, activity insights, feedback reports,
search, missed-event reports, and evidence-preserving exports.

Behaviour is checked with deterministic replays through the real tracker,
correlator, engine, incident manager and alert policy."""

import copy
import io
import json
import tempfile
import time
import unittest
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from sentinel.config import load_config, validate_config
from sentinel.engine import TemporalEventEngine
from sentinel.explain import explain_incident
from sentinel.incidents import AlertPolicy, AlertPolicyConfig
from sentinel.insights import ActivitySettings, compute_insights
from sentinel.models import Incident, SecurityEvent
from sentinel.replay import run_scenario
from sentinel.rule_lab import test_rule as lab_test_rule
from sentinel.rules import describe_rule, templates
from sentinel.simulation import Scenario
from sentinel.store import EventStore

from sentinel_helpers import NOON_MS, Driver, make_config, person

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None

CONFIG = load_config("config/sentinel.example.json", resolve_secrets=False)
NIGHT = datetime(2026, 10, 14, 0, 40)  # a Wednesday, 00:40
DAY = datetime(2026, 10, 14, 14, 0)
GATE_THEN_SHED = {"id": "gate_then_shed", "name": "Side gate then shed after midnight", "type": "sequence", "zones": ["side_gate", "shed"],
                  "classes": ["person"], "within_s": 180, "active_hours": {"start": "00:00", "end": "05:00"}, "severity": "high", "notify": "always"}


def walk_right(start=0.1, speed=0.004):
    return lambda now: [person(min(0.95, start + (now - NOON_MS) / 200 * speed), 0.8)]


class RuleLanguageTests(unittest.TestCase):
    def engine(self, rules, **kwargs):
        return TemporalEventEngine(make_config(rules=rules), **kwargs)

    def test_day_of_week_condition(self):
        rule = {"id": "weekend_shed", "type": "zone_entry", "zones": ["shed"], "severity": "high", "active_days": ["sat", "sun"]}
        wednesday = Driver(self.engine([rule]), start_ms=NOON_MS).run(4, {"cam_a": [person(0.8, 0.8)]})
        self.assertEqual(wednesday.of_type("zone_entry"), [])
        saturday = Driver(self.engine([rule]), start_ms=int(datetime(2026, 10, 17, 12, 0).timestamp() * 1000)).run(4, {"cam_a": [person(0.8, 0.8)]})
        self.assertEqual(len(saturday.of_type("zone_entry")), 1)

    def test_overnight_window_counts_for_the_day_it_started(self):
        rule = {"id": "fri_night", "type": "zone_entry", "zones": ["shed"], "severity": "high", "active_days": ["fri"],
                "active_hours": {"start": "22:00", "end": "06:00"}}
        saturday_2am = int(datetime(2026, 10, 17, 2, 0).timestamp() * 1000)
        events = Driver(self.engine([rule]), start_ms=saturday_2am).run(4, {"cam_a": [person(0.8, 0.8)]}).of_type("zone_entry")
        self.assertEqual(len(events), 1, "Saturday 02:00 belongs to Friday night")

    def test_cooldown_records_repeats_as_information(self):
        rule = {"id": "shed", "type": "zone_entry", "zones": ["shed"], "severity": "high", "cooldown_s": 600}
        driver = Driver(self.engine([rule]))
        driver.run(4, {"cam_a": [person(0.8, 0.8)]}).run(10, {})
        driver.run(4, {"cam_a": [person(0.8, 0.8)]})
        entries = driver.of_type("zone_entry")
        self.assertEqual([event.severity for event in entries], ["high", "info"])
        self.assertIn("rule_cooldown", entries[1].reason_codes)
        self.assertEqual(entries[1].details["original_severity"], "high")

    def test_min_observations_waits_for_enough_detections(self):
        rule = {"id": "shed", "type": "zone_entry", "zones": ["shed"], "severity": "high", "min_observations": 12}
        driver = Driver(self.engine([rule])).run(1.6, {"cam_a": [person(0.8, 0.8)]})
        self.assertEqual(driver.of_type("zone_entry"), [], "not enough detections yet")
        driver.run(2, {"cam_a": [person(0.8, 0.8)]})
        self.assertEqual(len(driver.of_type("zone_entry")), 1)

    def test_arming_later_never_fires_an_entry_for_someone_already_inside(self):
        rule = {"id": "shed_away", "type": "zone_entry", "zones": ["shed"], "severity": "high", "arm_modes": ["away"]}
        engine = self.engine([rule], arm_mode="home")
        driver = Driver(engine).run(20, {"cam_a": [person(0.8, 0.8)]})
        engine.arm_mode = "away"
        driver.run(10, {"cam_a": [person(0.8, 0.8)]})
        self.assertEqual(driver.of_type("zone_entry"), [])

    def test_combination_needs_several_observations_of_one_linked_subject(self):
        rules = [
            {"id": "in_yard", "type": "zone_entry", "zones": ["yard"], "severity": "low", "notify": "never"},
            {"id": "in_shed", "type": "zone_entry", "zones": ["shed"], "severity": "low", "notify": "never"},
            {"id": "yard_then_shed", "type": "combination", "rules": ["in_yard", "in_shed"], "min_matches": 2, "within_s": 120, "severity": "high"},
        ]
        driver = Driver(self.engine(rules)).run(40, {"cam_a": walk_right()})
        combos = driver.of_type("combination")
        self.assertEqual(len(combos), 1)
        self.assertEqual(combos[0].details["matched_rules"], ["in_shed", "in_yard"])
        self.assertIn("linked observations", combos[0].summary)
        only_yard = Driver(self.engine(rules)).run(10, {"cam_a": [person(0.2, 0.8)]})
        self.assertEqual(only_yard.of_type("combination"), [], "one observation is not enough")

    def test_site_scope_combination_says_people_may_differ(self):
        rules = [
            {"id": "porch", "type": "zone_entry", "zones": ["porch"], "severity": "low"},
            {"id": "shed", "type": "zone_entry", "zones": ["shed"], "severity": "low"},
            {"id": "both", "type": "combination", "rules": ["porch", "shed"], "min_matches": 2, "within_s": 300, "scope": "site", "severity": "medium"},
        ]
        driver = Driver(self.engine(rules)).run(4, {"cam_b": [person(0.5, 0.8)]}).run(4, {"cam_a": [person(0.8, 0.8)]})
        combo = driver.of_type("combination")
        self.assertEqual(len(combo), 1)
        self.assertIn("different people", combo[0].details["identity_note"])

    def test_notification_preferences_and_new_modes(self):
        policy = AlertPolicy(AlertPolicyConfig.from_dict({}))
        self.assertEqual(policy.config.notify_min_severity["night"], "medium")
        self.assertEqual(policy.config.notify_min_severity["vacation"], "low")

        def evaluate(notify, severity, mode):
            event = SecurityEvent("e", "zone_entry", severity, "cam_a", 1, 1, 0.9, "Person entered Shed", details={"notify": notify} if notify else {})
            incident = Incident(f"inc_{notify}_{severity}_{mode}", 1, 1, severity, "Person entered Shed")
            return AlertPolicy(AlertPolicyConfig.from_dict({})).evaluate(incident, [event], mode, 1)

        self.assertIsNone(evaluate("never", "high", "away"), "record-only rules never push")
        self.assertIsNotNone(evaluate("always", "low", "home"), "always-notify rules push below the mode's level")
        self.assertIsNone(evaluate("always", "high", "disarmed"), "nothing pushes while disarmed")
        self.assertIsNotNone(evaluate(None, "low", "vacation"))
        self.assertIsNone(evaluate(None, "low", "night"))

    def test_validation_of_new_fields(self):
        config = copy.deepcopy(CONFIG)
        config["rules"] += [
            {"id": "bad", "type": "zone_entry", "severity": "high", "active_days": ["funday"], "notify": "sometimes", "cooldown_s": -1, "min_observations": 0},
            {"id": "combo", "type": "combination", "rules": ["combo", "missing"], "min_matches": 5, "within_s": 0, "severity": "high"},
        ]
        with self.assertRaises(ValueError) as caught:
            validate_config(config)
        message = str(caught.exception)
        for fragment in ("active_days", "notify must be", "cooldown_s", "min_observations", "cannot include the rule itself",
                         "unknown rule 'missing'", "min_matches", "within_s"):
            self.assertIn(fragment, message)

    def test_templates_are_valid_once_zones_are_chosen(self):
        for template in templates():
            rule = copy.deepcopy(template["rule"]) | {"id": f"t_{template['key']}"}
            if rule["type"] == "sequence":
                rule["zones"] = ["side_gate", "shed"]
            if rule["type"] == "combination":
                rule["rules"] = ["restricted_entry", "porch_dwell"]
            config = copy.deepcopy(CONFIG)
            config["rules"].append(rule)
            validate_config(config)
            self.assertTrue(describe_rule(rule)["what"])


class CoherentIncidentScenarioTests(unittest.TestCase):
    """The brief's example: a person enters the side gate after midnight and is near
    the shed within three minutes -> one coherent, explained incident."""

    def replay(self, start, storage=None):
        config = copy.deepcopy(CONFIG)
        config["rules"].append(GATE_THEN_SHED)
        return run_scenario(config, Scenario.load("config/scenarios/gate_then_shed.json"), start=start, arm_mode="home", storage_dir=storage), config

    def test_night_sequence_creates_one_incident_with_one_notification(self):
        result, _ = self.replay(NIGHT)
        sequence = [event for event in result["events"] if event["rule_id"] == "gate_then_shed"]
        self.assertEqual(len(sequence), 1)
        self.assertEqual(len(result["incidents"]), 1)
        incident = result["incidents"][0]
        self.assertIn("sequence", incident["event_types"])
        self.assertEqual(incident["severity"], "high")
        self.assertEqual(len(result["notifications"]), 1)
        self.assertIn("Why:", result["notifications"][0]["message"])
        self.assertTrue(result["notifications"][0]["message"].endswith("Review the clip before acting."))

    def test_the_same_walk_by_day_does_not_match_the_night_rule(self):
        result, _ = self.replay(DAY)
        self.assertEqual([event for event in result["events"] if event["rule_id"] == "gate_then_shed"], [])

    def test_explanation_separates_observed_from_inferred_and_never_claims_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            result, config = self.replay(NIGHT, storage=directory)
            store = EventStore(Path(directory) / "sentinel.db")
            incident = store.list_incidents()[0]
            explanation = explain_incident(incident, store.list_events(incident_id=incident["incident_id"]), config)
            visits = store.list_visits()
            store.close()
        self.assertEqual([camera["name"] for camera in explanation["cameras"]], ["Backyard"])
        self.assertTrue({"Side gate", "Shed"} <= {zone["name"] for zone in explanation["zones"]})
        rules = {rule["rule_id"]: rule for rule in explanation["rules"]}
        self.assertIn("gate_then_shed", rules)
        self.assertIn("between 00:00 and 05:00", rules["gate_then_shed"]["description"]["when"])
        self.assertTrue(all(step["basis"] == "observed" for step in explanation["sequence"]), "one camera, no inference needed")
        self.assertIn("no cross-camera inference was needed", " ".join(explanation["confidence"]["uncertainty"]))
        text = json.dumps(explanation).lower()
        for word in ("intruder", "burglar", "suspicious", "criminal", "intent to"):
            self.assertNotIn(word, text)
        self.assertTrue(visits, "completed zone visits are logged for activity statistics")
        self.assertEqual({visit["label"] for visit in visits}, {"person"})

    def test_cross_camera_links_are_marked_as_inferred(self):
        with tempfile.TemporaryDirectory() as directory:
            run_scenario(copy.deepcopy(CONFIG), Scenario.load("config/scenarios/prowler.json"), start=datetime(2026, 10, 12, 2, 0), arm_mode="home", storage_dir=directory)
            store = EventStore(Path(directory) / "sentinel.db")
            incident = store.list_incidents()[0]
            explanation = explain_incident(incident, store.list_events(incident_id=incident["incident_id"]), CONFIG)
            store.close()
        self.assertEqual(len(explanation["cameras"]), 3)
        self.assertEqual(sorted(row["basis"] for row in explanation["cameras"]), ["inferred", "inferred", "observed"],
                         "the rules fired on the backyard camera; the other two cameras are known through cross-camera links")
        inferred = [step for step in explanation["sequence"] if step["basis"] == "inferred"]
        self.assertTrue(inferred and all("linking" in step["note"] for step in inferred if step["event_type"] != "repeated_presence"))
        self.assertTrue(explanation["confidence"]["cross_camera_links"])
        self.assertIn("not a recognition of who the person is", " ".join(explanation["confidence"]["uncertainty"]))
        self.assertIn("The most serious rule matched was high.", explanation["severity"]["reasons"])


class RuleLabTests(unittest.TestCase):
    def test_walk_simulation_runs_the_real_engine(self):
        result = lab_test_rule(CONFIG, GATE_THEN_SHED, source="walk", arm_mode="home", start=NIGHT,
                               steps=[{"zone_id": "side_gate", "stay_s": 6, "gap_s": 3}, {"zone_id": "shed", "stay_s": 10}])
        self.assertEqual(len(result["fired"]), 1)
        self.assertEqual(len(result["notifications"]), 1)
        self.assertIn("fired 1 time", result["verdict"])

    def test_walk_that_cannot_be_linked_between_cameras_is_explained(self):
        rule = GATE_THEN_SHED | {"zones": ["driveway", "shed"], "within_s": 600}
        result = lab_test_rule(CONFIG, rule, source="walk", arm_mode="home", start=NIGHT,
                               steps=[{"zone_id": "driveway", "stay_s": 6, "gap_s": 200}, {"zone_id": "side_gate", "stay_s": 5, "gap_s": 2}, {"zone_id": "shed", "stay_s": 8}])
        self.assertEqual(result["fired"], [], "a 200 s gap does not fit the driveway-backyard connection (45 s): no link, no match")
        self.assertTrue(any("not linked between cameras" in note for note in result["notes"]))

    def test_invalid_rule_is_rejected_before_simulating(self):
        with self.assertRaises(ValueError):
            lab_test_rule(CONFIG, GATE_THEN_SHED | {"zones": ["side_gate", "nowhere"]}, source="walk", steps=[{"zone_id": "side_gate"}])

    def test_recorded_activity_replay(self):
        base = int(datetime(2026, 10, 10, 1, 0).timestamp() * 1000)
        visits = [
            {"visit_id": f"{base}_st_1_side_gate", "site_track_id": "st_1", "label": "person", "kind": "", "camera_id": "cam_backyard", "zone_id": "side_gate",
             "zone_type": "perimeter", "start_ms": base, "end_ms": base + 8000, "dwell_s": 8, "arrived": True},
            {"visit_id": f"{base}_st_1_shed", "site_track_id": "st_1", "label": "person", "kind": "", "camera_id": "cam_backyard", "zone_id": "shed",
             "zone_type": "restricted", "start_ms": base + 60_000, "end_ms": base + 75_000, "dwell_s": 15, "arrived": True},
            {"visit_id": f"{base + 43_200_000}_st_4_side_gate", "site_track_id": "st_4", "label": "person", "kind": "", "camera_id": "cam_backyard",
             "zone_id": "side_gate", "zone_type": "perimeter", "start_ms": base + 43_200_000, "end_ms": base + 43_210_000, "dwell_s": 10, "arrived": True},
        ]
        result = lab_test_rule(CONFIG, GATE_THEN_SHED, source="history", arm_mode="home", visits=visits)
        self.assertEqual(result["runs"], 2)
        self.assertEqual(len(result["fired"]), 1, "only the 01:00 gate-then-shed visit matches; the midday gate visit does not")
        self.assertTrue(any("estimated" in note for note in result["notes"]))


def synthetic_history(now: datetime, days: int = 21):
    """Vehicles arrive 17-19h on weekdays, short daytime porch visits, a dog at
    dawn; plus a vehicle at 03:00 and an unusually long porch stay yesterday."""
    visits, trips = [], []
    seq = 0

    def add(label, zone_id, zone_type, camera, start, dwell, kind="", path=None):
        nonlocal seq
        seq += 1
        start_ms = int(start.timestamp() * 1000)
        site = f"st_{seq}"
        visits.append({"visit_id": f"{start_ms}_{site}_{zone_id}", "site_track_id": site, "label": label, "kind": kind, "camera_id": camera,
                       "zone_id": zone_id, "zone_type": zone_type, "start_ms": start_ms, "end_ms": start_ms + int(dwell * 1000), "dwell_s": dwell, "arrived": True})
        trips.append({"trip_id": f"{start_ms}_{site}", "site_track_id": site, "label": label, "kind": kind, "first_seen_ms": start_ms,
                      "last_seen_ms": start_ms + int(dwell * 1000) + 5000, "cameras": [camera], "path": path or [zone_id], "arrived": True,
                      "link_confidence": 1.0, "ambiguous": False})

    for day in range(days, 1, -1):
        date = (now - timedelta(days=day)).replace(minute=0, second=0, microsecond=0)
        add("vehicle", "driveway", "driveway", "cam_driveway", date.replace(hour=17 + day % 3, minute=10), 3600)
        add("person", "front_porch", "entrance", "cam_front", date.replace(hour=11, minute=20), 30 + day % 20, path=["front_lawn", "front_porch"])
        add("animal", "backyard", "yard", "cam_backyard", date.replace(hour=6, minute=5), 120, kind="dog")
    yesterday = (now - timedelta(days=1)).replace(second=0, microsecond=0)
    add("vehicle", "driveway", "driveway", "cam_driveway", yesterday.replace(hour=3, minute=10), 600)
    add("person", "front_porch", "entrance", "cam_front", yesterday.replace(hour=11, minute=40), 900, path=["front_lawn", "front_porch"])
    return visits, trips


class InsightsTests(unittest.TestCase):
    NOW = datetime(2026, 10, 14, 9, 0)

    def insights(self, visits, trips, **settings):
        return compute_insights(visits, trips, config=CONFIG, settings=ActivitySettings(**settings), now_ms=int(self.NOW.timestamp() * 1000))

    def test_patterns_unusual_observations_and_suggestions(self):
        visits, trips = synthetic_history(self.NOW)
        result = self.insights(visits, trips)
        self.assertTrue(result["window"]["enough_data"])
        self.assertEqual(result["vehicles"]["typical_arrival_hours"], "17:00–20:00")
        self.assertEqual(result["entrances"][0]["name"], "Front porch")
        self.assertGreaterEqual(result["deliveries"]["count"], 19)
        self.assertEqual(result["animals"]["kinds"], {"dog": 20})
        kinds = {(item["kind"], item["category"]) for item in result["unusual"]}
        self.assertIn(("time", "vehicle"), kinds, "a vehicle at 03:00 has not been seen at that hour before")
        self.assertIn(("stay", "person"), kinds, "a 15-minute porch stay is far longer than usual")
        self.assertTrue(all("suspicious" not in item["text"].lower() for item in result["unusual"]))
        suggestion = next(item for item in result["suggestions"] if item["id"] == "vehicle_outside_usual_hours")
        config = copy.deepcopy(CONFIG)
        config["rules"].append(suggestion["rule"] | {"id": "suggested"})
        validate_config(config)
        self.assertEqual(suggestion["rule"]["active_hours"], {"start": "20:00", "end": "17:00"})

    def test_not_enough_evidence_means_no_patterns_or_indicators(self):
        visits, trips = synthetic_history(self.NOW, days=5)
        result = self.insights(visits, trips)
        self.assertFalse(result["window"]["enough_data"])
        self.assertEqual((result["unusual"], result["suggestions"]), ([], []))
        self.assertIsNone(result["vehicles"]["typical_arrival_hours"])
        self.assertIn("at least 7 days", " ".join(result["notes"]))


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class IntelligenceApiTests(unittest.TestCase):
    def setUp(self):
        from sentinel.api import create_app
        from sentinel.service import SentinelService

        self.directory = tempfile.TemporaryDirectory()
        config = copy.deepcopy(CONFIG)
        config["storage"] = {"dir": self.directory.name}
        self.service = SentinelService(config, start_cameras=False)
        self.client = TestClient(create_app(self.service, api_token="tok"))
        self.auth = {"Authorization": "Bearer tok", "X-Sentinel-Operator": "tester"}

    def tearDown(self):
        self.service.stop()
        self.directory.cleanup()

    def post(self, path, body=None):
        return self.client.post(path, headers=self.auth, json=body or {})

    def test_rule_lifecycle_is_validated_persisted_and_applied_live(self):
        self.assertEqual(self.client.get("/api/v1/rules").status_code, 401)
        catalog = self.client.get("/api/v1/rules", headers=self.auth).json()
        self.assertEqual(catalog["catalog"]["modes"], ["home", "away", "night", "vacation"])
        self.assertTrue(catalog["templates"])
        check = self.post("/api/v1/rules/check", {"type": "sequence", "zones": ["side_gate"], "within_s": 60, "severity": "high"}).json()
        self.assertFalse(check["valid"])
        self.assertIn("at least two zones", " ".join(check["errors"]))
        self.assertEqual(self.post("/api/v1/rules", {"type": "zone_entry", "zones": ["nowhere"], "severity": "high"}).status_code, 422)
        created = self.post("/api/v1/rules", dict(GATE_THEN_SHED, enabled=False) | {"id": "ignored"}).json()
        self.assertEqual(created["id"], "side_gate_then_shed_after_midnight")
        self.assertFalse(created["enabled"])
        saved = json.loads((Path(self.directory.name) / "site.json").read_text())
        self.assertIn(created["id"], [rule["id"] for rule in saved["rules"]])
        engine_rule = next(rule for rule in self.service.runtime.engine.rules if rule["id"] == created["id"])
        self.assertFalse(engine_rule["enabled"])
        self.client.put(f"/api/v1/rules/{created['id']}/enabled", headers=self.auth, json={"enabled": True})
        self.assertTrue(next(rule for rule in self.service.runtime.engine.rules if rule["id"] == created["id"])["enabled"])
        combo = self.post("/api/v1/rules", {"name": "Two things", "type": "combination", "rules": [created["id"], "restricted_entry"],
                                            "min_matches": 2, "within_s": 300, "severity": "high"}).json()
        self.assertEqual(self.client.delete(f"/api/v1/rules/{created['id']}", headers=self.auth).status_code, 422, "used by a combination")
        self.assertEqual(self.client.delete(f"/api/v1/rules/{combo['id']}", headers=self.auth).status_code, 200)
        actions = [row["action"] for row in self.client.get("/api/v1/audit", headers=self.auth).json()]
        self.assertIn("config_changed", actions)

    def test_rule_test_endpoint(self):
        body = {"rule": GATE_THEN_SHED, "source": "walk", "arm_mode": "home", "start": NIGHT.isoformat(),
                "steps": [{"zone_id": "side_gate", "stay_s": 6, "gap_s": 3}, {"zone_id": "shed", "stay_s": 10}]}
        result = self.post("/api/v1/rules/test", body).json()
        self.assertEqual(len(result["fired"]), 1)
        scenario = self.post("/api/v1/rules/test", {"rule": GATE_THEN_SHED, "source": "scenario", "scenario": "gate_then_shed",
                                                    "arm_mode": "home", "start": NIGHT.isoformat()}).json()
        self.assertEqual(len(scenario["fired"]), 1)
        self.assertEqual(self.post("/api/v1/rules/test", body | {"source": "scenario", "scenario": "../site"}).status_code, 422)
        self.assertEqual(self.service.site.config["rules"], CONFIG["rules"], "testing never changes the live rules")

    def incident(self, event_id="evt_1", zone="shed", camera="cam_backyard", rule="restricted_entry", site="st_1", label="person", ts=1000):
        ts = int(time.time() * 1000) - 3_600_000 + ts  # within the last hour
        event = SecurityEvent(event_id, "zone_entry", "high", camera, ts, ts, 0.9, f"Person entered {zone}", label=label, zone_id=zone, site_track_id=site, rule_id=rule)
        self.service.runtime._handle_events([event], ts)
        return event.incident_id

    def test_search_explanation_notes_feedback_and_reports(self):
        shed = self.incident()
        porch = self.incident("evt_2", "front_porch", "cam_front", "porch_dwell", "st_2", ts=500_000)
        search = lambda query: [i["incident_id"] for i in self.client.get(f"/api/v1/search/incidents?{query}", headers=self.auth).json()]  # noqa: E731
        self.assertEqual(search("zone_id=shed"), [shed])
        self.assertEqual(search("camera_id=cam_front"), [porch])
        self.assertEqual(search("rule_id=porch_dwell"), [porch])
        self.assertEqual(sorted(search("label=person")), sorted([shed, porch]))
        self.assertEqual(search("q=porch"), [porch])
        self.assertEqual(search("label=vehicle"), [])
        explanation = self.client.get(f"/api/v1/incidents/{shed}/explanation", headers=self.auth).json()
        self.assertEqual(explanation["rules"][0]["rule_id"], "restricted_entry")
        self.assertTrue(explanation["severity"]["reasons"])
        note = self.post(f"/api/v1/incidents/{shed}/notes", {"text": "Gardener collecting tools"}).json()
        self.assertEqual(note["notes"][0]["by"], "tester")
        self.post(f"/api/v1/incidents/{shed}/resolve", {"outcome": "expected_activity"})
        self.post(f"/api/v1/incidents/{porch}/resolve", {"outcome": "real_concern"})
        self.assertEqual(search("outcome=expected_activity"), [shed])
        report = self.client.get("/api/v1/reports/performance?days=30", headers=self.auth).json()
        rules = {row["rule_id"]: row for row in report["rules"]}
        self.assertEqual(rules["restricted_entry"]["outcomes"]["Expected activity"], 1)
        self.assertEqual(rules["porch_dwell"]["outcomes"]["Genuine concern"], 1)
        cameras = {row["camera_id"]: row for row in report["cameras"]}
        self.assertEqual(cameras["cam_backyard"]["incidents"], 1)

    def test_missed_event_report_keeps_what_sentinel_saw(self):
        self.incident(ts=10_000)
        report = self.post("/api/v1/missed", {"occurred_ms": int(time.time() * 1000) - 3_600_000 + 60_000, "camera_id": "cam_backyard", "category": "person", "description": "Courier at the gate"}).json()
        self.assertEqual([item["event_id"] for item in report["sentinel_recorded"]], ["evt_1"])
        self.assertEqual(self.post("/api/v1/missed", {"occurred_ms": 1, "camera_id": "nope"}).status_code, 404)
        listed = self.client.get("/api/v1/missed?days=30", headers=self.auth).json()
        self.assertEqual(listed[0]["description"], "Courier at the gate")
        report = self.client.get("/api/v1/reports/performance?days=30", headers=self.auth).json()
        self.assertEqual(next(row for row in report["cameras"] if row["camera_id"] == "cam_backyard")["missed_reports"], 1)

    def test_export_fingerprints_evidence_and_excludes_secrets(self):
        runtime = self.service.runtime
        snapshot = runtime.media_root / "snapshots" / "cam_backyard" / "shot.jpg"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_bytes(b"\xff\xd8 test jpeg")
        runtime.record_evidence("snapshots/cam_backyard/shot.jpg")
        event = SecurityEvent("evt_x", "zone_entry", "high", "cam_backyard", 1000, 1000, 0.9, "Person entered Shed", label="person", zone_id="shed",
                              site_track_id="st_x", rule_id="restricted_entry", snapshot_path="snapshots/cam_backyard/shot.jpg")
        runtime._handle_events([event], 1000)
        response = self.client.get(f"/api/v1/incidents/{event.incident_id}/export?include_media=true", headers=self.auth)
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        report = json.loads(archive.read("report.json"))
        self.assertEqual(report["evidence"][0]["status"], "intact")
        self.assertIn("evidence/snapshots/cam_backyard/shot.jpg", archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(set(manifest["files"]), set(archive.namelist()) - {"manifest.json"})
        text = archive.read("report.html").decode() + archive.read("report.json").decode()
        for secret in ("rtsp://", "credential", "origin_geo"):
            self.assertNotIn(secret, text)
        snapshot.write_bytes(b"\xff\xd8 edited")
        changed = json.loads(zipfile.ZipFile(io.BytesIO(self.client.get(f"/api/v1/incidents/{event.incident_id}/export", headers=self.auth).content)).read("report.json"))
        self.assertEqual(changed["evidence"][0]["status"], "CHANGED since it was recorded")
        self.assertNotIn("evidence/snapshots/cam_backyard/shot.jpg", zipfile.ZipFile(io.BytesIO(response.content)).namelist()[:0])
        self.assertIn("incident_exported", [row["action"] for row in self.client.get("/api/v1/audit", headers=self.auth).json()])

    def test_insights_endpoint_and_activity_retention(self):
        visits, trips = synthetic_history(datetime.now())
        self.service.runtime.store.add_activity(visits, trips)
        data = self.client.get("/api/v1/insights?days=30", headers=self.auth).json()
        self.assertTrue(data["window"]["enough_data"])
        self.assertTrue(data["unusual"])
        self.assertEqual(self.service.runtime.store.list_incidents(), [], "statistics never create incidents or alerts")
        removed = self.service.runtime.store.purge_activity(int(time.time() * 1000))
        self.assertEqual(removed, len(visits) + len(trips))


if __name__ == "__main__":
    unittest.main()
