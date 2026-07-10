from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .clock import make_tick, synthesize_ticks, tick_interval_s
from .event_io import write_decisions_csv
from .mqtt_bridge import normalize_payload
from .pose_events import PoseEventEngine
from .pose_extractor import extract_features, load_zones_config
from .schemas import DetectorDecision
from .state_machine import NightSafetyStateMachine, load_rules

FALL_STATES = {"possible_fall", "fallen_no_motion", "urgent_alert"}
ROUTINE_URGENT_REASONS = {"bed_exit_no_return", "bathroom_overstay"}
FALL_EVENT_LABELS = {"fall_start", "impact_or_floor_contact", "fallen_state_start"}
FALL_CLIP_LABELS = {"fall"}
NO_FALL_CLIP_LABELS = {"no_fall", "routine_event"}
# Timestamps below this are clip-relative, not epoch; replayed on a fixed
# night-window date so results do not depend on when the replay runs.
EPOCH_MS_MIN = 10**11
SYNTHETIC_BASE = datetime(2026, 1, 1, 23, 0, 0)


@dataclass
class ClipRecording:
    clip_id: str
    path: Path
    frames: list[tuple[int, dict[str, tuple[float, float, float, float]]]]

    @property
    def start_ms(self) -> int:
        return self.frames[0][0] if self.frames else 0

    @property
    def duration_ms(self) -> int:
        return self.frames[-1][0] - self.frames[0][0] if len(self.frames) > 1 else 0


DELIBERATE_FLOOR_SCENARIOS = {"lie_down", "exercise_floor", "lying", "normal_lie_down"}


@dataclass
class ClipResult:
    clip_id: str
    truth_label: str
    impact_ms: int | None
    scenario: str = ""
    frames_total: int = 0
    frames_person: int = 0
    duration_s: float = 0.0
    first_fall_state: str = ""
    first_fall_state_ms: int | None = None
    first_urgent_ms: int | None = None
    detect_latency_s: float | None = None
    urgent_latency_s: float | None = None
    reason_codes: str = ""
    decisions: list[DetectorDecision] = field(default_factory=list)

    @property
    def predicted_fall(self) -> bool:
        return self.first_fall_state_ms is not None

    @property
    def outcome(self) -> str:
        if self.truth_label in FALL_CLIP_LABELS:
            detected = self.detect_latency_s is not None
            return "true_positive" if detected else "false_negative"
        if self.truth_label in NO_FALL_CLIP_LABELS:
            return "false_positive" if self.predicted_fall else "true_negative"
        return "uncertain"


def read_pose_jsonl(path: str | Path) -> ClipRecording:
    path = Path(path)
    frames: list[tuple[int, dict[str, tuple[float, float, float, float]]]] = []
    clip_id = path.stem
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            clip_id = row.get("clip_id") or clip_id
            landmark_map = {
                lm["name"]: (lm["x"], lm["y"], lm.get("z", 0.0), lm.get("visibility", 0.0))
                for lm in row.get("landmarks", [])
            }
            frames.append((int(row["timestamp_ms"]), landmark_map))
    frames.sort(key=lambda frame: frame[0])
    return ClipRecording(clip_id=clip_id, path=path, frames=frames)


def _event_time_local(timestamp_ms: int, clip_start_ms: int) -> str:
    if timestamp_ms >= EPOCH_MS_MIN:
        return datetime.fromtimestamp(timestamp_ms / 1000).isoformat(timespec="seconds")
    offset = timedelta(milliseconds=timestamp_ms - clip_start_ms)
    return (SYNTHETIC_BASE + offset).isoformat(timespec="seconds")


