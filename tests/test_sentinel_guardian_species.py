"""Guardian species recognition: decision policy (never forces a species), taxonomy
roll-up, geofence, per-track aggregation, wording that never lowers concern, the
background worker's failure handling, and (when installed) the real SpeciesNet
ONNX model on a synthetic crop."""

from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path

from sentinel.guardian.risk import FORBIDDEN_CLAIMS, classified_profile, species_profile
from sentinel.guardian.species import (
    Geofence, SpeciesPolicy, SpeciesSettings, TrackSpecies, guardian_key, parse_labels,
)

LABELS = """\
u0;mammalia;carnivora;ursidae;ursus;americanus;american black bear
u1;mammalia;carnivora;ursidae;ursus;arctos horribilis;grizzly bear
u2;mammalia;carnivora;ursidae;ursus;arctos;brown bear
u3;mammalia;carnivora;ursidae;;;bear family
u4;mammalia;carnivora;canidae;canis;latrans;coyote
u5;mammalia;carnivora;canidae;canis;lupus;grey wolf
u6;mammalia;carnivora;canidae;canis;familiaris;domestic dog
u7;;;;;;blank
u8;mammalia;carnivora;felidae;puma;concolor;puma
u9;mammalia;artiodactyla;cervidae;cervus;canadensis;elk
u10;mammalia;carnivora;ursidae;ursus;thibetanus;asiatic black bear
""".splitlines()

MODEL_DIR = Path(__file__).resolve().parents[1] / "models/speciesnet/v4.0.3a"


def policy(**settings):
    return SpeciesPolicy(parse_labels(LABELS), SpeciesSettings(**({"min_agreeing": 1} | settings)))


class LabelTests(unittest.TestCase):
    def test_parse_and_keys(self):
        taxa = parse_labels(LABELS)
        self.assertEqual(taxa[0].level, "species")
        self.assertEqual(taxa[3].level, "family")
        self.assertEqual(taxa[7].level, "non_animal")
        self.assertEqual(guardian_key(taxa[0]), "black_bear")
        self.assertEqual(guardian_key(taxa[1]), "grizzly_bear")
        self.assertEqual(guardian_key(taxa[2]), "brown_bear")
        self.assertEqual(guardian_key(taxa[5]), "wolf")
        self.assertIsNone(guardian_key(taxa[10]))  # Asiatic black bear is not the American black bear
        self.assertIsNone(guardian_key(taxa[3]))
        with self.assertRaises(ValueError):
            parse_labels(["a;b;c"])


