"""Evaluation reports from owner feedback, and incident exports.

Feedback (expected activity, false alarm, genuine concern, uncertain) is used
here to *measure* rules and cameras: how often each one led to a
notification that turned out not to matter. It never retrains a model or
changes a rule by itself.

Exports are for sharing an incident (for example with an insurer): a zip with a
readable report, the structured data, and optionally the recordings, plus
SHA-256 fingerprints so the recipient can check nothing was altered. Camera
addresses, credentials, map coordinates and other incidents are never included."""

from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import time
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .incidents import DISCLAIMER

OUTCOME_LABELS = {"expected_activity": "Expected activity", "false_alarm": "False alarm", "real_concern": "Genuine concern",
                  "unclear": "Uncertain", "": "Not reviewed"}
NUISANCE = {"expected_activity", "false_alarm"}


def incident_rules(store, since_ms: int) -> dict[str, dict[str, Any]]:
    """incident_id -> {rule_ids, cameras} from stored events."""
    links: dict[str, dict[str, Any]] = defaultdict(lambda: {"rule_ids": set(), "cameras": set()})
    for row in store.rule_events(since_ms):
        if row["incident_id"]:
            links[row["incident_id"]]["rule_ids"].add(row["rule_id"])
            links[row["incident_id"]]["cameras"].add(row["camera_id"])
    return links


