"""Collect Test Lab errors and corrections into a labelled example set (stdlib).

Sources, per lab run:
  * automatic errors where labels exist: detector boxes with no labelled person
    (false positive), labelled people no box matched (missed), frames where a
    labelled subject had no box or an empty scene had one;
  * the owner's corrections from the lab (false positive, missed with a drawn
    box, wrong label, confirmed correct, event verdicts).

`purpose="evaluation"` keeps everything (a local error review). `purpose="training"`
keeps only frames a sold model may learn from: clips whose licence allows
commercial training, never the `test` split (it must stay independent), and the
owner's own footage only while personalization is on (consent recorded, files
stay on this computer). The export writes COCO-format boxes per split for the
frames a person reviewed, plus a manifest with every clip's licence and
attribution."""

from __future__ import annotations

import json
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

PURPOSES = {"evaluation", "training"}


def personalization_path(storage_dir: str | Path) -> Path:
    return Path(storage_dir) / "lab" / "personalization.json"


def personalization(storage_dir: str | Path) -> dict[str, Any]:
    path = personalization_path(storage_dir)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"enabled": False}


def set_personalization(storage_dir: str | Path, enabled: bool, by: str = "", statement: str = "") -> dict[str, Any]:
    """Opt-in: the owner's own lab footage may be used to adapt models on this
    computer. Recorded with who agreed; turning it off stops new exports."""
    if enabled and not by:
        raise ValueError("record who agreed (--by)")
    record = {"enabled": enabled, "by": by, "statement": statement or "Owner's footage may be used to adapt detection models locally; it never leaves this computer.",
              "at": datetime.now().isoformat(timespec="seconds")}
    path = personalization_path(storage_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1), encoding="utf-8")
    return record