class PolicyTests(unittest.TestCase):
    def test_confident_species_is_confirmed_with_source_taxonomy(self):
        d = policy().decide({0: 0.92, 1: 0.05, 4: 0.01})
        self.assertEqual((d.status, d.key), ("confirmed", "black_bear"))
        self.assertAlmostEqual(d.confidence, 0.92)
        self.assertEqual(d.taxon["taxonomy"], "mammalia;carnivora;ursidae;ursus;americanus")
        self.assertEqual(d.top[0]["label"], "american black bear")

    def test_black_vs_grizzly_split_rolls_up_to_bear(self):
        d = policy().decide({0: 0.5, 1: 0.4, 2: 0.05})
        self.assertEqual(d.status, "uncertain")
        self.assertIsNone(d.key)
        self.assertEqual(d.display, "Bear detected — species uncertain")
        self.assertGreaterEqual(d.confidence, 0.9)
        self.assertTrue(any("below" in r or "runner-up" in r for r in d.reasons))

    def test_low_scores_are_unknown_not_forced(self):
        d = policy().decide({0: 0.2, 4: 0.2, 8: 0.2, 9: 0.2})
        self.assertEqual(d.status, "unknown")
        self.assertIsNone(d.key)

    def test_blank_is_unknown(self):
        self.assertEqual(policy().decide({7: 0.95, 0: 0.03}).status, "unknown")

    def test_margin_is_required(self):
        d = policy(min_confidence=0.4, min_margin=0.2).decide({4: 0.5, 5: 0.4})
        self.assertNotEqual(d.status, "confirmed")  # coyote vs wolf too close: canid group at most
        self.assertEqual(d.display, "Dog-like animal detected — species uncertain")

    def test_group_label_on_top_never_confirms(self):
        d = policy().decide({3: 0.95})
        self.assertEqual(d.status, "uncertain")

    def test_live_tracks_need_agreeing_crops(self):
        d = policy(min_agreeing=2).decide({0: 0.95}, crops=1)
        self.assertNotEqual(d.status, "confirmed")
        self.assertEqual(policy(min_agreeing=2).decide({0: 0.95}, crops=2).status, "confirmed")

    def test_geofence_blocks_out_of_range_species(self):
        taxa = parse_labels(LABELS)
        fence = Geofence({"mammalia;carnivora;ursidae;ursus;arctos horribilis": {"allow": {"USA": ["AK", "MT"], "CAN": []}}})
        p = SpeciesPolicy(taxa, SpeciesSettings(min_agreeing=1, country="GBR"), fence)
        self.assertNotEqual(p.decide({1: 0.95}).status, "confirmed")
        p = SpeciesPolicy(taxa, SpeciesSettings(min_agreeing=1, country="CAN"), fence)
        self.assertEqual(p.decide({1: 0.95}).status, "confirmed")
        p = SpeciesPolicy(taxa, SpeciesSettings(min_agreeing=1, country="USA", admin1_region="FL"), fence)
        self.assertNotEqual(p.decide({1: 0.95}).status, "confirmed")
        self.assertTrue(Geofence().allowed(taxa[1], "GBR"))
        block = Geofence({"mammalia;carnivora;canidae;canis;lupus": {"block": {"GBR": []}}})
        self.assertFalse(block.allowed(taxa[5], "GBR"))


class TrackSpeciesTests(unittest.TestCase):
    def test_aggregates_and_reclassifies_periodically(self):
        settings = SpeciesSettings(reclassify_s=4, max_crops_per_track=3, min_agreeing=2)
        p = SpeciesPolicy(parse_labels(LABELS), settings)
        state = TrackSpecies()
        self.assertTrue(state.due(0, settings))
        state.pending, state.last_request_ms = True, 0
        self.assertFalse(state.due(10_000, settings))  # one request in flight at a time
        first = state.add({0: 0.9, 1: 0.05}, p)
        self.assertNotEqual(first.status, "confirmed")  # one crop is not enough on a live track
        self.assertFalse(state.due(1000, settings))
        self.assertTrue(state.due(4000, settings))
        second = state.add({0: 0.88, 1: 0.07}, p)
        self.assertEqual(second.status, "confirmed")
        self.assertEqual(second.crops, 2)
        state.add({0: 0.9}, p)
        self.assertFalse(state.due(100_000, settings))  # crop budget per track spent

    def test_flip_flopping_track_stays_unconfirmed(self):
        settings = SpeciesSettings(min_agreeing=2)
        p = SpeciesPolicy(parse_labels(LABELS), settings)
        state = TrackSpecies()
        state.add({4: 0.9, 6: 0.05}, p)
        decision = state.add({6: 0.9, 4: 0.05}, p)
        self.assertNotEqual(decision.status, "confirmed")


class WordingTests(unittest.TestCase):
    def test_confirmed_wording_and_never_lower_concern(self):
        bear = policy().decide({0: 0.91})
        profile = classified_profile("bear", bear)
        self.assertEqual(profile.display, "Black bear — 91% species confidence")
        self.assertEqual(profile.status, "classifier")
        dog = policy().decide({6: 0.95})
        profile = classified_profile("dog", dog)
        self.assertEqual(profile.concern, species_profile("dog").concern)  # "Dog" never quietens a dog-like detection
        self.assertTrue(profile.display.startswith("Dog —"))

    def test_uncertain_keeps_detector_concern(self):
        unsure = policy().decide({0: 0.5, 1: 0.45})
        profile = classified_profile("bear", unsure)
        self.assertEqual(profile.display, "Bear detected — species uncertain")
        self.assertEqual(profile.concern, species_profile("bear").concern)
        self.assertEqual(classified_profile("bear", None), species_profile("bear"))

    def test_no_forbidden_claims(self):
        for scores in ({0: 0.95}, {0: 0.5, 1: 0.45}, {8: 0.99}, {5: 0.9}):
            text = classified_profile("dog", policy().decide(scores)).display.lower()
            for word in FORBIDDEN_CLAIMS:
                self.assertNotIn(word, text)