def performance_report(store, config: dict[str, Any], *, days: float = 30, now_ms: int | None = None) -> dict[str, Any]:
    now_ms = now_ms or int(time.time() * 1000)
    since_ms = now_ms - int(days * 86_400_000)
    rules = {rule["id"]: rule for rule in config.get("rules", [])}
    cameras = {camera["id"]: camera.get("name", camera["id"]) for camera in config.get("cameras", [])}
    incidents = store.search_incidents(since_ms=since_ms, limit=1000)
    links = incident_rules(store, since_ms)
    firing: Counter = Counter()
    downgraded: Counter = Counter()
    for row in store.rule_events(since_ms):
        firing[row["rule_id"]] += 1
        if row["severity"] == "info":
            downgraded[row["rule_id"]] += 1

    def blank() -> dict[str, Any]:
        return {"incidents": 0, "notified": 0, "outcomes": Counter(), "notified_outcomes": Counter()}

    by_rule: dict[str, dict[str, Any]] = defaultdict(blank)
    by_camera: dict[str, dict[str, Any]] = defaultdict(blank)
    for incident in incidents:
        outcome = incident.get("resolution", "")
        notified = bool(incident.get("notified_severity"))
        info = links.get(incident["incident_id"], {"rule_ids": set(), "cameras": set(incident.get("cameras", []))})
        for bucket in [by_rule[rule_id] for rule_id in info["rule_ids"]] + [by_camera[camera_id] for camera_id in (info["cameras"] or incident.get("cameras", []))]:
            bucket["incidents"] += 1
            bucket["outcomes"][outcome] += 1
            if notified:
                bucket["notified"] += 1
                bucket["notified_outcomes"][outcome] += 1

    def summarise(bucket: dict[str, Any]) -> dict[str, Any]:
        reviewed = sum(count for outcome, count in bucket["notified_outcomes"].items() if outcome)
        nuisance = sum(bucket["notified_outcomes"][outcome] for outcome in NUISANCE)
        return {
            "incidents": bucket["incidents"], "notified": bucket["notified"],
            "outcomes": {OUTCOME_LABELS[key]: bucket["outcomes"].get(key, 0) for key in OUTCOME_LABELS},
            "reviewed_notifications": reviewed, "nuisance_notifications": nuisance,
            "nuisance_rate": round(nuisance / reviewed, 2) if reviewed else None,
            "genuine_rate": round(bucket["notified_outcomes"]["real_concern"] / reviewed, 2) if reviewed else None,
        }

    rule_rows = []
    for rule_id in sorted(set(rules) | set(by_rule) | set(firing)):
        rule = rules.get(rule_id, {})
        rule_rows.append({"rule_id": rule_id, "name": rule.get("name") or rule_id.replace("_", " "), "enabled": rule.get("enabled", True) if rule else None,
                          "deleted": not rule, "matches": firing.get(rule_id, 0), "recorded_only": downgraded.get(rule_id, 0)} | summarise(by_rule[rule_id]))
    camera_rows = [{"camera_id": camera_id, "name": cameras.get(camera_id, camera_id)} | summarise(by_camera[camera_id])
                   for camera_id in sorted(set(cameras) | set(by_camera))]
    missed = store.list_missed_reports(since_ms=since_ms)
    missed_by_camera = Counter(report["camera_id"] for report in missed)
    for row in camera_rows:
        row["missed_reports"] = missed_by_camera.get(row["camera_id"], 0)
    reviewed = [incident for incident in incidents if incident.get("resolution")]
    return {
        "days": days, "since_ms": since_ms, "until_ms": now_ms,
        "totals": {"incidents": len(incidents), "notified": sum(1 for i in incidents if i.get("notified_severity")),
                   "reviewed": len(reviewed), "outcomes": {OUTCOME_LABELS[key]: sum(1 for i in incidents if i.get("resolution", "") == key) for key in OUTCOME_LABELS},
                   "missed_reports": len(missed)},
        "rules": rule_rows, "cameras": camera_rows,
        "missed": [{key: report.get(key) for key in ("report_id", "occurred_ms", "camera_id", "zone_id", "category", "description", "sentinel_recorded")}
                   | {"camera": cameras.get(report["camera_id"], report["camera_id"])} for report in missed[:50]],
        "note": "Nuisance rate = notifications later marked expected activity or false alarm, out of the reviewed notifications. "
                "Feedback is used to evaluate rules; it does not retrain or change anything automatically.",
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evidence_status(media_root: Path, paths: list[str], recorded: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for path in paths:
        target = media_root / path
        expected = recorded.get(path, {}).get("sha256")
        if not target.is_file():
            state, current = "missing (deleted by retention or disk clean-up)", None
        else:
            current = _sha256(target)
            state = "not fingerprinted when recorded" if not expected else "intact" if current == expected else "CHANGED since it was recorded"
        rows.append({"path": path, "sha256_recorded": expected, "sha256_now": current, "status": state})
    return rows


def export_incident(
    incident: dict[str, Any],
    events: list[dict[str, Any]],
    explanation: dict[str, Any],
    *,
    media_root: Path,
    recorded_hashes: dict[str, dict[str, Any]],
    site_name: str,
    actor: str,
    include_media: bool,
) -> bytes:
    media = list(dict.fromkeys(list(incident.get("clip_paths", [])) + [event["snapshot_path"] for event in events if event.get("snapshot_path")]))
    evidence = evidence_status(media_root, media, recorded_hashes)
    exported_ms = int(time.time() * 1000)
    public_events = [{key: event.get(key) for key in ("event_id", "event_type", "severity", "camera_id", "zone_id", "rule_id", "timestamp_ms",
                                                         "started_ms", "confidence", "summary", "label", "reason_codes", "snapshot_path", "clip_path")}
                     for event in sorted(events, key=lambda item: item["timestamp_ms"])]
    report = {
        "format": "sentinel-incident-export/1", "exported_ms": exported_ms, "exported_by": actor, "site": site_name,
        "incident": {key: incident.get(key) for key in ("incident_id", "title", "severity", "status", "opened_ms", "updated_ms", "closed_ms", "cameras",
                                                         "zones", "labels", "event_types", "notified_severity", "acknowledged_ms", "acknowledged_by",
                                                         "resolution", "resolved_by", "protected", "notes")},
        "outcome": OUTCOME_LABELS.get(incident.get("resolution", ""), incident.get("resolution", "")),
        "explanation": explanation, "events": public_events, "evidence": evidence, "media_included": include_media,
        "statement": DISCLAIMER,
    }
    snapshots = {}
    if include_media:
        for row in evidence:
            if row["path"].endswith(".jpg") and row["sha256_now"]:
                snapshots[row["path"]] = base64.b64encode((media_root / row["path"]).read_bytes()).decode()
    files: dict[str, bytes] = {
        "report.json": json.dumps(report, indent=1).encode(),
        "report.html": _report_html(report, snapshots).encode(),
    }
    if include_media:
        for row in evidence:
            if row["sha256_now"]:
                files[f"evidence/{row['path']}"] = (media_root / row["path"]).read_bytes()
    manifest = {"exported_ms": exported_ms, "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr("manifest.json", json.dumps(manifest, indent=1))
    return buffer.getvalue()


def _when(ms: int | None) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S") if ms else ""


def _report_html(report: dict[str, Any], snapshots: dict[str, str]) -> str:
    e = html.escape
    incident, explanation = report["incident"], report["explanation"]
    rows = "".join(
        f"<tr><td>{_when(step['time_ms'])}</td><td>{e(step['camera'])}</td><td>{e(step['zone'])}</td><td>{e(step['summary'])}</td>"
        f"<td>{round(step['confidence'] * 100)}%</td><td>{e(step['basis'])}{(' – ' + e(step['note'])) if step['note'] else ''}</td></tr>"
        for step in explanation["sequence"])
    rules = "".join(f"<li><b>{e(rule['name'])}</b>: {e(rule['description']['what'])}; {e(rule['description']['when'])}.</li>" for rule in explanation["rules"])
    evidence = "".join(f"<tr><td>{e(row['path'])}</td><td>{e(row['status'])}</td><td><code>{e(row['sha256_now'] or '')}</code></td></tr>" for row in report["evidence"])
    images = "".join(f"<figure><img src='data:image/jpeg;base64,{data}' alt='{e(path)}'><figcaption>{e(path)}</figcaption></figure>" for path, data in snapshots.items())
    notes = "".join(f"<li>{_when(note['ms'])} {e(note['by'])}: {e(note['text'])}</li>" for note in incident.get("notes") or [])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Incident {e(incident['incident_id'])}</title>
<style>body{{font:14px/1.45 system-ui,sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#16191d}}table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid #ddd;padding:4px 6px;text-align:left;vertical-align:top}}img{{max-width:100%}}code{{font-size:11px;word-break:break-all}}</style></head><body>
<h1>{e(incident['title'])}</h1>
<p>{e(report['site'])} · incident {e(incident['incident_id'])} · {e(incident['severity'])} · opened {_when(incident['opened_ms'])} · outcome: {e(report['outcome'])}</p>
<p>{e(explanation['headline'])}</p>
<h2>What happened</h2><table><tr><th>Time</th><th>Camera</th><th>Zone</th><th>Observation</th><th>Confidence</th><th>Basis</th></tr>{rows}</table>
<h2>Why it was raised</h2><ul>{rules}</ul><p>{e(' '.join(explanation['severity']['reasons']))}</p>
<h2>Uncertainty</h2><ul>{''.join(f'<li>{e(item)}</li>' for item in explanation['confidence']['uncertainty'])}</ul>
<h2>Notes</h2><ul>{notes or '<li>None</li>'}</ul>
<h2>Evidence</h2><table><tr><th>File</th><th>Integrity</th><th>SHA-256</th></tr>{evidence}</table>{images}
<p><i>{e(report['statement'])}</i> Exported {_when(report['exported_ms'])} by {e(report['exported_by'])}.</p></body></html>"""
