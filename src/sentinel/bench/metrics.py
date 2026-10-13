"""Benchmark metrics: frame-level detection, track continuity, event matching,
and runtime performance. Pure Python so it is unit-testable without video."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..models import BBox
from ..tracker import iou
from .labels import GTBox

SIZE_BUCKETS = (("small", 0.0, 0.10), ("medium", 0.10, 0.25), ("large", 0.25, 9.0))  # by box height / frame height
VISIBILITY_BUCKETS = (("<25%", 0.0, 0.25), ("25-50%", 0.25, 0.5), ("50-75%", 0.5, 0.75), (">=75%", 0.75, 9.0))


def size_bucket(bbox: BBox) -> str:
    for name, low, high in SIZE_BUCKETS:
        if low <= bbox[3] < high:
            return name
    return "large"


def visibility_bucket(visibility: float) -> str:
    for name, low, high in VISIBILITY_BUCKETS:
        if low <= visibility < high:
            return name
    return ">=75%"


def count_group_boxes(predictions: list[BBox], unmatched: list[int], ground_truth: list[GTBox]) -> int:
    """Unmatched predictions whose box contains the centres of two or more
    annotated people: several people reported as one detection."""
    centres = [(gt.bbox[0] + gt.bbox[2] / 2, gt.bbox[1] + gt.bbox[3] / 2) for gt in ground_truth if not gt.ignore]
    groups = 0
    for index in unmatched:
        x, y, w, h = predictions[index]
        if sum(1 for cx, cy in centres if x <= cx <= x + w and y <= cy <= y + h) >= 2:
            groups += 1
    return groups


def ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def f1(precision: float | None, recall: float | None) -> float | None:
    if not precision or not recall:
        return None
    return round(2 * precision * recall / (precision + recall), 4)


def match_boxes(
    predictions: list[BBox], ground_truth: list[GTBox], iou_threshold: float = 0.5
) -> tuple[list[tuple[int, GTBox]], list[int], list[GTBox]]:
    """Greedy IoU matching. Returns (matches, unmatched prediction indexes,
    missed scored boxes). Predictions that land on ignore regions are dropped."""
    scored = [box for box in ground_truth if not box.ignore]
    ignored = [box for box in ground_truth if box.ignore]
    pairs = sorted(
        ((iou(pred, gt.bbox), p, g) for p, pred in enumerate(predictions) for g, gt in enumerate(scored)),
        reverse=True,
    )
    used_p: set[int] = set()
    used_g: set[int] = set()
    matches: list[tuple[int, GTBox]] = []
    for overlap, p, g in pairs:
        if overlap < iou_threshold:
            break
        if p in used_p or g in used_g:
            continue
        used_p.add(p)
        used_g.add(g)
        matches.append((p, scored[g]))
    unmatched = []
    for p, pred in enumerate(predictions):
        if p in used_p:
            continue
        if any(iou(pred, box.bbox) >= iou_threshold * 0.5 or _covered(pred, box.bbox) for box in ignored):
            continue
        unmatched.append(p)
    missed = [gt for g, gt in enumerate(scored) if g not in used_g]
    return matches, unmatched, missed


def _covered(pred: BBox, region: BBox) -> bool:
    """Prediction mostly inside a don't-care region."""
    x0, y0 = max(pred[0], region[0]), max(pred[1], region[1])
    x1, y1 = min(pred[0] + pred[2], region[0] + region[2]), min(pred[1] + pred[3], region[1] + region[3])
    area = pred[2] * pred[3]
    return area > 0 and max(0.0, x1 - x0) * max(0.0, y1 - y0) / area >= 0.5


@dataclass
class DetectionStats:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    frames: int = 0
    tp_visible: int = 0
    fn_visible: int = 0
    group_boxes: int = 0
    by_visibility: dict[str, list[int]] = field(default_factory=lambda: {name: [0, 0] for name, _, _ in VISIBILITY_BUCKETS})
    by_size: dict[str, list[int]] = field(default_factory=lambda: {name: [0, 0] for name, _, _ in SIZE_BUCKETS})  # [tp, fn]

    def add(self, matches: list, unmatched: list, missed: list[GTBox], group_boxes: int = 0) -> None:
        self.frames += 1
        self.group_boxes += group_boxes
        self.tp += len(matches)
        self.fp += len(unmatched)
        self.fn += len(missed)
        for _, gt in matches:
            self.by_size[size_bucket(gt.bbox)][0] += 1
            self.tp_visible += gt.visibility >= 0.5
            self.by_visibility[visibility_bucket(gt.visibility)][0] += 1
        for gt in missed:
            self.by_visibility[visibility_bucket(gt.visibility)][1] += 1
            self.by_size[size_bucket(gt.bbox)][1] += 1
            self.fn_visible += gt.visibility >= 0.5

    def summary(self) -> dict[str, Any]:
        precision = ratio(self.tp, self.tp + self.fp)
        recall = ratio(self.tp, self.tp + self.fn)
        return {
            "frames_scored": self.frames,
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "precision": precision,
            "recall": recall,
            "f1": f1(precision, recall),
            "recall_visible": ratio(self.tp_visible, self.tp_visible + self.fn_visible),
            "fp_per_frame": ratio(self.fp, self.frames),
            "recall_by_size": {name: ratio(tp, tp + fn) for name, (tp, fn) in self.by_size.items()},
            "recall_by_visibility": {name: ratio(tp, tp + fn) for name, (tp, fn) in self.by_visibility.items()},
            "group_boxes": self.group_boxes,
            "group_box_share_of_fp": ratio(self.group_boxes, self.fp),
            "gt_boxes_by_size": {name: tp + fn for name, (tp, fn) in self.by_size.items()},
        }


