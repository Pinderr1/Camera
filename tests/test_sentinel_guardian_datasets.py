"""Guardian dataset importer: registry validation, label honesty, splits,
duplicates, budget and licence gating. Offline: a fake fetcher serves bytes."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from sentinel.guardian import datasets as ds

TARGETS = ["black_bear", "grizzly_bear", "elk", "coyote"]


def _source(**overrides):
    entry = {
        "id": "toy",
        "name": "Toy camera traps",
        "page": "https://example.org/toy",
        "version": "1",
        "format": "coco_camera_traps",
        "metadata_url": "https://example.org/toy/meta.json.zip",
        "image_base_url": "https://example.org/toy/images",
        "licence": {"id": "CDLA-Permissive-1.0", "url": "https://cdla.io/permissive-1-0/",
                    "commercial_use": "allowed", "conditions": "keep licence text",
                    "attribution": "Toy et al."},
        "label_map": {
            "ursus americanus": {"target": "black_bear", "status": "species"},
            "cervus elaphus": {"target": "elk", "status": "uncertain"},
            "canis latrans": {"target": "coyote", "status": "species"},
            "empty": {"status": "negative"},
            "human": {"status": "excluded"},
        },
    }
    entry.update(overrides)
    return entry


def _metadata(images):
    """images: list of (id, file, location, [labels])"""
    names = sorted({label for *_, labels in images for label in labels})
    cats = [{"id": i, "name": n} for i, n in enumerate(names)]
    ids = {n: i for i, n in enumerate(names)}
    anns = []
    for image_id, _, _, labels in images:
        for k, label in enumerate(labels):
            anns.append({"id": f"{image_id}-{k}", "image_id": image_id, "category_id": ids[label],
                         "bbox": [1, 2, 3, 4]})
    return {
        "info": {"version": "1"},
        "categories": cats,
        "images": [{"id": i, "file_name": f, "location": loc, "width": 64, "height": 48}
                   for i, f, loc, _ in images],
        "annotations": anns,
    }


def _zip_json(obj) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("meta.json", json.dumps(obj))
    return buf.getvalue()


class FakeWeb:
    def __init__(self, files):
        self.files = files
        self.requests = []

    def __call__(self, url):
        self.requests.append(url)
        if url not in self.files:
            raise OSError(f"404 {url}")
        return self.files[url]


class RegistryTests(unittest.TestCase):
    def test_project_registry_is_valid_and_honest(self):
        registry = ds.load_registry(Path(__file__).resolve().parents[1] / "config/guardian/datasets.json")
        ids = {s.id for s in registry.sources}
        self.assertTrue({"ena24", "caltech_camera_traps", "nacti", "wcs", "animal_kingdom"} <= ids)
        self.assertFalse(registry.get("animal_kingdom").commercial)
        self.assertFalse(registry.get("animal_kingdom").fetchable)
        self.assertFalse(registry.get("idaho_camera_traps").commercial)
        # Red deer is never silently turned into a confirmed elk.
        self.assertEqual(registry.get("nacti").label_map["cervus elaphus"].status, "uncertain")
        for source in registry.sources:
            for rule in source.label_map.values():
                if rule.target == "grizzly_bear":
                    self.fail("no registered source labels grizzly bears")

    def test_rejects_bad_entries(self):
        base = {"targets": TARGETS}
        with self.assertRaises(ds.DatasetError):
            ds.parse_registry({**base, "sources": [_source(label_map={"x": {"status": "maybe"}})]})
        with self.assertRaises(ds.DatasetError):
            ds.parse_registry({**base, "sources": [_source(label_map={"x": {"status": "species"}})]})
        with self.assertRaises(ds.DatasetError):
            ds.parse_registry({**base, "sources": [_source(label_map={"x": {"status": "negative", "target": "elk"}})]})
        with self.assertRaises(ds.DatasetError):
            ds.parse_registry({**base, "sources": [_source(format="mystery")]})
        licence = dict(_source()["licence"])
        del licence["attribution"]
        with self.assertRaises(ds.DatasetError):
            ds.parse_registry({**base, "sources": [_source(licence=licence)]})
        with self.assertRaises(ds.DatasetError):
            ds.parse_registry({**base, "sources": [_source(), _source()]})


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.source = ds.parse_registry({"targets": TARGETS, "sources": [_source()]}).sources[0]

    def image(self, labels, **kw):
        return ds.SourceImage(dataset="toy", image_id="1", file_name="a.jpg", labels=labels,
                              annotations=[], **kw)

    def test_buckets(self):
        self.assertEqual(ds.classify(self.image(["ursus americanus"]), self.source).bucket, "black_bear")
        self.assertEqual(ds.classify(self.image(["cervus elaphus"]), self.source).bucket, "uncertain:elk")
        self.assertEqual(ds.classify(self.image(["empty"]), self.source).bucket, "negative")

    def test_never_guesses(self):
        self.assertIsNone(ds.classify(self.image(["ursus arctos?"]), self.source))  # unmapped
        self.assertIsNone(ds.classify(self.image(["canis latrans", "human"]), self.source))  # excluded
        self.assertIsNone(ds.classify(self.image(["ursus americanus", "canis latrans"]), self.source))
        self.assertIsNone(ds.classify(self.image(["ursus americanus", "cervus elaphus"]), self.source))
        self.assertIsNone(ds.classify(self.image([]), self.source))
        self.assertIsNone(ds.classify(self.image(["empty"], extra={"corrupt": True}), self.source))

    def test_source_label_is_kept(self):
        cand = ds.classify(self.image(["cervus elaphus"]), self.source)
        self.assertEqual(cand.guardian_labels[0]["source_label"], "cervus elaphus")
        self.assertEqual(cand.guardian_labels[0]["status"], "uncertain")


class SplitAndSelectTests(unittest.TestCase):
    def test_split_by_location_is_stable_and_never_leaks(self):
        a = ds.SourceImage("toy", "1", "a.jpg", ["empty"], [], location="cam7")
        b = ds.SourceImage("toy", "2", "b.jpg", ["empty"], [], location="cam7")
        self.assertEqual(ds.split_for(a), ds.split_for(b))
        self.assertEqual(ds.split_for(a)[1], "location")
        none = ds.SourceImage("toy", "3", "c.jpg", ["empty"], [])
        self.assertEqual(ds.split_for(none)[0], "unassigned")
        seq = ds.SourceImage("toy", "4", "d.jpg", ["empty"], [], seq_id="s1")
        self.assertEqual(ds.split_for(seq)[1], "sequence")

    def test_select_spreads_over_locations(self):
        cands = []
        for loc in ("a", "b", "c"):
            for i in range(10):
                img = ds.SourceImage("toy", f"{loc}{i}", f"{loc}{i}.jpg", ["empty"], [], location=loc)
                cands.append(ds.Candidate(img, "negative", []))
        chosen = ds.select(cands, {"negative": 6})
        self.assertEqual(len(chosen), 6)
        self.assertEqual(sorted(c.image.location for c in chosen), ["a", "a", "b", "b", "c", "c"])
        self.assertEqual(ds.select(cands, {"negative": 100}).__len__(), 30)


class PipelineTests(unittest.TestCase):
    def make(self, tmp, images, files, **source_overrides):
        meta = _metadata(images)
        web = {"https://example.org/toy/meta.json.zip": _zip_json(meta)}
        for name, payload in files.items():
            web[f"https://example.org/toy/images/{name}"] = payload
        registry = ds.parse_registry({"targets": TARGETS, "sources": [_source(**source_overrides)]})
        return registry, FakeWeb(web)

    def test_end_to_end_with_duplicates_budget_and_provenance(self):
        images = [
            ("1", "loc1/bear1.jpg", "loc1", ["ursus americanus"]),
            ("2", "loc2/bear2.jpg", "loc2", ["ursus americanus"]),
            ("3", "loc3/bear3.jpg", "loc3", ["ursus americanus"]),  # same bytes as bear1
            ("4", "loc1/empty.jpg", "loc1", ["empty"]),
            ("5", "loc1/person.jpg", "loc1", ["human"]),
            ("6", "loc4/deer.jpg", "loc4", ["cervus elaphus"]),
            ("7", "loc5/missing.jpg", "loc5", ["canis latrans"]),
            ("8", "loc5/odd.jpg", "loc5", ["ursus arctos"]),
        ]
        files = {"loc1/bear1.jpg": b"A" * 100, "loc2/bear2.jpg": b"B" * 100, "loc3/bear3.jpg": b"A" * 100,
                 "loc1/empty.jpg": b"E" * 100, "loc1/person.jpg": b"P" * 100, "loc4/deer.jpg": b"D" * 100}
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            registry, web = self.make(tmp, images, files)
            quotas = {"black_bear": 10, "negative": 10, "uncertain:elk": 10, "coyote": 10}
            plan = ds.build_plan(registry, quotas, tmp / "cache", fetch=web)
            self.assertEqual(plan.unmapped_labels["toy"], {"ursus arctos": 1})
            summary = ds.fetch_images(plan, registry, tmp / "out", max_bytes=10_000,
                                      cache_root=tmp / "cache", fetch=web, log=lambda _: None)
            self.assertNotIn("https://example.org/toy/images/loc1/person.jpg", web.requests)
            self.assertEqual(summary["images"], 4)
            self.assertEqual(len(summary["duplicates"]), 1)
            self.assertEqual(len(summary["failures"]), 1)
            self.assertEqual(summary["split_leaks"], [])
            self.assertEqual(summary["buckets"]["grizzly_bear"]["images"], 0)
            records = [json.loads(line) for line in (tmp / "out/manifest.jsonl").read_text().splitlines()]
            deer = next(r for r in records if r["bucket"] == "uncertain:elk")
            self.assertEqual(deer["source_labels"], ["cervus elaphus"])
            self.assertEqual(deer["guardian_labels"][0]["status"], "uncertain")
            self.assertEqual(deer["source_annotations"][0]["bbox"], [1, 2, 3, 4])
            self.assertEqual(deer["licence"], "CDLA-Permissive-1.0")
            self.assertTrue((tmp / "out" / deer["path"]).exists())
            self.assertIn("Toy et al.", (tmp / "out/ATTRIBUTION.md").read_text())
            # Resumable: a second run downloads nothing new.
            before = len(web.requests)
            ds.fetch_images(plan, registry, tmp / "out", max_bytes=10_000, cache_root=tmp / "cache",
                            fetch=web, log=lambda _: None)
            new = [u for u in web.requests[before:] if "/images/" in u]
            dropped = {"toy:1": "loc1/bear1.jpg", "toy:3": "loc3/bear3.jpg"}[summary["duplicates"][0]["key"]]
            self.assertEqual(sorted(new), sorted([f"https://example.org/toy/images/{dropped}",
                                                  "https://example.org/toy/images/loc5/missing.jpg"]))

    def test_budget_stops_downloads(self):
        images = [(str(i), f"l{i}/x{i}.jpg", f"l{i}", ["empty"]) for i in range(5)]
        files = {f"l{i}/x{i}.jpg": bytes([i]) * 400 for i in range(5)}
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            registry, web = self.make(tmp, images, files)
            plan = ds.build_plan(registry, {"negative": 5}, tmp / "cache", fetch=web)
            summary = ds.fetch_images(plan, registry, tmp / "out", max_bytes=1000,
                                      cache_root=tmp / "cache", fetch=web, log=lambda _: None)
            self.assertEqual(summary["images"], 2)
            self.assertLessEqual(summary["bytes"], 1000)

    def test_research_only_sources_are_skipped_by_default(self):
        licence = dict(_source()["licence"], commercial_use="unknown")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            registry, web = self.make(tmp, [("1", "a.jpg", "l", ["empty"])], {"a.jpg": b"x"}, licence=licence)
            plan = ds.build_plan(registry, {"negative": 5}, tmp / "cache", fetch=web)
            self.assertEqual(plan.chosen, [])
            self.assertIn("toy", plan.skipped_sources)
            self.assertEqual(web.requests, [])
            plan = ds.build_plan(registry, {"negative": 5}, tmp / "cache", fetch=web, allow_research_only=True)
            self.assertEqual(len(plan.chosen), 1)

    def test_unsafe_file_names_are_refused(self):
        for bad in ("../../etc/passwd", "/abs/x.jpg", "a/../../b.jpg"):
            with self.assertRaises(ds.DatasetError):
                ds.safe_relative(bad)

    def test_megadetector_results_are_marked_machine_generated(self):
        md = {"info": {"detector": "md_v5a.0.0.pt"}, "detection_categories": {"1": "animal"},
              "images": [{"file": "l/a.jpg", "detections": [{"category": "1", "conf": 0.9, "bbox": [0, 0, .5, .5]}]}]}
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            registry, web = self.make(tmp, [("1", "l/a.jpg", "l", ["empty"])], {"l/a.jpg": b"x"},
                                      md_results_url="https://example.org/toy/md.json")
            web.files["https://example.org/toy/md.json"] = json.dumps(md).encode()
            plan = ds.build_plan(registry, {"negative": 5}, tmp / "cache", fetch=web)
            ds.fetch_images(plan, registry, tmp / "out", max_bytes=1000, cache_root=tmp / "cache",
                            fetch=web, log=lambda _: None)
            rec = json.loads((tmp / "out/manifest.jsonl").read_text().splitlines()[0])
            self.assertEqual(rec["megadetector"]["detections"][0]["category"], "animal")
            self.assertIn("not verified", rec["megadetector"]["status"])


if __name__ == "__main__":
    unittest.main()