def replay_clip(
    recording: ClipRecording,
    zones_config: dict[str, Any],
    rules: dict[str, Any],
    pad_tail_s: float = 0.0,
) -> tuple[list[DetectorDecision], int]:
    """Run recorded landmark frames through a fresh engine + state machine.

    Features are recomputed from raw landmarks so detection changes re-score
    old recordings. `pad_tail_s` repeats the final frame after the clip ends
    (a benchmark clip that ends on the floor stays on the floor), letting
    persistence and escalation rules run to completion. Returns
    (decisions, frames_with_person)."""
    engine = PoseEventEngine(zones_config)
    machine = NightSafetyStateMachine(rules)
    start_ms = recording.start_ms
    frames = list(recording.frames)
    if frames and pad_tail_s > 0:
        last_ms, last_landmarks = frames[-1]
        frames.extend(
            (last_ms + offset, last_landmarks) for offset in range(100, int(pad_tail_s * 1000) + 100, 100)
        )
    events = []
    frames_person = 0
    for timestamp_ms, landmark_map in recording.frames:
        features = extract_features(landmark_map, timestamp_ms)
        if features.person_present:
            frames_person += 1
    for timestamp_ms, landmark_map in frames:
        features = extract_features(landmark_map, timestamp_ms)
        for payload in engine.update(features):
            payload.setdefault("event_time_local", _event_time_local(timestamp_ms, start_ms))
            events.append(normalize_payload(payload))
    if frames:
        last_ms = frames[-1][0]
        # Trailing tick so duration rules (fallen -> urgent) fire even when the
        # engine goes quiet after its last transition.
        events.append(make_tick(last_ms, _event_time_local(last_ms, start_ms)))
    events = synthesize_ticks(events, tick_interval_s(rules) * 1000)
    return [machine.process(event) for event in events], frames_person


def read_clip_labels(labels_dir: str | Path) -> tuple[dict[str, str], dict[str, int], dict[str, str]]:
    """Returns (clip_id -> primary_label, clip_id -> fall impact_ms, clip_id -> scenario)."""
    labels_dir = Path(labels_dir)
    clip_labels: dict[str, str] = {}
    impacts: dict[str, int] = {}
    scenarios: dict[str, str] = {}
    clips_path = labels_dir / "clips.csv"
    if clips_path.exists():
        with clips_path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("clip_id"):
                    clip_labels[row["clip_id"]] = (row.get("primary_label") or "").strip()
                    scenarios[row["clip_id"]] = (row.get("scenario") or "").strip()
    events_path = labels_dir / "events.csv"
    if events_path.exists():
        with events_path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                clip_id = row.get("clip_id") or ""
                if row.get("event_label") in FALL_EVENT_LABELS:
                    reference = row.get("impact_ms") or row.get("start_ms")
                    if clip_id and reference not in (None, ""):
                        candidate = int(float(reference))
                        if clip_id not in impacts or candidate < impacts[clip_id]:
                            impacts[clip_id] = candidate
    return clip_labels, impacts, scenarios


def _is_fall_urgent(decision: DetectorDecision) -> bool:
    return decision.state == "urgent_alert" and not (set(decision.reason_codes) & ROUTINE_URGENT_REASONS)


def score_clip(
    recording: ClipRecording,
    decisions: list[DetectorDecision],
    frames_person: int,
    truth_label: str,
    impact_ms: int | None,
    detect_window_s: float,
    scenario: str = "",
) -> ClipResult:
    start_ms = recording.start_ms
    result = ClipResult(
        clip_id=recording.clip_id,
        truth_label=truth_label,
        impact_ms=impact_ms,
        scenario=scenario,
        frames_total=len(recording.frames),
        frames_person=frames_person,
        duration_s=recording.duration_ms / 1000.0,
        decisions=decisions,
    )
    reasons: list[str] = []
    for decision in decisions:
        relative_ms = decision.timestamp_ms - start_ms
        if decision.state in FALL_STATES and result.first_fall_state_ms is None:
            result.first_fall_state = decision.state
            result.first_fall_state_ms = relative_ms
            reasons = decision.reason_codes
        if _is_fall_urgent(decision) and result.first_urgent_ms is None:
            result.first_urgent_ms = relative_ms
    result.reason_codes = "|".join(reasons)

    if truth_label in FALL_CLIP_LABELS and impact_ms is not None:
        window_end = impact_ms + detect_window_s * 1000
        if result.first_fall_state_ms is not None and result.first_fall_state_ms <= window_end:
            result.detect_latency_s = max(0.0, (result.first_fall_state_ms - impact_ms) / 1000.0)
        if result.first_urgent_ms is not None:
            result.urgent_latency_s = max(0.0, (result.first_urgent_ms - impact_ms) / 1000.0)
    return result


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return round(ordered[index], 2)


