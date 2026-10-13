"""Combine session results into report.json / report.md / report.html."""

from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .metrics import f1, ratio

CAVEATS = [
    "Detection precision/recall are computed only on clips with human box annotations (MOTChallenge, CAVIAR), at IoU >= 0.5, person class only, on frames where the detector actually ran.",
    "Expected events are derived by running the annotated boxes and identities through the same zones and rules (the reference pass), plus any hand-written expected events in the manifest. Event matching uses type, camera, zone and a time window; it does not check which person triggered the event.",
    "Zones and rules for benchmark clips are configured for these camera views and short clip lengths; thresholds (e.g. dwell) are shorter than a real deployment.",
    "Synthetic conditions (synthetic_low_light, synthetic_night_ir, synthetic_rain) are image degradations of real daytime footage, not real night or rain recordings.",
    "Clips without annotations report throughput, resource use and alert counts only. No accuracy is claimed for them.",
    "Offline sessions process all cameras on one thread, so their realtime factor is a single-core figure; live mode runs one worker thread per camera. Synthetic-degradation sessions' wall time includes the degradation's own CPU cost (per-clip frame timings exclude it).",
    "Public benchmark footage (street scenes, an indoor lobby) is not residential security footage; real-property validation still requires labeled recordings from the target cameras.",
]


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    clips = [clip | {"session": result["session_id"], "degradation": result.get("degradation")} for result in results for clip in result["clips"]]
    by_condition: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for clip in clips:
        for condition in clip["conditions"] or ["unspecified"]:
            bucket = by_condition[condition]
            bucket["clips"] += 1
            if clip["detection"]:
                for key in ("tp", "fp", "fn", "frames_scored"):
                    bucket[key] += clip["detection"][key]
            for event_type, stats in (clip["events"] or {}).items():
                prefix = "activity_" if event_type == "activity" else "alert_"
                bucket[prefix + "expected"] += stats["expected"]
                bucket[prefix + "predicted"] += stats["predicted"]
                bucket[prefix + "matched"] += stats["matched"]
    conditions = {}
    for name, bucket in sorted(by_condition.items()):
        precision = ratio(bucket["tp"], bucket["tp"] + bucket["fp"])
        recall = ratio(bucket["tp"], bucket["tp"] + bucket["fn"])
        conditions[name] = {
            "clips": bucket["clips"],
            "frames_scored": bucket["frames_scored"],
            "detection_precision": precision,
            "detection_recall": recall,
            "detection_f1": f1(precision, recall),
            "alert_event_precision": ratio(bucket["alert_matched"], bucket["alert_predicted"]),
            "alert_event_recall": ratio(bucket["alert_matched"], bucket["alert_expected"]),
            "activity_precision": ratio(bucket["activity_matched"], bucket["activity_predicted"]),
            "activity_recall": ratio(bucket["activity_matched"], bucket["activity_expected"]),
        }
    sessions = [
        {
            "session": result["session_id"],
            "mode": result["mode"],
            "cameras": len(result["clips"]),
            "degradation": result.get("degradation"),
            "wall_s": result.get("wall_s"),
            "frames": sum(clip["frames_processed"] for clip in result["clips"]),
            "aggregate_fps": round(sum(clip["frames_processed"] for clip in result["clips"]) / result["wall_s"], 2) if result.get("wall_s") else None,
            "video_s": max((clip["video_seconds"] for clip in result["clips"]), default=0),
            "realtime_factor": _realtime_factor(result),
            "resources": result.get("resources"),
        }
        for result in results
    ]
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "host": results[0].get("host") if results else {},
        "caveats": CAVEATS,
        "clips": clips,
        "conditions": conditions,
        "sessions": sessions,
    }


def _realtime_factor(result: dict[str, Any]) -> float | None:
    """>1 means the pipeline processed the recordings faster than real time at the bench fps."""
    video_s = max((clip["video_seconds"] for clip in result["clips"]), default=0)
    return round(video_s / result["wall_s"], 2) if result.get("wall_s") else None


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _num(value: Any, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}" if isinstance(value, float) else str(value)


def _alert_events(clip: dict[str, Any]) -> tuple[int, int, int]:
    expected = predicted = matched = 0
    for event_type, stats in (clip["events"] or {}).items():
        if event_type != "activity":
            expected += stats["expected"]
            predicted += stats["predicted"]
            matched += stats["matched"]
    return expected, predicted, matched