@dataclass
class TrackingStats:
    """Continuity of confirmed tracks against annotated identities."""

    id_switches: int = 0
    _last_track_for_gt: dict[str, str] = field(default_factory=dict)
    _gt_frames: dict[str, int] = field(default_factory=dict)
    _gt_matched: dict[str, int] = field(default_factory=dict)
    _tracks_per_gt: dict[str, set] = field(default_factory=dict)

    def add(self, matches: list[tuple[str, GTBox]], scored_gt: Iterable[GTBox]) -> None:
        for gt in scored_gt:
            self._gt_frames[gt.track_id] = self._gt_frames.get(gt.track_id, 0) + 1
        for track_id, gt in matches:
            self._gt_matched[gt.track_id] = self._gt_matched.get(gt.track_id, 0) + 1
            self._tracks_per_gt.setdefault(gt.track_id, set()).add(track_id)
            previous = self._last_track_for_gt.get(gt.track_id)
            if previous is not None and previous != track_id:
                self.id_switches += 1
            self._last_track_for_gt[gt.track_id] = track_id

    def summary(self, min_frames: int = 5) -> dict[str, Any]:
        eligible = [gt for gt, frames in self._gt_frames.items() if frames >= min_frames]
        coverage = {gt: self._gt_matched.get(gt, 0) / self._gt_frames[gt] for gt in eligible}
        mostly_tracked = sum(1 for value in coverage.values() if value >= 0.8)
        mostly_lost = sum(1 for value in coverage.values() if value < 0.2)
        fragments = [len(self._tracks_per_gt.get(gt, ())) for gt in eligible if gt in self._tracks_per_gt]
        return {
            "gt_identities": len(eligible),
            "mostly_tracked": mostly_tracked,
            "mostly_lost": mostly_lost,
            "never_tracked": sum(1 for gt in eligible if gt not in self._gt_matched),
            "id_switches": self.id_switches,
            "mean_tracks_per_identity": round(sum(fragments) / len(fragments), 2) if fragments else None,
        }


@dataclass(frozen=True)
class ExpectedEvent:
    event_type: str
    time_s: float
    camera_id: str = ""
    zone_id: str = ""
    window_s: tuple[float, float] = (-2.0, 6.0)
    source: str = "manual"
    note: str = ""


def match_events(
    predicted: list[dict[str, Any]], expected: list[ExpectedEvent], start_ms: int
) -> dict[str, Any]:
    """One-to-one matching on (type, camera, zone) within each expected
    event's tolerance window, closest in time first. Identity is not used:
    with several people in a zone, any matching event counts."""
    candidates = []
    for e_index, exp in enumerate(expected):
        for p_index, pred in enumerate(predicted):
            if pred["event_type"] != exp.event_type:
                continue
            if exp.camera_id and pred["camera_id"] != exp.camera_id:
                continue
            if exp.zone_id and pred.get("zone_id") != exp.zone_id:
                continue
            delta = (pred["timestamp_ms"] - start_ms) / 1000 - exp.time_s
            if exp.window_s[0] <= delta <= exp.window_s[1]:
                candidates.append((abs(delta), e_index, p_index, delta))
    used_e: set[int] = set()
    used_p: set[int] = set()
    latencies: dict[str, list[float]] = {}
    matched = []
    for _, e_index, p_index, delta in sorted(candidates):
        if e_index in used_e or p_index in used_p:
            continue
        used_e.add(e_index)
        used_p.add(p_index)
        latencies.setdefault(expected[e_index].event_type, []).append(delta)
        matched.append((e_index, p_index, round(delta, 2)))

    types = sorted({exp.event_type for exp in expected} | {pred["event_type"] for pred in predicted})
    per_type = {}
    for event_type in types:
        tp = sum(1 for e, _, _ in matched if expected[e].event_type == event_type)
        n_exp = sum(1 for exp in expected if exp.event_type == event_type)
        n_pred = sum(1 for pred in predicted if pred["event_type"] == event_type)
        precision, recall = ratio(tp, n_pred), ratio(tp, n_exp)
        per_type[event_type] = {
            "expected": n_exp,
            "predicted": n_pred,
            "matched": tp,
            "missed": n_exp - tp,
            "false": n_pred - tp,
            "precision": precision,
            "recall": recall,
            "latency_s": percentiles(latencies.get(event_type, [])),
        }
    return {
        "per_type": per_type,
        "matched": matched,
        "missed": [e for e in range(len(expected)) if e not in used_e],
        "false": [p for p in range(len(predicted)) if p not in used_p],
    }


def percentiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"n": 0, "p50": None, "p95": None, "mean": None, "max": None}
    ordered = sorted(values)

    def pick(fraction: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))], 3)

    return {"n": len(ordered), "p50": pick(0.5), "p95": pick(0.95), "mean": round(sum(ordered) / len(ordered), 3), "max": round(ordered[-1], 3)}
