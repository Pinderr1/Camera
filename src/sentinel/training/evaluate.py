"""Compare a candidate detector with the current one through the Test Lab.

Both run on the same labelled clips of one split, with the same fps, rules and
Guardian settings, through `lab.session.execute` (the real stack). Each run is
kept as a normal lab run, so every difference can be replayed side by side in
the dashboard. Only labelled clips count; clips without labels are skipped and
listed. The verdict is deliberately simple and conservative:
  * better:    more found, not more false positives;
  * no_worse:  found and false positives within one percentage point / one frame;
  * worse:     anything else.
A verdict on the `validation` split is a tuning signal only; promotion requires `test`."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from ..lab.catalog import all_clips
from ..lab.session import LabManager
from .registry import ModelRegistry, lab_profiles


def _metrics(summary: dict[str, Any]) -> dict[str, Any]:
    boxes, presence = summary.get("boxes"), summary.get("presence")
    if boxes:
        return {"kind": "boxes", "found": boxes["tp"], "of": boxes["tp"] + boxes["missed"], "false": boxes["fp"],
                "recall": boxes["recall"], "precision": boxes["precision"], "detect_ms_p50": summary["detect_ms"]["p50"]}
    if presence:
        return {"kind": "presence", "found": presence["found"], "of": presence["present_frames"], "false": presence["false_positive_frames"],
                "recall": presence["recall"], "precision": None, "detect_ms_p50": summary["detect_ms"]["p50"]}
    return {"kind": "none"}


def verdict(candidate: list[dict[str, Any]], baseline: list[dict[str, Any]]) -> str:
    found_c, of_c, false_c = (sum(m[k] for m in candidate) for k in ("found", "of", "false"))
    found_b, of_b, false_b = (sum(m[k] for m in baseline) for k in ("found", "of", "false"))
    if not of_c or not of_b:
        return "insufficient_labels"
    recall_c, recall_b = found_c / of_c, found_b / of_b
    if recall_c > recall_b + 0.01 and false_c <= false_b:
        return "better"
    if recall_c >= recall_b - 0.01 and false_c <= false_b + max(1, round(0.01 * of_b)):
        return "no_worse"
    return "worse"


def compare(storage_dir: str | Path, candidate: str, baseline: str = "current", split: str = "test", clip_ids: list[str] | None = None,
            fps: float = 5.0, max_seconds: float = 30.0, species: bool = False, progress=print) -> dict[str, Any]:
    storage = Path(storage_dir)
    lab = LabManager(storage, extra_profiles=lambda: lab_profiles(storage))
    clips = [c for c in all_clips(storage) if c.split == split and Path(c.path).exists() and (not clip_ids or c.id in clip_ids)]
    rows, skipped = [], []
    for clip in clips:
        if clip.labels == "none":
            skipped.append(clip.id)
            continue
        row: dict[str, Any] = {"clip": clip.id, "labels": clip.labels}
        for side, profile in (("baseline", baseline), ("candidate", candidate)):
            view = lab.run_blocking(clip.id, profile=profile, fps=fps, max_seconds=max_seconds, species=species, actor="training.compare")
            if view["state"] != "done":
                raise RuntimeError(f"{clip.id} / {profile}: {view['state']} {view.get('error')}")
            row[side] = _metrics(view["summary"]) | {"run": view["id"]}
            progress(f"{clip.id:22} {side:9} {profile:22} found {row[side].get('found')}/{row[side].get('of')} false {row[side].get('false')}")
        rows.append(row)
    result = {
        "candidate": candidate, "baseline": baseline, "split": split, "fps": fps, "max_seconds": max_seconds,
        "at": datetime.now().isoformat(timespec="seconds"), "clips": rows, "skipped_unlabelled": skipped,
        "verdict": verdict([r["candidate"] for r in rows], [r["baseline"] for r in rows]) if rows else "insufficient_labels",
    }
    ModelRegistry(storage).record_evaluation(candidate, result)
    return result


def render(result: dict[str, Any]) -> str:
    lines = [f"# {result['candidate']} vs {result['baseline']} ({result['split']} split)", "",
             f"Verdict: **{result['verdict']}** ({result['fps']} fps, first {result['max_seconds']} s per clip)", "",
             "| Clip | Labels | Baseline found | Baseline false | Candidate found | Candidate false | Detect ms p50 (b / c) |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in result["clips"]:
        b, c = row["baseline"], row["candidate"]
        lines.append(f"| {row['clip']} | {row['labels']} | {b['found']}/{b['of']} | {b['false']} | {c['found']}/{c['of']} | {c['false']} | {b['detect_ms_p50']} / {c['detect_ms_p50']} |")
    if result["skipped_unlabelled"]:
        lines += ["", "Skipped (no labels): " + ", ".join(result["skipped_unlabelled"])]
    lines += ["", "Lab runs are kept: open them in the dashboard (Lab tab) to replay every difference."]
    return "\n".join(lines)
