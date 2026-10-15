"""Operation reports: what was planned, recorded, observed and notified during a
burn operation, with a fingerprint so a copy can be checked later.

A report states what people recorded and what the thermal sensor observed. It
never states that a burn was approved, legal or safe, or that a fire is out.
Standard library only."""

from __future__ import annotations

import hashlib
import html
import json
import time
from datetime import datetime
from typing import Any

from . import operations as ops

KEY_KINDS = ("monitoring_started", "ignition_reported", "post_burn_started", "stood_down", "closed", "emergency_alert", "monitoring_interrupted")
LIMITATIONS = [
    "Thermal observations cover only what the drone camera saw; the coverage figures say how much of the boundary and watch strip that was.",
    "Positions assume flat ground and carry the stated uncertainty.",
    "The thermal camera saturates at its maximum measuring temperature; flaming fuel is reported as at least that hot.",
    "Checklist items, permits, qualifications and weather entered by people are recorded as entered and are not verified by Sentinel.",
    "Weather limits are the burn boss's; Sentinel only compares observations with them.",
]


def build_report(flame: Any, operation: dict[str, Any]) -> dict[str, Any]:
    operation_id = operation["operation_id"]
    view = flame.operation_view(operation_id, observations=5000)
    rows = view["observations"]
    field = view["field"]
    window = (operation["created_ms"], operation["timeline"].get("closed_ms") or ops.now_ms())
    manager = flame._manager(operation["drone_id"]) if operation["drone_id"] else None
    flights = [f for f in (manager.flights(200) if manager else []) if window[0] <= f["started_ms"] <= window[1]]
    device = flame.accessory_for_drone(operation["drone_id"]) if operation["drone_id"] else None
    accessory = flame.accessory_view(device) if device else None
    crew_names = {member["name"].lower() for member in operation["crew"]}
    readiness = operation.get("readiness_at_start") or view.get("readiness")
    report = {
        "report": "Sentinel burn operation report",
        "generated_ms": int(time.time() * 1000),
        "simulated": bool(operation.get("simulated")),
        "operation": {key: operation.get(key) for key in ("operation_id", "name", "status", "created_ms", "created_by", "drone_id", "timeline",
                                                          "acknowledged_incomplete", "post_burn", "stand_down", "counts")},
        "field": {key: field.get(key) for key in ("id", "name", "crop", "area_ha", "perimeter_m", "boundary", "fireguard_m", "edge_m",
                                                  "watch_buffer_m", "sensitive", "monitored_areas")},
        "prescription": operation["prescription"],
        "readiness": {"at": "monitoring start" if operation.get("readiness_at_start") else "report time",
                      "statement": (readiness or {}).get("statement", ""),
                      "items": [{k: item.get(k) for k in ("id", "group", "label", "state", "detail", "by")} for item in (readiness or {}).get("items", [])]},
        "crew": operation["crew"],
        "qualifications": [{k: q.get(k) for k in ("person", "qualification", "reference", "expires", "recorded_by")}
                           for q in flame.ag_records("qualification") if q["person"].lower() in crew_names],
        "authorizations": [{k: a.get(k) for k in ("authorization", "reference", "issued_by", "valid_from", "valid_until", "conditions", "recorded_by")}
                           for a in flame.ag_records("authorization") if not a.get("field_id") or a["field_id"] == operation["field_id"]],
        "equipment": [{k: item.get(k) for k in ("label", "checked", "by", "note")} for item in operation["equipment"]],
        "weather": [{"recorded_ms": r["recorded_ms"], **r.get("weather", {}), "assessment": r.get("assessment")} for r in rows if r["kind"] == "weather"],
        "drone": {"drone_id": operation["drone_id"], "aircraft": manager.aircraft_label() if manager else "",
                  "configuration": flame.configuration(operation["drone_id"]) if operation["drone_id"] else "",
                  "flights": [{k: f.get(k) for k in ("flight_id", "started_ms", "ended_ms", "duration_s", "max_alt_m", "distance_m",
                                                     "battery_start_pct", "battery_min_pct", "safety_events", "simulated")} for f in flights],
                  "watch_requests": [{k: r.get(k) for k in ("request_id", "target_label", "status", "requested_by", "decided_by", "created_ms")}
                                     for r in view["watch_requests"]]},
        "accessory": None if accessory is None else {
            "name": accessory["name"], "profile": accessory["profile_name"], "support": accessory["support"], "serial": accessory["serial"],
            "commissioning": accessory["commissioning"], "compatibility": accessory["compatibility"]["verdict"] if accessory["compatibility"] else "",
            "records_in_window": [{k: r.get(k) for k in ("kind", "recorded_ms", "by", "note")} for r in accessory["records"]
                                  if window[0] <= r["recorded_ms"] <= window[1]]},
        "thermal": {
            "camera": view.get("thermal"), "coverage_at_report": view["coverage"],
            "heat_outside_boundary": sum(1 for r in rows if r["kind"] == "heat_outside_boundary"),
            "heat_at_boundary": sum(1 for r in rows if r["kind"] == "heat_at_boundary"),
            "remaining_heat_post_burn": sum(1 for r in rows if r["kind"] == "residual_heat"),
            "source": "simulated fire and thermal camera" if operation.get("simulated") else "no thermal data (real thermal ingestion not implemented)",
        },
        "events": [{k: r.get(k) for k in ("recorded_ms", "kind", "severity", "text", "zone", "distance_m", "hotspot")} for r in rows
                   if r["severity"] in ("warning", "critical") or r["kind"] in KEY_KINDS],
        "crew_observations": [{k: r.get(k) for k in ("recorded_ms", "text", "position")} for r in rows if r["kind"] == "crew_observation"],
        "limitations": LIMITATIONS,
        "disclaimer": ops.DISCLAIMER,
    }
    body = json.dumps(report, sort_keys=True, default=str)
    report["fingerprint"] = hashlib.sha256(body.encode()).hexdigest()
    return report