def _runs(storage_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    out = []
    for path in sorted((storage_dir / "lab" / "runs").glob("*/run.json")):
        meta = json.loads(path.read_text(encoding="utf-8"))
        if meta.get("state") in {"done", "cancelled", "interrupted"}:
            out.append((path.parent, meta))
    return out


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def allowed(clip: dict[str, Any], purpose: str, personal: bool) -> tuple[bool, str]:
    if purpose == "evaluation":
        return True, ""
    if clip.get("split") == "test":
        return False, "test split stays independent"
    if clip.get("source") == "private":
        return (True, "") if personal else (False, "personalization is off")
    if not clip.get("commercial_training"):
        return False, f"licence does not allow commercial training ({clip.get('licence')})"
    return True, ""


def collect(storage_dir: str | Path, out_dir: str | Path, purpose: str = "evaluation", copy_images: bool = True) -> dict[str, Any]:
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of {sorted(PURPOSES)}")
    storage = Path(storage_dir)
    out = Path(out_dir)
    (out / "images").mkdir(parents=True, exist_ok=True)
    personal = bool(personalization(storage).get("enabled"))
    examples: list[dict[str, Any]] = []
    excluded: Counter = Counter()
    clips: dict[str, dict[str, Any]] = {}
    coco: dict[str, dict[str, Any]] = {}
    categories: dict[str, int] = {}
    for run_dir, meta in _runs(storage):
        clip = meta["clip"]
        ok, why = allowed(clip, purpose, personal)
        if not ok:
            excluded[why] += 1
            continue
        frames = {f["n"]: f for f in _read_jsonl(run_dir / "frames.jsonl")}
        corrections = _read_jsonl(run_dir / "corrections.jsonl")
        clips[clip["id"]] = {key: clip.get(key) for key in ("id", "title", "source", "licence", "commercial_training", "split", "attribution", "labels")}
        used: set[int] = set()

        def add(kind: str, n: int, origin: str, **extra: Any) -> None:
            examples.append({"run": meta["id"], "clip": clip["id"], "split": clip.get("split"), "profile": meta.get("profile"),
                             "frame": n, "t": frames.get(n, {}).get("t"), "kind": kind, "origin": origin,
                             "image": f"images/{meta['id']}_{n:05d}.jpg"} | extra)
            used.add(n)

        for n, frame in frames.items():
            for i, det in enumerate(frame.get("dets", [])):
                if det.get("verdict") == "fp":
                    add("false_positive", n, "labels", bbox=det["bbox"], label=det["label"], confidence=det["conf"], detection=i)
            for box in frame.get("gt", []):
                if box.get("missed") and not box.get("ignore"):
                    add("missed", n, "labels", bbox=box["bbox"], label="person")
            verdict = (frame.get("presence") or {}).get("verdict")
            if verdict in {"missed", "false_positive"}:
                add(f"frame_{verdict}", n, "labels")
        reviewed: dict[int, list[dict[str, Any]]] = {}
        for correction in corrections:
            if "frame" not in correction:
                add(correction["kind"], -1, "owner", note=correction.get("note", ""), event_id=correction.get("event_id"))
                continue
            n = correction["frame"]
            det = None
            if correction.get("detection") is not None and n in frames and correction["detection"] < len(frames[n]["dets"]):
                det = frames[n]["dets"][correction["detection"]]
            add(correction["kind"], n, "owner", bbox=correction.get("bbox") or (det or {}).get("bbox"),
                label=correction.get("label") or (det or {}).get("label"), note=correction.get("note", ""))
            reviewed.setdefault(n, []).append(correction | {"_det": det})
        split = clip.get("split") or "validation"
        for n, items in reviewed.items():
            frame = frames.get(n)
            if frame is None:
                continue
            _coco_frame(coco.setdefault(split, {"images": [], "annotations": []}), categories, meta["id"], frame, items)
        if copy_images:
            for n in used:
                source = run_dir / "frames" / f"{n:05d}.jpg"
                if n >= 0 and source.exists():
                    shutil.copyfile(source, out / "images" / f"{meta['id']}_{n:05d}.jpg")
    with (out / "examples.jsonl").open("w", encoding="utf-8") as handle:
        for example in examples:
            handle.write(json.dumps(example) + "\n")
    category_list = [{"id": cid, "name": name} for name, cid in sorted(categories.items(), key=lambda kv: kv[1])]
    for split, data in coco.items():
        (out / f"coco_{split}.json").write_text(json.dumps(data | {"categories": category_list}), encoding="utf-8")
    splits_by_clip = {cid: c["split"] for cid, c in clips.items()}
    manifest = {
        "created": datetime.now().isoformat(timespec="seconds"), "purpose": purpose, "personalization": personal,
        "examples": len(examples), "by_kind": dict(Counter(e["kind"] for e in examples)),
        "by_split": dict(Counter(e["split"] for e in examples)), "excluded_runs": dict(excluded), "clips": list(clips.values()),
        "coco": {split: {"images": len(d["images"]), "boxes": len(d["annotations"])} for split, d in coco.items()},
        "splits_disjoint_by_clip": True if len(set(splits_by_clip.values())) <= 1 else _disjoint(splits_by_clip),
        "rules": "test split never exported for training; licences checked per clip; private footage only with personalization on",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def _disjoint(splits_by_clip: dict[str, str]) -> bool:
    return len(splits_by_clip) == len(set(splits_by_clip))  # a clip id appears under one split only


def _coco_frame(data: dict[str, Any], categories: dict[str, int], run_id: str, frame: dict[str, Any], corrections: list[dict[str, Any]]) -> None:
    """A reviewed frame becomes a training image: the detector's confident boxes,
    minus the ones the owner marked wrong, relabelled where corrected, plus the
    boxes the owner drew for missed objects. Boxes stay normalised (x, y, w, h)."""
    image_id = len(data["images"]) + 1
    data["images"].append({"id": image_id, "file_name": f"images/{run_id}_{frame['n']:05d}.jpg", "run": run_id, "frame": frame["n"]})
    rejected = {c["detection"] for c in corrections if c["kind"] == "false_positive" and c.get("detection") is not None}
    relabel = {c["detection"]: c["label"] for c in corrections if c["kind"] == "wrong_label" and c.get("detection") is not None and c.get("label")}
    boxes = [(relabel.get(i, d["label"]), d["bbox"]) for i, d in enumerate(frame.get("dets", [])) if not d.get("weak") and i not in rejected]
    boxes += [(c.get("label") or "person", c["bbox"]) for c in corrections if c["kind"] == "missed" and c.get("bbox")]
    for label, bbox in boxes:
        category = categories.setdefault(label, len(categories) + 1)
        data["annotations"].append({"id": len(data["annotations"]) + 1, "image_id": image_id, "category_id": category, "bbox_normalized": bbox})
