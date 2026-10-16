"""Streaming CSV metadata (NACTI-style): row parsing straight from a zip, merged
multi-species rows, the same honest label rules, and an end-to-end fetch."""

from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from sentinel.guardian import dataset_stream as stream
from sentinel.guardian import datasets as ds

HEADER = ["seq_no", "id", "filename", "study", "location", "width", "height", "category_id", "name", "genus", "family", "order", "class", "common_name"]


def _csv_zip(path: Path, rows: list[list[str]]) -> Path:
    text = io.StringIO()
    writer = csv.writer(text)
    writer.writerow(HEADER)
    writer.writerows(rows)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("meta.csv", text.getvalue())
    return path


def _row(i, image_id, name, location="Area A"):
    return [str(i), image_id, f"part0/{image_id}", "STUDY", location, "64", "48", "1", name, "", "", "", "", name]


def _registry():
    return ds.parse_registry({"targets": ["moose", "elk", "black_bear"], "sources": [{
        "id": "toy", "name": "Toy", "page": "https://example.org", "version": "1", "format": "coco_camera_traps",
        "metadata_url": "https://example.org/meta.json", "image_base_url": "https://example.org/img",
        "licence": {"id": "CDLA-Permissive-1.0", "commercial_use": "allowed", "conditions": "c", "attribution": "a"},
        "label_map": {"alces alces": {"target": "moose", "status": "species"},
                      "cervus elaphus": {"target": "elk", "status": "uncertain"},
                      "empty": {"status": "negative"}, "vehicle": {"status": "excluded"}},
    }]})


class StreamTests(unittest.TestCase):
    def test_rows_merge_and_labels_stay_honest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _csv_zip(Path(tmp) / "m.csv.zip", [
                _row(0, "a.jpg", "alces alces"),
                _row(1, "b.jpg", "cervus elaphus"),
                _row(2, "c.jpg", "alces alces"), _row(3, "c.jpg", "vehicle"),  # two rows, one image
                _row(4, "d.jpg", "ursus arctos"),  # unmapped
            ])
            images = list(stream.iter_lila_csv("toy", path))
            self.assertEqual([i.image_id for i in images], ["a.jpg", "b.jpg", "c.jpg", "d.jpg"])
            self.assertEqual(images[2].labels, ["alces alces", "vehicle"])
            self.assertEqual(images[0].location, "Area A")
            source = _registry().sources[0]
            plan = stream.plan_from_images(source, iter(images), {"moose": 5, "uncertain:elk": 5})
            self.assertEqual(sorted(c.bucket for c in plan.chosen), ["moose", "uncertain:elk"])  # c excluded, d unmapped
            self.assertEqual(plan.unmapped_labels["toy"], {"ursus arctos": 1})
            self.assertEqual(plan.available["moose"]["toy"], 1)

    def test_missing_columns_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.csv"
            path.write_text("a,b\n1,2\n", encoding="utf-8")
            with self.assertRaises(ds.DatasetError):
                list(stream.iter_lila_csv("toy", path))

    def test_end_to_end_fetch_from_stream(self):
        rows = [_row(i, f"m{i}.jpg", "alces alces", location=f"Area {i % 3}") for i in range(9)]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            path = _csv_zip(tmp / "m.csv.zip", rows)
            registry = _registry()
            plan = stream.plan_from_images(registry.sources[0], stream.iter_lila_csv("toy", path), {"moose": 6})
            self.assertEqual(len(plan.chosen), 6)
            self.assertEqual(len({c.image.location for c in plan.chosen}), 3)  # spread across areas
            files = {f"https://example.org/img/part0/m{i}.jpg": bytes([i]) * 50 for i in range(9)}
            summary = ds.fetch_images(plan, registry, tmp / "out", max_bytes=10_000, cache_root=tmp / "cache",
                                      fetch=lambda url: files[url], log=lambda _: None)
            self.assertEqual(summary["images"], 6)
            self.assertEqual(summary["split_leaks"], [])
            record = json.loads((tmp / "out/manifest.jsonl").read_text().splitlines()[0])
            self.assertEqual(record["split_basis"], "location")
            self.assertEqual(record["source_annotations"][0]["category"], "alces alces")


if __name__ == "__main__":
    unittest.main()
