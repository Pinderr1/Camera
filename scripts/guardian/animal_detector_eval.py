"""Compare animal detectors on the Guardian wildlife sample, then feed each
detector's boxes to SpeciesNet.

    python scripts/guardian/animal_detector_eval.py run --out bench_runs/guardian_detectors/NAME
    python scripts/guardian/animal_detector_eval.py report --out bench_runs/guardian_detectors/NAME

`run` starts one child process per detector (`detect`), so each one's load time
and memory are measured on its own, then calls `report`. Detectors run at a low
threshold (0.05) and `report` sweeps the operating thresholds offline.

Ground truth:
  * animal present: every image whose source label is not "empty" (the Guardian
    "negative" bucket holds deer, foxes, raccoons...: animals, just not targets);
  * empty frames (40): any animal box there is a false positive;
  * boxes: the dataset's own boxes (ENA24, part of Caltech). NACTI and most
    Caltech images have none, so box metrics cover the boxed images only;
  * night: an image whose three colour channels are equal (infrared).

Caveat printed in the report: MegaDetector was trained on LILA camera-trap data,
very likely including these images; YOLOX was trained on COCO only. MegaDetector's
numbers here are optimistic, and none of them describe house-mounted security
cameras."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

RUNS = ["data/guardian/runs/initial", "data/guardian/runs/nacti_initial"]
LOW = 0.05
DETECTORS = {
    # name: (detector settings, thresholds to sweep, operating threshold)
    "yolox_s": ({"type": "onnx", "model": "auto"}, [0.1, 0.2, 0.3, 0.4, 0.5], 0.5),
    "mdv6_apa_rtdetr_c": ({"type": "onnx", "model": "mdv6_apa_rtdetr_c"}, [0.1, 0.2, 0.3, 0.4, 0.5, 0.6], None),
}
COCO_ANIMALS = ["bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"]
ANIMAL_LABELS = set(COCO_ANIMALS) | {"animal"}
CLASSES = COCO_ANIMALS + ["animal", "person", "car", "truck", "bus", "motorcycle", "vehicle"]


def records():
    out = []
    for run in RUNS:
        base = ROOT / run
        for line in (base / "manifest.jsonl").read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            record["_file"] = str(base / record["path"])
            out.append(record)
    return out


def is_night(image) -> bool:
    import numpy as np

    small = image[::16, ::16].astype(np.int16)
    return float(np.abs(small[..., 0] - small[..., 1]).mean() + np.abs(small[..., 1] - small[..., 2]).mean()) < 2.0


def pct(values, q):
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(q / 100 * len(values)))], 1)


# ---- detect: one detector, one process --------------------------------------

def detect(name: str, out: Path, provider: str, limit: int) -> None:
    import cv2

    from sentinel.detectors import create_detector, resolve_detector_settings
    from sentinel.resources import _process_rss_mb
    from sentinel.vision import Frame

    rows = records()[: limit or None]
    settings = resolve_detector_settings(DETECTORS[name][0] | {"providers": provider, "classes": CLASSES})
    rss0 = _process_rss_mb()
    started = time.perf_counter()
    detector = create_detector(settings, LOW)
    first = cv2.imread(rows[0]["_file"])
    detector.detect(Frame(first, 0))  # warm-up: first DirectML run compiles kernels
    load_s = time.perf_counter() - started
    rss_loaded = _process_rss_mb()
    times, rss, results = [], [], []
    for index, record in enumerate(rows):
        image = cv2.imread(record["_file"])
        t = time.perf_counter()
        detections = detector.detect(Frame(image, 0))
        times.append((time.perf_counter() - t) * 1000)
        results.append({"key": record["key"], "night": is_night(image),
                        "detections": [{"label": d.label, "conf": round(d.confidence, 4), "bbox": [round(v, 5) for v in d.bbox]}
                                       for d in detections]})
        if index % 25 == 0:
            rss.append(_process_rss_mb() or 0)
    resources = {
        "detector": detector.describe() if hasattr(detector, "describe") else settings,
        "images": len(rows), "load_and_warmup_s": round(load_s, 2),
        "ms": {"p50": pct(times, 50), "p95": pct(times, 95), "mean": round(sum(times) / len(times), 1)},
        "rss_mb": {"before_load": rss0, "after_load": rss_loaded, "peak_sampled": max(rss), "added_by_detector": round(max(rss) - (rss0 or 0), 1)},
    }
    suffix = "" if provider == "auto" else f".{provider}"
    (out / f"detect.{name}{suffix}.json").write_text(json.dumps({"resources": resources, "rows": results}), encoding="utf-8")
    print(name, provider, json.dumps(resources))


# ---- report -----------------------------------------------------------------

def reference_boxes(record):
    boxes = []
    for annotation in record["source_annotations"]:
        if annotation.get("bbox"):
            x, y, w, h = annotation["bbox"]
            boxes.append((x / record["width"], y / record["height"], w / record["width"], h / record["height"]))
    return boxes


def match(predicted, reference, threshold=0.5):
    """Greedy one-to-one matching by IoU. Returns matched IoUs and unmatched predictions."""
    from sentinel.tracker import iou

    pairs = sorted(((iou(p, r), i, j) for i, p in enumerate(predicted) for j, r in enumerate(reference)), reverse=True)
    used_p, used_r, ious = set(), set(), []
    for value, i, j in pairs:
        if value < threshold or i in used_p or j in used_r:
            continue
        used_p.add(i)
        used_r.add(j)
        ious.append(value)
    return ious, len(predicted) - len(used_p)


def animal_boxes(detections, threshold):
    return [d for d in detections if d["label"] in ANIMAL_LABELS and d["conf"] >= threshold]


def merge(primary, animal, iou_threshold=0.5):
    """Same rule as detectors.AnimalAugmentedDetector: keep every primary box, add
    animal-detector boxes that do not overlap a primary animal box."""
    from sentinel.tracker import iou

    kept = list(primary)
    for det in animal:
        if not any(p["label"] in ANIMAL_LABELS and iou(p["bbox"], det["bbox"]) >= iou_threshold for p in primary):
            kept.append(det)
    return kept


def paired(primary, animal):
    """detectors.AnimalAugmentedDetector with `merge: animal_box` (the shipped default)."""
    from sentinel.detectors import AnimalAugmentedDetector
    from sentinel.models import Detection

    to_det = lambda d: Detection(d["label"], d["conf"], tuple(d["bbox"]))  # noqa: E731
    merged = AnimalAugmentedDetector._paired([to_det(d) for d in primary], [to_det(d) for d in animal])
    return [{"label": d.label, "conf": d.confidence, "bbox": list(d.bbox)} for d in merged]


def score(records_by_key, rows_by_key, threshold, night_by_key):
    """Detection metrics for one detector at one threshold."""
    c = Counter()
    by_bucket = defaultdict(Counter)
    ious = []
    for key, record in records_by_key.items():
        dets = animal_boxes(rows_by_key[key], threshold)
        empty = [label.lower() for label in record["source_labels"]] == ["empty"]
        light = "night" if night_by_key[key] else "day"
        if empty:
            c["empty_images"] += 1
            c["empty_with_animal_box"] += bool(dets)
            c["empty_animal_boxes"] += len(dets)
            continue
        found = bool(dets)
        c["animal_images"] += 1
        c["animal_found"] += found
        c[f"{light}_images"] += 1
        c[f"{light}_found"] += found
        by_bucket[record["bucket"]]["images"] += 1
        by_bucket[record["bucket"]]["found"] += found
        reference = reference_boxes(record)
        if reference:
            matched, extra = match([tuple(d["bbox"]) for d in dets], reference)
            c["ref_boxes"] += len(reference)
            c["ref_matched"] += len(matched)
            c["pred_boxes_on_boxed"] += len(dets)
            c["pred_unmatched_on_boxed"] += extra
            ious += matched
    out = {
        "threshold": threshold,
        "image_recall": round(c["animal_found"] / max(1, c["animal_images"]), 3),
        "found": f"{c['animal_found']}/{c['animal_images']}",
        "day_recall": round(c["day_found"] / max(1, c["day_images"]), 3), "day": f"{c['day_found']}/{c['day_images']}",
        "night_recall": round(c["night_found"] / max(1, c["night_images"]), 3), "night": f"{c['night_found']}/{c['night_images']}",
        "empty_fp_images": f"{c['empty_with_animal_box']}/{c['empty_images']}",
        "empty_fp_boxes": c["empty_animal_boxes"],
        "box_recall_iou50": round(c["ref_matched"] / max(1, c["ref_boxes"]), 3),
        "box_matched": f"{c['ref_matched']}/{c['ref_boxes']}",
        "box_precision_iou50": round((c["pred_boxes_on_boxed"] - c["pred_unmatched_on_boxed"]) / max(1, c["pred_boxes_on_boxed"]), 3),
        "unmatched_boxes_on_boxed_images": c["pred_unmatched_on_boxed"],
        "matched_iou_mean": round(sum(ious) / len(ious), 3) if ious else None,
        "by_bucket": {bucket: f"{v['found']}/{v['images']}" for bucket, v in sorted(by_bucket.items())},
    }
    return out


def species_stage(records_by_key, configs, model_dir, provider):
    """SpeciesNet on each configuration's most confident animal box."""
    import cv2

    from speciesnet_eval import expected_for, judge
    from sentinel.guardian.species import SpeciesPolicy, SpeciesSettings
    from sentinel.guardian.species_onnx import SpeciesNetOnnx

    model = SpeciesNetOnnx(model_dir, provider)
    policy = SpeciesPolicy(model.taxa, SpeciesSettings(min_agreeing=1), model.geofence)
    cache: dict[tuple, dict] = {}
    results = {name: Counter() for name in configs}
    per_bucket = {name: defaultdict(Counter) for name in configs}
    wrong: dict[str, list[str]] = {name: [] for name in configs}
    images = {}
    for key, record in records_by_key.items():
        source_label, expected = expected_for(record)
        empty = source_label == "empty"
        for name, dets in configs.items():
            c = results[name]
            boxes = dets[key]
            bucket = per_bucket[name][record["bucket"]]
            bucket["images"] += 1
            if empty:
                c["empty_images"] += 1
                if boxes:
                    c["empty_classified"] += 1
                continue
            if not boxes:
                c["no_box"] += 1
                continue
            best = max(boxes, key=lambda d: d["conf"])
            cache_key = (key, tuple(round(v, 3) for v in best["bbox"]))
            if cache_key not in cache:
                if key not in images:
                    images = {key: cv2.imread(record["_file"])}
                crop = model.preprocess(images[key], tuple(best["bbox"]))
                if crop is None:
                    cache[cache_key] = {"skipped": True}
                else:
                    decision = policy.decide(model.classify([crop])[0])
                    cache[cache_key] = {**decision.as_dict(), **judge(decision, expected)}
            result = cache[cache_key]
            if result.get("skipped"):
                c["box_too_small"] += 1
                continue
            c["classified"] += 1
            c[f"status_{result['status']}"] += 1
            if expected is not None:
                c["with_expected"] += 1
                if result["status"] == "confirmed":
                    c["confirmed_right" if result["top1_correct"] else "confirmed_wrong"] += 1
                    if not result["top1_correct"]:
                        wrong[name].append(f"{key} ({source_label} -> {result['display']} {result['confidence']:.2f})")
                    bucket["confirmed_right" if result["top1_correct"] else "confirmed_wrong"] += 1
    expected_total = sum(1 for r in records_by_key.values() if expected_for(r)[1] is not None)
    for name, c in results.items():
        c["animal_images_with_expected_species"] = expected_total
    return {name: {"totals": dict(c), "by_bucket": {b: dict(v) for b, v in sorted(per_bucket[name].items())}, "confirmed_wrong": wrong[name]}
            for name, c in results.items()}, model.provider