class FakeModel:
    version = "test"
    provider = "CPUExecutionProvider"
    load_ms = 1.0

    def __init__(self, fail_on=None):
        self.fail_on = fail_on

    def classify(self, crops):
        if self.fail_on is not None and crops[0] == self.fail_on:
            raise RuntimeError("inference blew up")
        return [{0: 0.9}]


class WorkerTests(unittest.TestCase):
    def make(self, factory, queue_size=4):
        from sentinel.guardian.species_onnx import SpeciesWorker

        events = {"results": [], "failures": [], "ready": threading.Event(), "errors": []}
        worker = SpeciesWorker(
            SpeciesSettings(queue_size=queue_size),
            on_result=lambda job, scores: events["results"].append((job.guardian_id, scores)),
            on_failure=lambda job: events["failures"].append(job.guardian_id),
            on_ready=lambda model: events["ready"].set(),
            on_error=lambda error: (events["errors"].append(error), events["ready"].set()),
            model_factory=factory,
        )
        events["ready"].wait(5)
        return worker, events

    def wait(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.01)

    def test_results_and_contained_inference_errors(self):
        from sentinel.guardian.species_onnx import SpeciesJob

        worker, events = self.make(lambda: FakeModel(fail_on="bad"))
        worker.submit(SpeciesJob("cam", "g1", "ok", 0))
        worker.submit(SpeciesJob("cam", "g2", "bad", 0))
        worker.submit(SpeciesJob("cam", "g3", "ok", 0))
        self.wait(lambda: len(events["results"]) + len(events["failures"]) == 3)
        self.assertEqual([g for g, _ in events["results"]], ["g1", "g3"])
        self.assertEqual(events["failures"], ["g2"])
        self.assertEqual(worker.status()["state"], "ready")
        self.assertEqual(worker.status()["failures"], 1)
        worker.stop()

    def test_missing_model_reports_and_refuses_jobs(self):
        from sentinel.guardian.species_onnx import SpeciesJob

        def broken():
            raise FileNotFoundError("speciesnet.onnx missing")

        worker, events = self.make(broken)
        self.assertEqual(worker.status()["state"], "failed")
        self.assertIn("missing", events["errors"][0])
        self.assertFalse(worker.submit(SpeciesJob("cam", "g1", "ok", 0)))
        worker.stop()

    def test_full_queue_drops_instead_of_blocking(self):
        from sentinel.guardian.species_onnx import SpeciesJob

        gate = threading.Event()

        class Slow(FakeModel):
            def classify(self, crops):
                gate.wait(5)
                return [{0: 0.9}]

        worker, events = self.make(lambda: Slow(), queue_size=1)
        accepted = [worker.submit(SpeciesJob("cam", f"g{i}", "x", 0)) for i in range(5)]
        self.assertIn(False, accepted)
        self.assertGreater(worker.status()["dropped"], 0)
        gate.set()
        worker.stop()


@unittest.skipUnless((MODEL_DIR / "speciesnet.onnx").exists(), "SpeciesNet ONNX not installed")
class RealModelTests(unittest.TestCase):
    def test_loads_and_scores_a_crop(self):
        import numpy as np

        from sentinel.guardian.species_onnx import SpeciesNetOnnx

        model = SpeciesNetOnnx(MODEL_DIR, "cpu")
        self.assertEqual(len(model.taxa), 2498)
        image = np.full((400, 600, 3), 90, dtype=np.uint8)
        crop = model.preprocess(image, (0.2, 0.2, 0.4, 0.5))
        self.assertEqual(crop.shape, (480, 480, 3))
        self.assertIsNone(model.preprocess(image, (0.5, 0.5, 0.01, 0.01), min_px=48))
        scores = model.classify([crop])[0]
        self.assertEqual(len(scores), 10)
        self.assertLessEqual(sum(scores.values()), 1.0 + 1e-5)  # top-10 of a softmax
        decision = SpeciesPolicy(model.taxa, SpeciesSettings(min_agreeing=1), model.geofence).decide(scores)
        self.assertNotEqual(decision.status, "confirmed")  # a grey square is not a species


