"""Run SpeciesNet (ONNX) on a Guardian dataset sample and report what it gets right.

    python scripts/guardian/speciesnet_eval.py --run data/guardian/runs/initial \
        --out bench_runs/guardian_species/initial --provider dml --cpu-sample 20

For every image it crops two boxes and classifies each:
  * `sentinel`: the most confident animal box from Sentinel's own detector
    (the live path: YOLOX via ONNX Runtime, `model: auto`);
  * `reference`: the dataset's own box, or LILA's MegaDetector v5a box when the
    dataset has none (unverified; used only to separate detector misses from
    classifier mistakes).
Decisions use guardian.species.SpeciesPolicy with one crop per image
(`min_agreeing=1`), so they are stricter on live tracks, not looser.

This is a small camera-trap sample from datasets SpeciesNet may have been
trained on. The numbers describe this sample only; they are not an accuracy
claim for Sentinel cameras.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

ANIMALS = ["bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"]
# Source label (lower case) -> expected (genus, species); species None accepts any species of the genus.
EXPECTED = {
    "american black bear": ("ursus", "americanus"),
    "mountain_lion": ("puma", "concolor"),
    "coyote": ("canis", "latrans"),
    "dog": ("canis", "familiaris"),
    "white_tailed_deer": ("odocoileus", "virginianus"),
    "deer": ("odocoileus", None),
    "bobcat": ("lynx", "rufus"),
    "red fox": ("vulpes", "vulpes"),
    "northern raccoon": ("procyon", "lotor"),
    "domestic cat": ("felis", "catus"),
    "horse": ("equus", "caballus"),
    "empty": None,
    # NACTI uses scientific names. Its "cervus elaphus" (red deer) is scored against the
    # source label as given: an "elk" prediction counts as a different species.
    "ursus americanus": ("ursus", "americanus"),
    "puma concolor": ("puma", "concolor"),
    "canis latrans": ("canis", "latrans"),
    "alces alces": ("alces", "alces"),
    "cervus canadensis": ("cervus", "canadensis"),
    "cervus elaphus": ("cervus", "elaphus"),
    "canis familiaris": ("canis", "familiaris"),
    "odocoileus hemionus": ("odocoileus", "hemionus"),
    "bos taurus": ("bos", "taurus"),
    "lynx rufus": ("lynx", "rufus"),
    "sus scrofa": ("sus", "scrofa"),
}


def pct(values, q):
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(q / 100 * len(values)))], 3)


def gpu_counters(pid: int) -> dict:
    """Windows performance counters for this process's GPU use (works for any WDDM GPU)."""
    script = (
        f"$s=(Get-Counter '\\GPU Process Memory(pid_{pid}*)\\Shared Usage','\\GPU Process Memory(pid_{pid}*)\\Dedicated Usage',"
        f"'\\GPU Engine(pid_{pid}*engtype_3D)\\Utilization Percentage','\\GPU Engine(pid_{pid}*engtype_Compute*)\\Utilization Percentage' "
        "-ErrorAction SilentlyContinue).CounterSamples; $s | ForEach-Object { \"$($_.Path)|$($_.CookedValue)\" }"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    shared = dedicated = util = 0.0
    for line in out.splitlines():
        path, _, value = line.rpartition("|")
        try:
            number = float(value)
        except ValueError:
            continue
        if "shared usage" in path.lower():
            shared += number
        elif "dedicated usage" in path.lower():
            dedicated += number
        elif "utilization" in path.lower():
            util += number
    return {"shared_mb": round(shared / 2**20, 1), "dedicated_mb": round(dedicated / 2**20, 1), "engine_utilization_pct": round(util, 1)}


def expected_for(record):
    labels = [label.lower() for label in record["source_labels"]]
    known = [EXPECTED[label] for label in labels if label in EXPECTED]
    return labels[0] if labels else "", (known[0] if known else None)


def judge(decision, expected):
    taxon = decision.taxon or {}
    taxonomy = (taxon.get("taxonomy") or ";;;;").split(";")
    genus, species = taxonomy[3], taxonomy[4]
    if expected is None:
        return {"top1_correct": None, "genus_correct": None}
    top1 = taxon.get("level") == "species" and genus == expected[0] and (expected[1] is None or species.split(" ")[0] == expected[1])
    return {"top1_correct": bool(top1), "genus_correct": genus == expected[0]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", default="data/guardian/runs/initial")
    parser.add_argument("--model", default="models/speciesnet/v4.0.3a")
    parser.add_argument("--out", default="bench_runs/guardian_species/initial")
    parser.add_argument("--provider", default="auto", help="auto | dml | cpu (classifier and detector)")
    parser.add_argument("--cpu-sample", type=int, default=20, help="crops to re-time on the CPU provider")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    import cv2

    from sentinel.detectors import create_detector, detection_thresholds, resolve_detector_settings
    from sentinel.guardian.species import SpeciesPolicy, SpeciesSettings
    from sentinel.guardian.species_onnx import SpeciesNetOnnx
    from sentinel.resources import _process_rss_mb
    from sentinel.vision import Frame

    run = Path(args.run)
    records = [json.loads(line) for line in (run / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    if args.limit:
        records = records[: args.limit]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    resources = {"rss_start_mb": _process_rss_mb()}
    started = time.perf_counter()
    detector_settings = resolve_detector_settings({"type": "onnx", "model": "auto", "providers": args.provider, "classes": ANIMALS})
    threshold, _ = detection_thresholds(detector_settings)
    detector = create_detector(detector_settings, threshold)
    detector.detect(Frame(cv2.imread(str(run / records[0]["path"])), 0))  # warm-up (and model download on first use)
    resources["detector_load_s"] = round(time.perf_counter() - started, 1)
    resources["rss_after_detector_mb"] = _process_rss_mb()
    started = time.perf_counter()
    model = SpeciesNetOnnx(args.model, args.provider)
    resources["classifier_load_s"] = round(time.perf_counter() - started, 1)
    resources["rss_after_classifier_mb"] = _process_rss_mb()
    policy = SpeciesPolicy(model.taxa, SpeciesSettings(min_agreeing=1), model.geofence)
    policy_can = SpeciesPolicy(model.taxa, SpeciesSettings(min_agreeing=1, country="CAN"), model.geofence)

    rows = []
    det_ms, pre_ms, cls_ms, rss = [], [], [], []
    crops_for_cpu = []
    cpu_start, wall_start = sum(os.times()[:2]), time.perf_counter()
    gpu_mid = {}
    for index, record in enumerate(records):
        image = cv2.imread(str(run / record["path"]))
        height, width = image.shape[:2]
        t = time.perf_counter()
        detections = [d for d in detector.detect(Frame(image, 0)) if d.label in ANIMALS]
        det_ms.append((time.perf_counter() - t) * 1000)
        boxes = {}
        if detections:
            best = max(detections, key=lambda d: d.confidence)
            boxes["sentinel"] = (best.bbox, {"detector_label": best.label, "detector_conf": round(best.confidence, 3)})
        source = next((a["bbox"] for a in record["source_annotations"] if a.get("bbox")), None)
        if source:
            x, y, w, h = source
            boxes["reference"] = ((x / width, y / height, w / width, h / height), {"box": "dataset"})
        elif record.get("megadetector"):
            md = [d for d in record["megadetector"]["detections"] if d["category"] == "animal" and (d["conf"] or 0) >= 0.2]
            if md:
                best_md = max(md, key=lambda d: d["conf"])
                boxes["reference"] = (tuple(best_md["bbox_relative"]), {"box": "megadetector_v5a_unverified", "md_conf": best_md["conf"]})
        source_label, expected = expected_for(record)
        row = {"key": record["key"], "dataset": record["dataset"], "bucket": record["bucket"], "split": record["split"],
               "source_label": source_label, "expected": list(expected) if expected else None,
               "detections": [{"label": d.label, "conf": round(d.confidence, 3)} for d in detections]}
        for name, (bbox, meta) in boxes.items():
            t = time.perf_counter()
            crop = model.preprocess(image, bbox)
            pre_ms.append((time.perf_counter() - t) * 1000)
            if crop is None:
                row[name] = {**meta, "skipped": "box too small"}
                continue
            t = time.perf_counter()
            scores = model.classify([crop])[0]
            cls_ms.append((time.perf_counter() - t) * 1000)
            if len(crops_for_cpu) < args.cpu_sample:
                crops_for_cpu.append(crop)
            decision = policy.decide(scores)
            row[name] = {**meta, "bbox": [round(v, 4) for v in bbox], **decision.as_dict(),
                         "status_in_canada": policy_can.decide(scores).status, **judge(decision, expected)}
        rows.append(row)
        if index % 20 == 0:
            rss.append(_process_rss_mb())
        if index == len(records) // 2:
            gpu_mid = gpu_counters(os.getpid())
        if index % 40 == 0:
            print(f"{index}/{len(records)}", flush=True)
    wall = time.perf_counter() - wall_start
    resources["process_cpu_pct_of_machine"] = round(100 * (sum(os.times()[:2]) - cpu_start) / wall / (os.cpu_count() or 1), 1)
    resources["rss_peak_sampled_mb"] = max(v for v in rss if v) if any(rss) else None
    resources["gpu_mid_run"] = gpu_mid
    resources["wall_s"] = round(wall, 1)

    cpu_timing = {}
    if crops_for_cpu and model.provider != "CPUExecutionProvider":
        cpu_model = SpeciesNetOnnx(args.model, "cpu", cpu_threads=2)
        cpu_model.classify(crops_for_cpu[:1])
        times = []
        for crop in crops_for_cpu:
            t = time.perf_counter()
            cpu_model.classify([crop])
            times.append((time.perf_counter() - t) * 1000)
        cpu_timing = {"crops": len(times), "p50_ms": pct(times, 50), "p95_ms": pct(times, 95), "threads": 2}

    summary = summarise(rows)
    summary["timing"] = {
        "provider": model.provider, "detector": detector.describe() if hasattr(detector, "describe") else {},
        "detect_ms": {"p50": pct(det_ms, 50), "p95": pct(det_ms, 95)},
        "preprocess_ms": {"p50": pct(pre_ms, 50), "p95": pct(pre_ms, 95)},
        "classify_ms": {"p50": pct(cls_ms, 50), "p95": pct(cls_ms, 95), "n": len(cls_ms)},
        "classify_cpu_ms": cpu_timing,
    }
    summary["resources"] = resources | {"rss_end_mb": _process_rss_mb()}
    summary["model"] = {"name": "SpeciesNet", "version": model.version, "labels": len(model.taxa), "onnx": "speciesnet.onnx"}
    (out / "results.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=2), encoding="utf-8")
    (out / "report.md").write_text(report(summary), encoding="utf-8")
    print(report(summary))
    return 0


def summarise(rows):
    out = {"images": len(rows), "by_box": {}}
    for box in ("sentinel", "reference"):
        table = defaultdict(lambda: Counter())
        conf = defaultdict(list)
        canid, bear, empty = Counter(), Counter(), Counter()
        predicted: dict[str, Counter] = defaultdict(Counter)
        for row in rows:
            key = f"{row['bucket']}|{row['split']}"
            table[key]["images"] += 1
            result = row.get(box)
            if not result or "status" not in result:
                table[key]["no_box" if not result else "box_too_small"] += 1
                if row["bucket"] == "negative" and row["source_label"] == "empty" and box == "sentinel":
                    empty["no_detection"] += 1
                continue
            table[key]["classified"] += 1
            table[key][f"status_{result['status']}"] += 1
            if result["top1_correct"] is not None:
                table[key]["top1_correct" if result["top1_correct"] else "top1_wrong"] += 1
                if result["status"] == "confirmed":
                    table[key]["confirmed_correct" if result["top1_correct"] else "confirmed_wrong"] += 1
                conf["correct" if result["top1_correct"] else "wrong"].append(result["taxon"]["score"])
            label = (result.get("taxon") or {}).get("label", "")
            taxonomy = (result.get("taxon") or {}).get("taxonomy", ";;;;").split(";")
            predicted[row["bucket"]][f"{label} ({result['status']})"] += 1
            if row["source_label"] in ("coyote", "dog", "red fox", "canis latrans", "canis familiaris"):
                kind = (label if label in ("coyote", "domestic dog", "grey wolf", "red fox") else
                        ("other canid" if taxonomy[2] == "canidae" else "not a canid"))
                canid[f"{row['source_label']} -> {kind}"] += 1
            if row["bucket"] == "black_bear":
                kind = (label if label in ("american black bear", "brown bear", "grizzly bear") else
                        ("other bear / bear group" if taxonomy[2] == "ursidae" else "not a bear"))
                bear[f"{kind} ({result['status']})"] += 1
            if row["source_label"] == "empty" and box == "sentinel":
                empty[f"detector box -> {label} ({result['status']})"] += 1
        out["by_box"][box] = {
            "table": {k: dict(v) for k, v in sorted(table.items())},
            "confidence": {k: {"n": len(v), "p10": pct(v, 10), "p50": pct(v, 50), "p90": pct(v, 90),
                               "bins": dict(Counter("<0.5" if s < 0.5 else "0.5-0.7" if s < 0.7 else "0.7-0.9" if s < 0.9 else ">=0.9" for s in v))}
                           for k, v in conf.items()},
            "canid_confusion": dict(canid.most_common()),
            "black_bear": dict(bear.most_common()),
            "empty_frames": dict(empty.most_common()),
            "predicted_by_bucket": {b: dict(c.most_common(8)) for b, c in sorted(predicted.items())},
        }
    return out


def report(summary):
    lines = ["# SpeciesNet on the Guardian sample", "",
             f"Images: {summary['images']}. Model: SpeciesNet {summary['model']['version']} (ONNX, {summary['model']['labels']} labels). "
             f"Classifier provider: {summary['timing']['provider']}.",
             "Small camera-trap sample; SpeciesNet's training data may include these public datasets. Not an accuracy claim.", ""]
    for box, data in summary["by_box"].items():
        lines += [f"## Crops from {box} boxes", "",
                  "| Bucket / split | Images | Classified | Top-1 correct | Top-1 wrong | Confirmed (right / wrong) | Uncertain | Unknown | No box |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for key, c in data["table"].items():
            lines.append(f"| {key.replace('|', ' / ')} | {c.get('images', 0)} | {c.get('classified', 0)} | {c.get('top1_correct', 0)} | "
                         f"{c.get('top1_wrong', 0)} | {c.get('confirmed_correct', 0)} / {c.get('confirmed_wrong', 0)} | "
                         f"{c.get('status_uncertain', 0)} | {c.get('status_unknown', 0)} | {c.get('no_box', 0) + c.get('box_too_small', 0)} |")
        lines += ["", f"Confidence of the top label: {json.dumps(data['confidence'])}", "",
                  f"Dog / coyote / fox: {json.dumps(data['canid_confusion'])}", "",
                  f"Black bear images: {json.dumps(data['black_bear'])}", ""]
        lines += ["Top label by bucket (status):", ""] + [f"- {b}: {json.dumps(c)}" for b, c in data["predicted_by_bucket"].items()] + [""]
        if data["empty_frames"]:
            lines += [f"Empty frames: {json.dumps(data['empty_frames'])}", ""]
    lines += ["## Speed and resources", "", "```", json.dumps({"timing": summary["timing"], "resources": summary["resources"]}, indent=2), "```", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