def report(out: Path, model_dir: str, provider: str, union_threshold: float | None) -> None:
    all_records = {r["key"]: r for r in records()}
    loaded = {}
    for name in DETECTORS:
        path = out / f"detect.{name}.json"
        if path.exists():
            loaded[name] = json.loads(path.read_text(encoding="utf-8"))
    cpu = {name: json.loads((out / f"detect.{name}.cpu.json").read_text(encoding="utf-8"))["resources"]
           for name in DETECTORS if (out / f"detect.{name}.cpu.json").exists()}
    keys = [k for k in all_records if all(k in {r["key"] for r in data["rows"]} for data in loaded.values())]
    records_by_key = {k: all_records[k] for k in keys}
    rows = {name: {r["key"]: r["detections"] for r in data["rows"]} for name, data in loaded.items()}
    night = {r["key"]: r["night"] for r in next(iter(loaded.values()))["rows"]}

    sweeps = {name: [score(records_by_key, rows[name], t, night) for t in DETECTORS[name][1]] for name in loaded}
    # Operating points: YOLOX at its shipped threshold; MegaDetector at the highest
    # threshold that keeps recall within 2 points of its best, unless one is given.
    chosen = {}
    for name, sweep in sweeps.items():
        fixed = DETECTORS[name][2] if name != "mdv6_apa_rtdetr_c" else union_threshold
        if fixed is not None:
            chosen[name] = next(s for s in sweep if s["threshold"] == fixed)
        else:
            best = max(s["image_recall"] for s in sweep)
            chosen[name] = max((s for s in sweep if s["image_recall"] >= best - 0.02), key=lambda s: s["threshold"])
    configs = {name: {k: animal_boxes(rows[name][k], chosen[name]["threshold"]) for k in keys} for name in loaded}
    if {"yolox_s", "mdv6_apa_rtdetr_c"} <= set(loaded):
        ty, tm = chosen["yolox_s"]["threshold"], chosen["mdv6_apa_rtdetr_c"]["threshold"]
        union_rows = {k: merge(animal_boxes(rows["yolox_s"][k], ty), animal_boxes(rows["mdv6_apa_rtdetr_c"][k], tm)) for k in keys}
        configs["yolox_s+mdv6"] = union_rows
        chosen["yolox_s+mdv6"] = score(records_by_key, {k: [d | {"conf": 1.0} for d in v] for k, v in union_rows.items()}, 0.0, night)
        chosen["yolox_s+mdv6"]["threshold"] = f"{ty} / {tm}"
        paired_rows = {k: paired(animal_boxes(rows["yolox_s"][k], ty), animal_boxes(rows["mdv6_apa_rtdetr_c"][k], tm)) for k in keys}
        configs["yolox_s+mdv6 paired"] = paired_rows
        chosen["yolox_s+mdv6 paired"] = score(records_by_key, {k: [d | {"conf": 1.0} for d in v] for k, v in paired_rows.items()}, 0.0, night)
        chosen["yolox_s+mdv6 paired"]["threshold"] = f"{ty} / {tm}"
    species, species_provider = species_stage(records_by_key, configs, model_dir, provider)

    summary = {"images": len(keys), "low_threshold": LOW, "sweeps": sweeps, "operating_points": chosen,
               "species": species, "species_provider": species_provider,
               "resources": {name: data["resources"] for name, data in loaded.items()}, "resources_cpu": cpu}
    (out / "results.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    text = render(summary)
    (out / "report.md").write_text(text, encoding="utf-8")
    print(text)


def render(s):
    lines = ["# Animal detectors on the Guardian wildlife sample", "",
             f"{s['images']} camera-trap stills (ENA24, Caltech Camera Traps, NACTI; CDLA-Permissive-1.0). "
             "Animal present = any source label except `empty`.",
             "**Caveat:** MegaDetector's training data very likely includes these public datasets; YOLOX saw COCO only. "
             "MegaDetector's figures are therefore optimistic, and none describe house-mounted security cameras.", "",
             "## Operating points", "",
             "| Detector | Threshold | Animal images found | Day | Night (IR) | Empty frames with a box | Box recall @IoU 0.5 | Box precision @IoU 0.5 | Mean IoU (matched) |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, p in s["operating_points"].items():
        lines.append(f"| {name} | {p['threshold']} | {p['found']} ({p['image_recall']:.0%}) | {p['day']} | {p['night']} | "
                     f"{p['empty_fp_images']} | {p['box_matched']} ({p['box_recall_iou50']:.0%}) | {p['box_precision_iou50']:.0%} | {p['matched_iou_mean']} |")
    lines += ["", "Found by bucket (bucket = Guardian target; `negative` = other animals):", ""]
    for name, p in s["operating_points"].items():
        lines.append(f"- {name}: {json.dumps(p['by_bucket'])}")
    lines += ["", "## Threshold sweeps", ""]
    for name, sweep in s["sweeps"].items():
        lines += [f"### {name}", "", "| Threshold | Found | Day | Night | Empty FP images | Box recall | Box precision | Mean IoU |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for p in sweep:
            lines.append(f"| {p['threshold']} | {p['found']} | {p['day']} | {p['night']} | {p['empty_fp_images']} | {p['box_matched']} | "
                         f"{p['box_precision_iou50']:.0%} | {p['matched_iou_mean']} |")
        lines.append("")
    lines += ["## SpeciesNet on each detector's best box", "",
              f"Classifier provider: {s['species_provider']}. One crop per image (`min_agreeing=1`). "
              "`End-to-end` = images where the detector found the animal and SpeciesNet confirmed the dataset's species, "
              "over all animal images with a known expected species.", "",
              "| Configuration | Classified | Confirmed right | Confirmed wrong | Uncertain | Unknown | No box | End-to-end | Empty frames classified |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, data in s["species"].items():
        t = data["totals"]
        total = t.get("animal_images_with_expected_species", 0)
        lines.append(f"| {name} | {t.get('classified', 0)} | {t.get('confirmed_right', 0)} | {t.get('confirmed_wrong', 0)} | "
                     f"{t.get('status_uncertain', 0)} | {t.get('status_unknown', 0)} | {t.get('no_box', 0)} | "
                     f"{t.get('confirmed_right', 0)}/{total} ({t.get('confirmed_right', 0) / max(1, total):.0%}) | "
                     f"{t.get('empty_classified', 0)}/{t.get('empty_images', 0)} |")
    lines += ["", "Confirmed right by bucket:", ""]
    for name, data in s["species"].items():
        lines.append(f"- {name}: " + json.dumps({b: f"{v.get('confirmed_right', 0)}/{v.get('images', 0)}" for b, v in data["by_bucket"].items()}))
    lines += ["", "Confirmed wrong (source label -> SpeciesNet):", ""]
    for name, data in s["species"].items():
        lines.append(f"- {name}: " + ("; ".join(data["confirmed_wrong"]) or "none"))
    lines += ["", "## Speed and memory", "", "| Detector | Provider | Input | Load + warm-up | p50 ms | p95 ms | Process RSS added (MB) | CPU p50 / p95 ms |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, r in s["resources"].items():
        d = r["detector"]
        cpu = s["resources_cpu"].get(name)
        cpu_text = f"{cpu['ms']['p50']} / {cpu['ms']['p95']} ({cpu['detector'].get('model')}, {cpu['images']} images)" if cpu else "-"
        lines.append(f"| {name} | {d.get('provider')} | {d.get('input')} | {r['load_and_warmup_s']} s | {r['ms']['p50']} | {r['ms']['p95']} | "
                     f"{r['rss_mb']['added_by_detector']} | {cpu_text} |")
    lines += ["", "RSS is the process working set (model, ONNX Runtime and the DirectML device). GPU memory is not included.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "detect", "report"])
    parser.add_argument("--out", type=Path, default=Path("bench_runs/guardian_detectors/latest"))
    parser.add_argument("--detector", choices=list(DETECTORS))
    parser.add_argument("--provider", default="auto", help="auto | dml | cpu")
    parser.add_argument("--cpu-sample", type=int, default=40, help="images to re-time on the CPU provider (0 = skip)")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--md-threshold", type=float, help="fix MegaDetector's operating threshold instead of choosing it")
    parser.add_argument("--species-model", default="models/speciesnet/v4.0.3a")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.command == "detect":
        detect(args.detector, args.out, args.provider, args.limit)
        return 0
    if args.command == "run":
        env = os.environ | {"PYTHONPATH": str(ROOT / "src")}
        for name in DETECTORS:
            jobs = [(args.provider, args.limit)] + ([("cpu", args.cpu_sample)] if args.cpu_sample else [])
            for provider, limit in jobs:
                subprocess.run([sys.executable, __file__, "detect", "--detector", name, "--out", str(args.out),
                                "--provider", provider, "--limit", str(limit)], check=True, env=env)
    report(args.out, args.species_model, args.provider, args.md_threshold)
    return 0


if __name__ == "__main__":
    sys.exit(main())