class MonitorIntegrationTests(unittest.TestCase):
    """Species decisions flow into tracking IDs, wording, events, overlays and the API view."""

    def run_track(self, scores, seconds=8.0, label="bear", approach=False, **species):
        from sentinel.guardian.monitor import GuardianMonitor
        from sentinel.tracker import IoUTracker
        from test_sentinel_guardian import FPS, NOON_MS, box, guardian_config

        config = guardian_config(species_classifier={"enabled": True, "reclassify_s": 1, **species})
        monitor = GuardianMonitor(config)
        monitor.attach_species(SpeciesPolicy(parse_labels(LABELS), monitor.species_settings))
        tracker = IoUTracker("cam_a")
        events, requested = [], []
        for step in range(int(seconds * FPS)):
            now = NOON_MS + step * 1000 // FPS
            grow = step / FPS if approach else 0.0  # approaching the camera: lower and larger in the image
            active, ended = tracker.update([box(0.2 + 0.06 * step / FPS, 0.75 - 0.15 * approach + 0.02 * grow, 0.12 + 0.01 * grow, label=label)], now)
            events += monitor.process("cam_a", now, active, ended)
            for guardian_id, bbox in monitor.species_requests("cam_a", now):
                requested.append(now)
                monitor.species_result(guardian_id, scores)
        return monitor, events, requested

    def test_confirmed_species_reaches_events_overlay_and_view(self):
        monitor, events, requested = self.run_track({0: 0.93, 1: 0.03})
        self.assertLessEqual(len(requested), monitor.species_settings.max_crops_per_track)
        self.assertGreaterEqual(min(b - a for a, b in zip(requested, requested[1:])), 1000)  # periodic, not every frame
        later = [e for e in events if "species confidence" in e.summary]
        self.assertTrue(later, [e.summary for e in events])
        self.assertTrue(later[-1].summary.startswith("Black bear — 93% species confidence — "))
        species = later[-1].details["species"]
        self.assertTrue(species["species_specific_guidance_allowed"])
        self.assertEqual(species["classifier"]["taxon"]["taxonomy"], "mammalia;carnivora;ursidae;ursus;americanus")
        view = monitor.snapshot()[0]
        self.assertEqual(view["species"]["status"], "classifier")
        self.assertEqual(view["species"]["species_confidence"], 0.93)
        self.assertTrue(monitor.overlays("cam_a")[0]["label"].startswith("Black bear 93%"))

    def test_blank_classifier_sets_aside_a_generic_camera_trap_box(self):
        # MegaDetector's generic "animal" box on a rock, approaching in image terms: once two
        # crops agree it is blank, the track gets no level, no event and no overlay.
        monitor, events, requested = self.run_track({7: 0.93, 0: 0.02}, label="animal", approach=True)
        view = monitor.snapshot()[0]
        self.assertTrue(view["set_aside"])
        self.assertIsNone(view["assessment"])
        self.assertEqual(monitor.overlays("cam_a"), [])
        self.assertFalse([e for e in events if e.timestamp_ms > requested[1]], "nothing after the second blank crop")
        monitor, _, requested = self.run_track({7: 0.93, 0: 0.02}, label="animal", approach=True, reclassify_s=4)
        self.assertEqual(requested[1] - requested[0], 1000, "a blank-looking generic box is re-checked after 1 s, not 4 s")
        monitor, _, requested = self.run_track({0: 0.93}, label="animal", reclassify_s=4)
        self.assertEqual(requested[1] - requested[0], 4000, "an animal-looking box keeps the normal interval")

    def test_confident_confirmation_stops_reclassifying(self):
        _, _, settled = self.run_track({0: 0.97, 1: 0.01}, seconds=12.0)
        self.assertEqual(len(settled), 2, "two agreeing crops at 0.97 settle the species")
        _, _, unsettled = self.run_track({0: 0.90, 1: 0.03}, seconds=12.0)
        self.assertGreater(len(unsettled), 2, "a confirmation below settle_confidence keeps being re-checked")
        _, _, off = self.run_track({0: 0.97, 1: 0.01}, seconds=12.0, settle_confidence=1)
        self.assertGreater(len(off), 2)

    def test_blank_never_quietens_a_specific_detector_label(self):
        monitor, events, _ = self.run_track({7: 0.95}, label="bear", approach=True)
        view = monitor.snapshot()[0]
        self.assertEqual(view["set_aside"], "")
        self.assertTrue(monitor.overlays("cam_a"))
        self.assertEqual(view["species"]["concern"], "high", "a COCO 'bear' keeps its concern whatever the classifier says")

    def test_one_blank_crop_or_a_weak_blank_is_not_enough(self):
        for scores, species in (({7: 0.93}, {"max_crops_per_track": 1}), ({7: 0.6, 0: 0.3}, {}), ({7: 1.0}, {"blank_confidence": 1})):
            with self.subTest(scores=scores):
                monitor, _, _ = self.run_track(scores, label="animal", min_agreeing=2, **species)
                self.assertEqual(monitor.snapshot()[0]["set_aside"], "")
                self.assertTrue(monitor.overlays("cam_a"))

    def test_uncertain_species_never_allows_species_guidance(self):
        monitor, events, _ = self.run_track({0: 0.5, 1: 0.45})
        view = monitor.snapshot()[0]["species"]
        self.assertEqual(view["display"], "Bear detected — species uncertain")
        self.assertFalse(view["species_specific_guidance_allowed"])
        self.assertIsNone(view["species_confidence"])
        self.assertTrue(all("species confidence" not in e.summary for e in events))
        self.assertTrue(monitor.overlays("cam_a")[0]["label"].startswith("Bear?"))
        for event in events:
            self.assertFalse(event.details["species"]["species_specific_guidance_allowed"])

    def test_classifier_cannot_lower_alert_levels(self):
        _, with_dog, _ = self.run_track({6: 0.97})  # classifier says "domestic dog" about a detector "bear"
        _, without, _ = self.run_track({7: 0.99})  # "blank": no decision changes anything
        rank = {"approaching": 1, "elevated_concern": 2, "urgent_review": 3}
        self.assertEqual(max(rank[e.details["level"]] for e in with_dog), max(rank[e.details["level"]] for e in without))

    def test_no_classifier_keeps_detector_wording(self):
        from sentinel.guardian.monitor import GuardianMonitor
        from test_sentinel_guardian import guardian_config

        monitor = GuardianMonitor(guardian_config(species_classifier={"enabled": True}))
        self.assertEqual(monitor.species_requests("cam_a", 0), [])  # nothing attached: nothing requested

    def test_config_validation(self):
        from sentinel.config import validate_config
        from test_sentinel_guardian import guardian_config

        validate_config(guardian_config(species_classifier={"enabled": True, "country": "CAN", "min_confidence": 0.8}))
        bad = guardian_config(species_classifier={"enabled": "yes", "min_confidence": 2, "country": "Canada", "colour": 1, "min_agreeing": 0})
        with self.assertRaises(Exception) as caught:
            validate_config(bad)
        text = str(caught.exception)
        for fragment in ("enabled", "min_confidence", "country", "unknown keys", "min_agreeing"):
            self.assertIn(fragment, text)