def summarize(results: list[ClipResult]) -> dict[str, Any]:
    fall_results = [r for r in results if r.truth_label in FALL_CLIP_LABELS]
    no_fall_results = [r for r in results if r.truth_label in NO_FALL_CLIP_LABELS]
    detected = [r for r in fall_results if r.detect_latency_s is not None]
    missed = [r for r in fall_results if r.detect_latency_s is None]
    fp_clips = [r for r in no_fall_results if r.predicted_fall]
    fp_deliberate_floor = [r for r in fp_clips if r.scenario in DELIBERATE_FLOOR_SCENARIOS]
    urgent_fp_clips = [r for r in no_fall_results if r.first_urgent_ms is not None]
    urgent_tp_clips = [r for r in fall_results if r.first_urgent_ms is not None]
    detect_latencies = [r.detect_latency_s for r in detected if r.detect_latency_s is not None]
    urgent_latencies = [r.urgent_latency_s for r in urgent_tp_clips if r.urgent_latency_s is not None]
    no_fall_hours = sum(r.duration_s for r in no_fall_results) / 3600.0
    frames_total = sum(r.frames_total for r in results)
    frames_person = sum(r.frames_person for r in results)

    def ratio(numerator: int, denominator: int) -> float | None:
        return round(numerator / denominator, 3) if denominator else None

    return {
        "clips_scored": len(results),
        "fall_clips": len(fall_results),
        "no_fall_clips": len(no_fall_results),
        "unlabeled_or_uncertain_clips": len(results) - len(fall_results) - len(no_fall_results),
        "fall_detected": len(detected),
        "fall_missed": len(missed),
        "fall_recall": ratio(len(detected), len(fall_results)),
        "false_positive_clips": len(fp_clips),
        "false_positive_clips_deliberate_floor": len(fp_deliberate_floor),
        "false_positive_clips_other": len(fp_clips) - len(fp_deliberate_floor),
        "possible_fall_precision": ratio(len(detected), len(detected) + len(fp_clips)),
        "urgent_true_clips": len(urgent_tp_clips),
        "urgent_false_clips": len(urgent_fp_clips),
        "urgent_precision": ratio(len(urgent_tp_clips), len(urgent_tp_clips) + len(urgent_fp_clips)),
        "detect_latency_p50_s": _percentile(detect_latencies, 0.5),
        "detect_latency_p95_s": _percentile(detect_latencies, 0.95),
        "urgent_latency_p50_s": _percentile(urgent_latencies, 0.5),
        "urgent_latency_p95_s": _percentile(urgent_latencies, 0.95),
        "false_positives_per_no_fall_hour": round(len(fp_clips) / no_fall_hours, 2) if no_fall_hours else None,
        "person_present_frame_coverage": ratio(frames_person, frames_total),
    }


PREDICTION_FIELDNAMES = [
    "clip_id",
    "truth_label",
    "scenario",
    "outcome",
    "first_fall_state",
    "first_fall_state_ms",
    "impact_ms",
    "detect_latency_s",
    "urgent_ms",
    "urgent_latency_s",
    "reason_codes",
    "frames_total",
    "frames_person",
    "duration_s",
]


def _prediction_row(result: ClipResult) -> dict[str, object]:
    return {
        "clip_id": result.clip_id,
        "truth_label": result.truth_label,
        "scenario": result.scenario,
        "outcome": result.outcome,
        "first_fall_state": result.first_fall_state,
        "first_fall_state_ms": result.first_fall_state_ms if result.first_fall_state_ms is not None else "",
        "impact_ms": result.impact_ms if result.impact_ms is not None else "",
        "detect_latency_s": result.detect_latency_s if result.detect_latency_s is not None else "",
        "urgent_ms": result.first_urgent_ms if result.first_urgent_ms is not None else "",
        "urgent_latency_s": result.urgent_latency_s if result.urgent_latency_s is not None else "",
        "reason_codes": result.reason_codes,
        "frames_total": result.frames_total,
        "frames_person": result.frames_person,
        "duration_s": round(result.duration_s, 1),
    }