def _when(ms: Any) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S") if ms else ""


def render_html(report: dict[str, Any]) -> str:
    e = lambda value: html.escape(str(value if value is not None else ""))  # noqa: E731
    operation, field = report["operation"], report["field"]
    banner = '<p class="sim">SIMULATED OPERATION: no real fire, aircraft or sensor data.</p>' if report["simulated"] else ""

    def table(headers: list[str], rows: list[list[Any]]) -> str:
        if not rows:
            return '<p class="muted">None recorded.</p>'
        head = "".join(f"<th>{e(h)}</th>" for h in headers)
        body = "".join("<tr>" + "".join(f"<td>{e(c)}</td>" for c in row) + "</tr>" for row in rows)
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    timeline = operation.get("timeline") or {}
    parts = [
        f"<h1>{e(operation['name'])}</h1>", banner,
        f"<p>Field <b>{e(field['name'])}</b> ({e(field.get('crop') or 'crop not recorded')}), {e(field['area_ha'])} ha, perimeter {e(field['perimeter_m'])} m, "
        f"fireguard {e(field['fireguard_m'])} m, watch strip {e(field['watch_buffer_m'])} m. Status: <b>{e(operation['status'])}</b>.</p>",
        "<h2>Timeline</h2>",
        table(["Event", "Time"], [[label, _when(timeline.get(key))] for key, label in (
            ("monitoring_started_ms", "Monitoring started"), ("ignition_reported_ms", "Ignition reported by the crew"),
            ("post_burn_started_ms", "Post-burn inspection started"), ("closed_ms", "Closed")) if timeline.get(key)]),
        f"<p>Post-burn result: <b>{e((operation.get('post_burn') or {}).get('result', 'not recorded'))}</b> "
        f"{e((operation.get('post_burn') or {}).get('note', ''))}</p>",
        "<h2>Checklist</h2>", f"<p>{e(report['readiness']['statement'])} (as at {e(report['readiness']['at'])})</p>",
        table(["Item", "State", "Detail", "By"], [[i["label"], i["state"], i["detail"], i["by"]] for i in report["readiness"]["items"]]),
        f"<p>Open items acknowledged when monitoring started: {e(', '.join(operation.get('acknowledged_incomplete') or []) or 'none')}</p>",
        "<h2>Permits and authorizations (as recorded)</h2>",
        table(["Kind", "Reference", "Issued by", "Valid", "Recorded by"],
              [[a["authorization"], a["reference"], a["issued_by"], f"{a['valid_from']} to {a['valid_until']}", a["recorded_by"]] for a in report["authorizations"]]),
        "<h2>Crew</h2>", table(["Name", "Role"], [[m["name"], m["role"]] for m in report["crew"]]),
        table(["Person", "Qualification", "Reference", "Expires"], [[q["person"], q["qualification"], q["reference"], q["expires"]] for q in report["qualifications"]]),
        "<h2>Equipment</h2>", table(["Item", "Checked", "By", "Note"], [[i["label"], "yes" if i["checked"] else "no", i.get("by", ""), i.get("note", "")] for i in report["equipment"]]),
        "<h2>Weather</h2>",
        table(["Time", "Wind m/s", "From °", "Gust", "Humidity %", "Temp °C", "Against limits"],
              [[_when(w["recorded_ms"]), w.get("wind_mps"), w.get("wind_from_deg"), w.get("gust_mps"), w.get("humidity_pct"), w.get("temperature_c"), w.get("assessment")]
               for w in report["weather"]]),
        "<h2>Drone and accessory</h2>",
        f"<p>{e(report['drone']['aircraft'])}, configuration {e(report['drone']['configuration'])}. "
        f"Accessory: {e((report['accessory'] or {}).get('name', 'none'))} ({e((report['accessory'] or {}).get('support', ''))}).</p>",
        table(["Flight", "Start", "Duration s", "Max alt m", "Battery min %", "Safety events"],
              [[f["flight_id"], _when(f["started_ms"]), f.get("duration_s"), f.get("max_alt_m"), f.get("battery_min_pct"), len(f.get("safety_events") or [])]
               for f in report["drone"]["flights"]]),
        "<h2>Thermal watch</h2>",
        f"<p>Source: {e(report['thermal']['source'])}. Heat outside the boundary: {e(report['thermal']['heat_outside_boundary'])}; "
        f"at the boundary: {e(report['thermal']['heat_at_boundary'])}; remaining heat after the burn: {e(report['thermal']['remaining_heat_post_burn'])}.</p>",
        "<h2>Warnings, alerts and key events</h2>",
        table(["Time", "Severity", "What"], [[_when(ev["recorded_ms"]), ev["severity"], ev["text"]] for ev in report["events"]]),
        "<h2>Crew observations</h2>", table(["Time", "Observation"], [[_when(o["recorded_ms"]), o["text"]] for o in report["crew_observations"]]),
        "<h2>Limitations</h2><ul>" + "".join(f"<li>{e(item)}</li>" for item in report["limitations"]) + "</ul>",
        f"<p><b>{e(report['disclaimer'])}</b></p>",
        f"<p class=\"muted\">Generated {_when(report['generated_ms'])}. Fingerprint (SHA-256 of the JSON report): <code>{e(report['fingerprint'])}</code></p>",
    ]
    style = ("body{font:14px/1.45 system-ui,sans-serif;margin:24px;max-width:1000px;color:#111;background:#fff}table{border-collapse:collapse;width:100%;"
             "margin:6px 0 14px}th,td{border:1px solid #ccc;padding:4px 6px;text-align:left;vertical-align:top}th{background:#f2f2f2}"
             ".muted{color:#666}.sim{background:#fff3cd;border:1px solid #e0b800;padding:8px;font-weight:600}h2{margin-top:22px}")
    return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>{e(operation['name'])}: operation report</title>"
            f"<style>{style}</style></head><body>{''.join(parts)}</body></html>")


__all__ = ["LIMITATIONS", "build_report", "render_html"]