class RuntimeSpeciesTests(unittest.TestCase):
    def frames(self, runtime, seconds=6.0):
        import numpy as np

        from sentinel.tracker import IoUTracker
        from test_sentinel_guardian import FPS, NOON_MS, box

        tracker = IoUTracker("cam_a")
        image = np.full((360, 640, 3), 80, dtype=np.uint8)
        events = []
        for step in range(int(seconds * FPS)):
            now = NOON_MS + step * 1000 // FPS
            active, ended = tracker.update([box(0.2 + 0.06 * step / FPS, 0.75, 0.3)], now)
            events += runtime.process_camera_update("cam_a", now, active, ended, image=image)
            runtime.guardian_species_frame("cam_a", now, image)
            time.sleep(0.02)
        return events

    def test_runtime_classifies_tracked_animal_in_background(self):
        import tempfile
        from unittest import mock

        from sentinel.guardian import species_onnx
        from sentinel.notify import CallbackNotifier
        from sentinel.runtime import SentinelRuntime
        from test_sentinel_guardian import guardian_config

        class Fake(species_onnx.SpeciesNetOnnx):
            provider = "CPUExecutionProvider"

            def __init__(self, *args, **kwargs):
                self.taxa, self.geofence, self.version, self.load_ms, self.session = parse_labels(LABELS), Geofence(), "fake", 0.0, None

            def classify(self, crops):
                assert crops[0].shape == (480, 480, 3)
                return [{0: 0.95, 1: 0.02}]

        config = guardian_config(species_classifier={"enabled": True, "reclassify_s": 0.4, "min_box_px": 8})
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(species_onnx, "SpeciesNetOnnx", Fake):
            runtime = SentinelRuntime(config, storage_dir=folder, notifiers=[CallbackNotifier("capture", lambda n: None)])
            try:
                events = self.frames(runtime)
                state = runtime.snapshot_state()["guardian"]
                self.assertEqual(state["species_classifier"]["state"], "ready")
                self.assertGreaterEqual(state["species_classifier"]["classified"], 2)
                self.assertEqual(state["tracks"][0]["species"]["status"], "classifier")
                self.assertEqual(runtime.health.components["guardian_species"]["state"], "ok")
                self.assertTrue(any("Black bear — 95% species confidence" in e.summary for e in events),
                                [e.summary for e in events])
            finally:
                runtime.close()

    def test_missing_model_degrades_quietly_and_guardian_keeps_working(self):
        import tempfile

        from sentinel.notify import CallbackNotifier
        from sentinel.runtime import SentinelRuntime
        from test_sentinel_guardian import guardian_config

        with tempfile.TemporaryDirectory() as folder:
            config = guardian_config(species_classifier={"enabled": True, "model_dir": str(Path(folder) / "nowhere")})
            runtime = SentinelRuntime(config, storage_dir=folder, notifiers=[CallbackNotifier("capture", lambda n: None)])
            try:
                deadline = time.monotonic() + 5
                while "guardian_species" not in runtime.health.components and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(runtime.health.components["guardian_species"]["state"], "degraded")
                events = self.frames(runtime, seconds=8)
                wildlife = [e for e in events if e.event_type.startswith("wildlife_")]
                self.assertTrue(wildlife)  # movement alerts unaffected
                self.assertTrue(all("species confidence" not in e.summary for e in wildlife))
                self.assertEqual(runtime.snapshot_state()["guardian"]["species_classifier"]["state"], "failed")
            finally:
                runtime.close()

    def test_disabling_in_config_stops_the_worker(self):
        import tempfile

        from sentinel.notify import CallbackNotifier
        from sentinel.runtime import SentinelRuntime
        from test_sentinel_guardian import guardian_config

        with tempfile.TemporaryDirectory() as folder:
            config = guardian_config(species_classifier={"enabled": True, "model_dir": str(Path(folder) / "nowhere")})
            runtime = SentinelRuntime(config, storage_dir=folder, notifiers=[CallbackNotifier("capture", lambda n: None)])
            try:
                self.assertIsNotNone(runtime.species)
                runtime.reconfigure(guardian_config())
                self.assertIsNone(runtime.species)
                self.assertNotIn("guardian_species", runtime.health.components)
                self.assertEqual(runtime.snapshot_state()["guardian"]["species_classifier"], {"enabled": False})
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