def _write_predictions(path: Path, results: list[ClipResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PREDICTION_FIELDNAMES)
        writer.writeheader()
        for result in results:
            writer.writerow(_prediction_row(result))


def run_replay(
    pose_paths: list[Path],
    zones_config: dict[str, Any],
    rules: dict[str, Any],
    labels_dir: str | Path | None,
    out_dir: str | Path,
    detect_window_s: float = 10.0,
    write_clip_decisions: bool = False,
    pad_tail_s: float = 0.0,
) -> dict[str, Any]:
    clip_labels, impacts, scenarios = read_clip_labels(labels_dir) if labels_dir else ({}, {}, {})
    out_dir = Path(out_dir)
    results: list[ClipResult] = []
    for path in pose_paths:
        recording = read_pose_jsonl(path)
        if not recording.frames:
            print(f"Skipping empty recording: {path}")
            continue
        # Freezing the last frame is only physically sound for fall clips (a
        # fallen person stays down); other clips ended naturally.
        clip_pad_s = pad_tail_s if clip_labels.get(recording.clip_id) in FALL_CLIP_LABELS else 0.0
        decisions, frames_person = replay_clip(recording, zones_config, rules, pad_tail_s=clip_pad_s)
        result = score_clip(
            recording,
            decisions,
            frames_person,
            clip_labels.get(recording.clip_id, ""),
            impacts.get(recording.clip_id),
            detect_window_s,
            scenario=scenarios.get(recording.clip_id, ""),
        )
        results.append(result)
        if write_clip_decisions:
            write_decisions_csv(out_dir / "decisions" / f"{recording.clip_id}.csv", decisions)

    metrics = summarize(results)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True), encoding="utf-8")
    _write_predictions(out_dir / "predictions.csv", results)
    _write_predictions(out_dir / "false_positives.csv", [r for r in results if r.outcome == "false_positive"])
    _write_predictions(out_dir / "false_negatives.csv", [r for r in results if r.outcome == "false_negative"])
    return metrics


def collect_pose_paths(sources: list[str]) -> list[Path]:
    paths: list[Path] = []
    for source in sources:
        path = Path(source)
        if path.is_dir():
            paths.extend(sorted(path.rglob("*.jsonl")))
        elif path.exists():
            paths.append(path)
        else:
            raise SystemExit(f"Pose path not found: {source}")
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay recorded pose JSONL through the detection pipeline and score it.")
    parser.add_argument("--pose", nargs="+", required=True, help="Pose JSONL file(s) or directories.")
    parser.add_argument("--zones", default="config/zones.example.json")
    parser.add_argument("--rules", default="config/monitoring-rules.example.json")
    parser.add_argument("--labels", default="data/labels", help="Directory containing clips.csv and events.csv.")
    parser.add_argument("--out", default="detector_runs/replay", help="Output directory for metrics and predictions.")
    parser.add_argument("--detect-window-s", type=float, default=10.0, help="Seconds after impact for a detection to count.")
    parser.add_argument("--clip-decisions", action="store_true", help="Also write per-clip decision CSVs.")
    parser.add_argument(
        "--pad-tail-s",
        type=float,
        default=0.0,
        help="Repeat the final frame this long so persistence rules finish on clips that end mid-incident.",
    )
    args = parser.parse_args()

    metrics = run_replay(
        collect_pose_paths(args.pose),
        load_zones_config(args.zones),
        load_rules(args.rules),
        args.labels,
        args.out,
        detect_window_s=args.detect_window_s,
        write_clip_decisions=args.clip_decisions,
        pad_tail_s=args.pad_tail_s,
    )
    print(json.dumps(metrics, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