def tables(report: dict[str, Any]) -> list[tuple[str, list[str], list[list[str]]]]:
    detection_rows, event_rows, perf_rows = [], [], []
    for clip in report["clips"]:
        name = f"{clip['clip_id']}" + (f" [{clip['degradation']}]" if clip.get("degradation") else "")
        det = clip["detection"]
        trk = clip["tracking"]
        if det:
            sizes = det["recall_by_size"]
            detection_rows.append([
                name, ", ".join(clip["conditions"]), str(det["frames_scored"]), _pct(det["precision"]), _pct(det["recall"]), _pct(det.get("recall_visible")), _pct(det["f1"]),
                f"{_pct(sizes['small'])} / {_pct(sizes['medium'])} / {_pct(sizes['large'])}",
                f"{trk['mostly_tracked']}/{trk['gt_identities']}", str(trk["mostly_lost"]), str(trk["id_switches"]),
            ])
        else:
            detection_rows.append([name, ", ".join(clip["conditions"]), "unlabeled", "n/a", "n/a", "n/a", "n/a", "n/a", "n/a", "n/a", "n/a"])
        if clip["events"] is not None:
            expected, predicted, matched = _alert_events(clip)
            activity = (clip["events"] or {}).get("activity", {"expected": 0, "predicted": 0, "matched": 0})
            notif = clip["notification_match"] or {}
            event_rows.append([
                name,
                f"{matched}/{expected} ({_pct(ratio(matched, expected))})", f"{predicted - matched} of {predicted} ({_pct(ratio(matched, predicted))} precise)",
                f"{activity['matched']}/{activity['expected']}", f"{activity['predicted'] - activity['matched']}",
                f"{notif.get('matched', 0)}/{notif.get('expected', 0)}", str(notif.get("false", 0)), _num(clip["false_alerts_per_hour"]),
            ])
        else:
            event_rows.append([name, "no labels", f"{sum(v for k, v in clip['event_counts'].items() if k != 'activity')} produced", "n/a",
                               str(clip["event_counts"].get("activity", 0)) + " produced", "n/a", f"{clip['notifications']} sent",
                               _num(clip["false_alerts_per_hour"]) if clip["expect_no_notifications"] else "unverified"])
        timings = clip["timings_ms"]
        perf_rows.append([
            name, f"{clip['resolution'][0]}x{clip['resolution'][1]}", str(clip["frames_processed"]), f"{clip['detector_runs']}",
            _num(timings["detect"]["p50"]), _num(timings["detect"]["p95"]), _num(timings["engine"]["p95"], 2), _num(timings["total"]["p95"]),
            _num(clip["throughput_fps"]),
        ])
    condition_rows = [
        [name, str(c["clips"]), str(c["frames_scored"]), _pct(c["detection_precision"]), _pct(c["detection_recall"]),
         _pct(c["alert_event_precision"]), _pct(c["alert_event_recall"]), _pct(c["activity_recall"])]
        for name, c in report["conditions"].items()
    ]
    session_rows = []
    for session in report["sessions"]:
        resources = session["resources"] or {}
        session_rows.append([
            session["session"] + (f" [{session['degradation']}]" if session["degradation"] else ""), session["mode"], str(session["cameras"]),
            _num(session["aggregate_fps"]), _num(session["realtime_factor"], 2),
            _num(resources.get("process_cpu_percent_of_machine")), _num(resources.get("process_cpu_percent_of_one_core")), _num(resources.get("rss_mb_peak")),
        ])
    return [
        ("Detection and tracking (person, IoU 0.5)", ["Clip", "Conditions", "Frames scored", "Precision", "Recall", "Recall (>=50% visible)", "F1", "Recall small/med/large", "Mostly tracked", "Mostly lost", "ID switches"], detection_rows),
        ("Events vs expected", ["Clip", "Alert events found", "False alert events", "Activity found", "Extra activity", "Notifications matched", "False notifications", "False alerts/h"], event_rows),
        ("Per-clip performance (ms)", ["Clip", "Resolution", "Frames", "Detector runs", "Detect p50", "Detect p95", "Engine p95", "Frame p95", "Throughput fps"], perf_rows),
        ("By condition", ["Condition", "Clips", "Frames scored", "Det precision", "Det recall", "Alert-event precision", "Alert-event recall", "Activity recall"], condition_rows),
        ("Sessions (simultaneous feeds)", ["Session", "Mode", "Cameras", "Aggregate fps", "Realtime factor", "CPU % machine", "CPU % one core", "RSS peak MB"], session_rows),
    ]


def write_reports(report: dict[str, Any], out_dir: Path) -> None:
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    host = report.get("host") or {}
    lines = [
        "# Sentinel real-footage benchmark",
        "",
        f"Generated {report['generated_at']} on {host.get('platform', '?')} ({host.get('logical_cpus', '?')} logical CPUs), OpenCV {host.get('opencv', '?')}.",
        "",
        "## Read this first",
        "",
        *[f"- {caveat}" for caveat in report["caveats"]],
        "",
    ]
    for title, headers, rows in tables(report):
        lines += [f"## {title}", "", "| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
        lines += ["| " + " | ".join(row) + " |" for row in rows]
        lines.append("")
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")

    sections = []
    for title, headers, rows in tables(report):
        head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
        body = "".join("<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>" for row in rows)
        sections.append(f"<h2>{html.escape(title)}</h2><div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>")
    caveats = "".join(f"<li>{html.escape(c)}</li>" for c in report["caveats"])
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sentinel benchmark</title><style>
:root {{ --bg:#f6f7f9; --fg:#16191d; --muted:#5d6570; --line:#d9dde3; --card:#fff; color-scheme: light; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --bg:#0f1215; --fg:#e7eaee; --muted:#9aa3ad; --line:#2c333b; --card:#181c21; color-scheme: dark; }} }}
:root[data-theme="dark"] {{ --bg:#0f1215; --fg:#e7eaee; --muted:#9aa3ad; --line:#2c333b; --card:#181c21; color-scheme: dark; }}
body {{ background:var(--bg); color:var(--fg); font:14px/1.45 system-ui,sans-serif; margin:0; padding:16px; }}
main {{ max-width:1300px; margin:0 auto; }} .muted {{ color:var(--muted); }}
.scroll {{ overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:10px; }}
table {{ border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }}
th, td {{ padding:6px 10px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; }}
th {{ font-weight:600; }} li {{ margin:4px 0; }}
</style></head><body><main>
<h1>Sentinel real-footage benchmark</h1>
<p class="muted">Generated {html.escape(report['generated_at'])} · {html.escape(str(host.get('platform', '')))} · {html.escape(str(host.get('logical_cpus', '')))} logical CPUs</p>
<h2>Read this first</h2><ul>{caveats}</ul>
{''.join(sections)}
</main></body></html>"""
    (out_dir / "report.html").write_text(page, encoding="utf-8")
