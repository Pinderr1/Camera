"""Selective fine-tuning: plan and dataset checks only (stdlib). No training runs here.

`plan` reads an exported example set (examples.collect, purpose="training") and
writes plan.md + plan.json: what data is usable, per split and class, which
recipe fits, and what is blocked. Training itself is a separate, approved step
in its own environment (PyTorch is not a Sentinel dependency): the official
YOLOX training code (Apache-2.0) from the shipped COCO weights, with the
backbone frozen, a few epochs, and the replay of COCO-like data to avoid
forgetting. The result is exported to ONNX and enters Sentinel only through
`python -m sentinel.training register` + `compare` on the test split.

`check` validates an export: COCO files load, every box is inside the image,
every image file exists, no clip appears in two splits, no test-split data."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

MIN_IMAGES_PER_CLASS = 200  # below this, fine-tuning mostly memorises a handful of scenes

RECIPE = {
    "base": "YOLOX-s COCO weights (Apache-2.0, Megvii-BaseDetection/YOLOX)",
    "code": "https://github.com/Megvii-BaseDetection/YOLOX (Apache-2.0); never Ultralytics (AGPL)",
    "method": "freeze backbone (FPN/head trainable), 10-30 epochs, lr 1e-4 cosine, mosaic off for small sets, "
              "mix 1:1 with COCO person/vehicle/animal images to avoid forgetting",
    "export": "tools/export_onnx.py --dynamic, then register with family=yolox labels=coco80",
    "hardware": "a CUDA GPU (the GTX 1070 machine); the Iris Xe laptop is too slow for training",
    "gate": "python -m sentinel.training compare --candidate NEW --split test must say better or no_worse before promote",
}


def check(export_dir: str | Path) -> dict[str, Any]:
    root = Path(export_dir)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    problems: list[str] = []
    counts: dict[str, Any] = {}
    clip_split: dict[str, str] = {}
    for example in (json.loads(line) for line in (root / "examples.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()):
        previous = clip_split.setdefault(example["clip"], example["split"])
        if previous != example["split"]:
            problems.append(f"clip {example['clip']} appears in {previous} and {example['split']}")
        if manifest["purpose"] == "training" and example["split"] == "test":
            problems.append(f"test-split example from {example['clip']} in a training export")
    for path in sorted(root.glob("coco_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        per_class = Counter()
        names = {c["id"]: c["name"] for c in data["categories"]}
        for image in data["images"]:
            if not (root / image["file_name"]).exists():
                problems.append(f"{path.name}: missing {image['file_name']}")
        for annotation in data["annotations"]:
            x, y, w, h = annotation["bbox_normalized"]
            if w <= 0 or h <= 0 or x < -0.01 or y < -0.01 or x + w > 1.01 or y + h > 1.01:
                problems.append(f"{path.name}: box {annotation['id']} outside the image")
            per_class[names.get(annotation["category_id"], "?")] += 1
        counts[path.stem.removeprefix("coco_")] = {"images": len(data["images"]), "boxes": dict(per_class)}
    return {"ok": not problems, "problems": problems[:50], "splits": counts, "purpose": manifest["purpose"]}


def plan(export_dir: str | Path) -> dict[str, Any]:
    root = Path(export_dir)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    checked = check(root)
    train_like = {split: info for split, info in checked["splits"].items() if split != "test"}
    images = sum(info["images"] for info in train_like.values())
    per_class: Counter = Counter()
    for info in train_like.values():
        per_class.update(info["boxes"])
    blocked = []
    if manifest["purpose"] != "training":
        blocked.append("export was made for evaluation; re-export with --purpose training")
    if not checked["ok"]:
        blocked.append("dataset check failed: " + "; ".join(checked["problems"][:3]))
    thin = [name for name, count in per_class.items() if count < MIN_IMAGES_PER_CLASS]
    if not per_class:
        blocked.append("no reviewed frames yet: mark detections in the Test Lab first")
    elif thin:
        blocked.append(f"too few boxes for {', '.join(sorted(thin))} (fewer than {MIN_IMAGES_PER_CLASS} each)")
    result = {"images": images, "boxes_per_class": dict(per_class), "splits": checked["splits"], "recipe": RECIPE,
              "ready": not blocked, "blocked": blocked, "excluded_runs": manifest.get("excluded_runs", {}),
              "approval": "Training is not started by Sentinel. It needs the owner's approval and a GPU machine."}
    (root / "plan.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    lines = ["# Fine-tuning plan", "", f"Ready: **{'yes' if result['ready'] else 'no'}**", ""]
    lines += [f"- blocked: {item}" for item in blocked] or ["- nothing blocks preparation; training still needs approval"]
    lines += ["", f"Reviewed training images: {images}", "", "| Class | Boxes |", "| --- | --- |"]
    lines += [f"| {name} | {count} |" for name, count in sorted(per_class.items())]
    lines += ["", "## Recipe", ""] + [f"- **{key}**: {value}" for key, value in RECIPE.items()]
    (root / "plan.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result
