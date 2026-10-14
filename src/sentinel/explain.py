"""Explanations for incidents: what Sentinel observed, where, in what order,
which rules fired and why, how confident it is, and which recordings support
it. Built only from stored events, the incident, and the site configuration.

Two kinds of statement are kept apart:
- **observed**: a camera detected an object in a zone at a time (with the
  detector's confidence);
- **inferred**: Sentinel's own reasoning on top of observations, such as
  "the person on camera A is the person later on camera B" (a cross-camera link
  from timing and topology), or "these visits are related".

The text never states intent, identity or certainty that the evidence does not
support. Standard library only."""

from __future__ import annotations

from typing import Any

from .incidents import DISCLAIMER
from .models import SEVERITY_RANK
from .rules import TYPE_LABELS, describe_rule

HEALTH_TYPES = {"camera_offline", "camera_online", "camera_frozen", "camera_ai_failed", "camera_stalled", "camera_restarting",
                "camera_failed", "camera_unverified", "system_health", "scene_condition"}


def _pct(value: float | None) -> str:
    return "unknown" if value is None else f"{round(value * 100)}%"


def explain_incident(incident: dict[str, Any], events: list[dict[str, Any]], config: dict[str, Any], *, notify_thresholds: dict[str, str] | None = None) -> dict[str, Any]:
    events = sorted((event for event in events if event["event_type"] not in HEALTH_TYPES), key=lambda event: event["timestamp_ms"])
    cameras = {camera["id"]: camera for camera in config.get("cameras", [])}
    zones = {zone["id"]: zone | {"camera_id": camera["id"]} for camera in config.get("cameras", []) for zone in camera.get("zones", [])}
    rules = {rule["id"]: rule for rule in config.get("rules", [])}
    zone_names = {zone_id: zone.get("name", zone_id) for zone_id, zone in zones.items()}
    rule_names = {rule_id: rule.get("name") or rule_id.replace("_", " ") for rule_id, rule in rules.items()}
    camera_name = lambda camera_id: cameras.get(camera_id, {}).get("name", camera_id)  # noqa: E731

    # 1. objects
    objects: dict[str, dict[str, Any]] = {}
    for event in events:
        if not event.get("label"):
            continue
        kind = (event.get("details", {}).get("observation") or {}).get("kind") or event["label"]
        entry = objects.setdefault(event["label"], {"category": event["label"], "kinds": [], "confidence_min": 1.0, "confidence_max": 0.0, "detections": 0})
        if kind and kind not in entry["kinds"] and kind != event["label"]:
            entry["kinds"].append(kind)
        entry["confidence_min"] = min(entry["confidence_min"], event["confidence"])
        entry["confidence_max"] = max(entry["confidence_max"], event["confidence"])
        entry["detections"] = max(entry["detections"], (event.get("details", {}).get("observation") or {}).get("track_hits", 0))

    # 2-3. cameras and zones, with first/last times. A camera that only saw the
    # subject through a cross-camera link is listed as inferred.
    camera_rows: dict[str, dict[str, Any]] = {}
    zone_rows: dict[str, dict[str, Any]] = {}
    for event in events:
        row = camera_rows.setdefault(event["camera_id"], {"camera_id": event["camera_id"], "name": camera_name(event["camera_id"]),
                                                          "first_ms": event["timestamp_ms"], "last_ms": event["timestamp_ms"], "basis": "observed"})
        row["last_ms"] = event["timestamp_ms"]
    for event in events:
        for link in (event.get("details", {}).get("subject") or {}).get("links", []):
            for camera_id in (link["from_camera"], link["to_camera"]):
                row = camera_rows.setdefault(camera_id, {"camera_id": camera_id, "name": camera_name(camera_id), "first_ms": link["timestamp_ms"],
                                                         "last_ms": link["timestamp_ms"], "basis": "inferred"})
                if row["basis"] == "inferred":
                    row["first_ms"] = min(row["first_ms"], link["timestamp_ms"])
                    row["last_ms"] = max(row["last_ms"], link["timestamp_ms"])
        if event.get("zone_id"):
            zone = zones.get(event["zone_id"], {})
            zone_row = zone_rows.setdefault(event["zone_id"], {"zone_id": event["zone_id"], "name": zone_names.get(event["zone_id"], event["zone_id"]),
                                                               "type": zone.get("type", ""), "camera_id": zone.get("camera_id", event["camera_id"]),
                                                               "first_ms": event["started_ms"]})
            zone_row["first_ms"] = min(zone_row["first_ms"], event["started_ms"])

    # 4. sequence, observed vs inferred
    sequence = []
    for event in events:
        details = event.get("details", {})
        basis = "observed"
        note = ""
        spans = {zones.get(zone_id, {}).get("camera_id") for zone_id in details.get("sequence", [])} if event["event_type"] == "sequence" else set()
        cross_camera = event["event_type"] == "multi_camera" or len(spans) > 1 or (
            event["event_type"] == "combination" and len((details.get("subject") or {}).get("cameras", [])) > 1)
        if cross_camera:
            basis = "inferred"
            note = "relies on linking sightings on different cameras to one subject"
        if event["event_type"] == "repeated_presence" or details.get("scope") == "site":
            basis = "inferred"
            note = details.get("identity_note", "may involve different people")
        sequence.append({
            "time_ms": event["timestamp_ms"], "started_ms": event["started_ms"], "camera_id": event["camera_id"], "camera": camera_name(event["camera_id"]),
            "zone": zone_names.get(event.get("zone_id", ""), ""), "event_type": event["event_type"], "severity": event["severity"],
            "summary": event["summary"], "confidence": event["confidence"], "basis": basis, "note": note, "event_id": event["event_id"],
            "rule_id": event.get("rule_id", ""), "site_track_id": event.get("site_track_id", ""),
        })

    # 5. rules
    triggered = []
    for rule_id in dict.fromkeys(event["rule_id"] for event in events if event.get("rule_id")):
        rule = rules.get(rule_id)
        hits = [event for event in events if event.get("rule_id") == rule_id]
        downgraded = [event for event in hits if "rule_cooldown" in event.get("reason_codes", []) or "category_alerts_off" in event.get("reason_codes", [])]
        triggered.append({
            "rule_id": rule_id, "name": rule_names.get(rule_id, rule_id), "type": TYPE_LABELS.get(rule["type"], rule["type"]) if rule else "",
            "description": describe_rule(rule, zone_names, rule_names) if rule else {"what": "This rule has since been changed or deleted.", "when": "", "then": ""},
            "matched": [{"time_ms": event["timestamp_ms"], "summary": event["summary"], "details": _public_details(event)} for event in hits],
            "severity": max((event.get("details", {}).get("original_severity", event["severity"]) for event in hits), key=lambda s: SEVERITY_RANK.get(s, 0)),
            "downgraded": [_downgrade_reason(event) for event in downgraded],
        })

    # 6. confidence and identity uncertainty
    links, ambiguous, subjects = [], False, set()
    for event in events:
        subject = event.get("details", {}).get("subject") or {}
        if event.get("site_track_id"):
            subjects.add(event["site_track_id"])
        for link in subject.get("links", []):
            if link["kind"] == "camera_link":
                item = {"from": camera_name(link["from_camera"]), "to": camera_name(link["to_camera"]), "confidence": link["confidence"], "time_ms": link["timestamp_ms"]}
                if item not in links:
                    links.append(item)
        ambiguous = ambiguous or bool(subject.get("ambiguous_with")) or bool(event.get("details", {}).get("correlation_note"))
    detection = [event["confidence"] for event in events if event.get("label")]
    uncertainty = []
    if links:
        weakest = min(link["confidence"] for link in links)
        uncertainty.append(f"Sightings on different cameras were linked to one subject from timing and camera connections (weakest link {_pct(weakest)}). "
                           "This is an inference, not a recognition of who the person is.")
    if ambiguous:
        uncertainty.append("At least one possible link between sightings was too uncertain, so Sentinel kept them apart; the same subject may appear more than once.")
    if len(subjects) > 1:
        uncertainty.append(f"{len(subjects)} separately tracked subjects are involved; they may be the same or different people.")
    if any(event["event_type"] == "repeated_presence" for event in events):
        uncertainty.append("Repeated visits are counted per visit; they may be by the same or different people.")
    if detection and min(detection) < 0.6:
        uncertainty.append(f"Some detections had low confidence (lowest {_pct(min(detection))}); review the recording.")
    if not uncertainty:
        uncertainty.append("All observations come from one camera's continuous tracking; no cross-camera inference was needed.")

    # 7. severity
    peak = incident.get("peak_event_severity") or (max((e["severity"] for e in events), key=lambda s: SEVERITY_RANK[s]) if events else "info")
    reasons = [f"The most serious rule matched was {peak}."]
    behaviours = [kind for kind in incident.get("event_types", []) if kind != "activity"]
    if SEVERITY_RANK.get(incident["severity"], 0) > SEVERITY_RANK.get(peak, 0):
        reasons.append(f"Raised one level to {incident['severity']} because {len(behaviours)} different kinds of behaviour were observed in one incident "
                       f"({', '.join(behaviours)}).")
    if any(item["downgraded"] for item in triggered):
        reasons.append("Some matches were recorded as information only (cooldown or category without alerts), so they did not raise the severity.")
    notified = incident.get("notified_severity")
    reasons.append(f"A notification was sent at {notified} severity." if notified else "No notification was sent for this incident.")
    if notify_thresholds:
        reasons.append("Notification levels by mode: " + ", ".join(f"{mode} {level}" for mode, level in notify_thresholds.items()) + ".")

    # 8. evidence
    clip_starts = incident.get("clip_starts", {})
    evidence = [{"type": "clip", "path": path, "camera": camera_name(clip_starts.get(path, {}).get("camera_id", "")),
                 "started_ms": clip_starts.get(path, {}).get("started_ms")} for path in incident.get("clip_paths", [])]
    evidence += [{"type": "snapshot", "path": event["snapshot_path"], "camera": camera_name(event["camera_id"]), "time_ms": event["timestamp_ms"],
                  "event_id": event["event_id"]} for event in events if event.get("snapshot_path")]

    category_text = ", ".join(f"{row['category']}" + (f" ({', '.join(row['kinds'])})" if row["kinds"] else "") for row in objects.values()) or "no object"
    headline = (f"Sentinel observed {category_text} on {len(camera_rows)} camera(s)"
                + (f", in {', '.join(row['name'] for row in zone_rows.values())}" if zone_rows else "")
                + f". {len(triggered)} rule(s) matched; incident severity {incident['severity']}.")
    return {
        "incident_id": incident["incident_id"],
        "headline": headline,
        "objects": [row | {"confidence_min": round(row["confidence_min"], 3), "confidence_max": round(row["confidence_max"], 3)} for row in objects.values()],
        "cameras": list(camera_rows.values()),
        "zones": list(zone_rows.values()),
        "sequence": sequence,
        "rules": triggered,
        "confidence": {
            "detection_min": round(min(detection), 3) if detection else None,
            "detection_max": round(max(detection), 3) if detection else None,
            "cross_camera_links": links,
            "uncertainty": uncertainty,
        },
        "severity": {"level": incident["severity"], "reasons": reasons},
        "evidence": evidence,
        "statement": DISCLAIMER,
    }


def _downgrade_reason(event: dict[str, Any]) -> str:
    details = event.get("details", {})
    if "rule_cooldown" in event.get("reason_codes", []):
        return f"{event['summary']}: recorded as information, rule cooldown ({details.get('cooldown_remaining_s', '?')} s left)"
    return f"{event['summary']}: recorded as information, alerts are off for this category"


def _public_details(event: dict[str, Any]) -> dict[str, Any]:
    details = event.get("details", {})
    keep = ("dwell_s", "threshold_s", "roam_body_heights", "visit_count", "sequence", "elapsed_s", "cameras", "matched_rules", "identity_note", "original_severity")
    return {key: details[key] for key in keep if key in details}


def notification_reason(events: list[Any], rules: dict[str, dict[str, Any]]) -> str:
    """One line for a push notification: which rule(s) matched."""
    names = []
    for event in events:
        rule = rules.get(getattr(event, "rule_id", ""))
        if rule and event.event_type != "activity":
            name = rule.get("name") or rule["id"].replace("_", " ")
            if name not in names:
                names.append(name)
    return f"Why: rule{'s' if len(names) > 1 else ''} {', '.join(repr(name) for name in names)} matched." if names else ""

