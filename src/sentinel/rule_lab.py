"""Try a security rule before enabling it.

Every test runs the candidate rule through the real tracker, cross-camera
correlator, temporal engine, incident manager and alert policy (`replay`), in
virtual time, on a throwaway database. Three sources of activity:

- `walk`: a simulated subject walking through chosen zones with chosen
  timings. Works on any site; cross-camera links follow the site's camera
  connections, so a walk that is too fast or slow for a connection is (correctly)
  not linked.
- `scenario`: one of the scripted scenarios in `config/scenarios/` (they use the
  example site's camera ids).
- `history`: recorded activity. Stored zone visits from the last days are turned
  back into subjects that stand at a point inside each zone for the recorded
  time. This approximates real movement (no positions are stored), so dwell
  rules that limit roaming and appearance-based linking are only estimated.

Nothing here changes the live configuration."""

from __future__ import annotations

import copy
from datetime import datetime
from pathlib import Path
from typing import Any

from senior_safety.pose_events import point_in_polygon

from .config import validate_config, zone_index
from .replay import run_scenario
from .simulation import Scenario

SCENARIO_DIR = Path(__file__).resolve().parents[2] / "config" / "scenarios"
MAX_HISTORY_DAYS = 14
MAX_HISTORY_GROUPS = 150


def interior_point(polygon: list[list[float]]) -> tuple[float, float]:
    """A point inside the polygon, as close to its centroid as possible."""
    cx = sum(point[0] for point in polygon) / len(polygon)
    cy = sum(point[1] for point in polygon) / len(polygon)
    if point_in_polygon(cx, cy, polygon):
        return cx, cy
    xs, ys = [point[0] for point in polygon], [point[1] for point in polygon]
    best = None
    for i in range(1, 30):
        for j in range(1, 30):
            x = min(xs) + (max(xs) - min(xs)) * i / 30
            y = min(ys) + (max(ys) - min(ys)) * j / 30
            if point_in_polygon(x, y, polygon):
                distance = (x - cx) ** 2 + (y - cy) ** 2
                if best is None or distance < best[0]:
                    best = (distance, x, y)
    return (best[1], best[2]) if best else (cx, cy)


def walk_scenario(config: dict[str, Any], steps: list[dict[str, Any]], label: str = "person") -> Scenario:
    """`steps`: [{"zone_id", "stay_s", "gap_s"}]: stand in each zone for `stay_s`,
    then take `gap_s` to reach the next (unseen, if the next zone is on another camera)."""
    zones = zone_index(config)
    path, t = [], 2.0
    for index, step in enumerate(steps):
        zone = zones.get(step.get("zone_id", ""))
        if zone is None:
            raise ValueError(f"unknown zone {step.get('zone_id')!r}")
        x, y = interior_point(zone["polygon"])
        stay = max(1.0, float(step.get("stay_s", 10)))
        path += [{"t": round(t, 2), "camera": zone["camera_id"], "x": x, "y": y}, {"t": round(t + stay, 2), "camera": zone["camera_id"], "x": x, "y": y}]
        t += stay + (max(0.5, float(step.get("gap_s", 5))) if index < len(steps) - 1 else 0)
    if not path:
        raise ValueError("a simulated walk needs at least one step")
    path.append({"t": round(t + 0.01, 2), "camera": None})
    return Scenario(name="simulated walk", duration_s=t + 1, actors=[{"id": "walker", "label": label, "path": path, "noise": 0.002}], loop=False)


def history_scenarios(config: dict[str, Any], visits: list[dict[str, Any]], *, gap_s: float = 300) -> list[tuple[int, Scenario]]:
    """Recorded visits -> scenarios of nearby-in-time subjects, each starting a little
    before its first visit (so time-of-day conditions use the recorded clock)."""
    zones = zone_index(config)
    by_track: dict[str, list[dict[str, Any]]] = {}
    for visit in sorted(visits, key=lambda item: item["start_ms"]):
        if visit["zone_id"] in zones:
            by_track.setdefault(visit["site_track_id"], []).append(visit)
    subjects = []
    for track_id, items in by_track.items():
        # Site-track ids restart with the service, so one id can name several
        # subjects days apart: split where the visits are far apart in time.
        chunk: list[dict[str, Any]] = []
        for visit in items:
            if chunk and visit["start_ms"] - max(item["end_ms"] for item in chunk) > 600_000:
                subjects.append((chunk[0]["start_ms"], max(item["end_ms"] for item in chunk), f"{track_id}@{chunk[0]['start_ms']}", chunk))
                chunk = []
            chunk.append(visit)
        if chunk:
            subjects.append((chunk[0]["start_ms"], max(item["end_ms"] for item in chunk), f"{track_id}@{chunk[0]['start_ms']}", chunk))
    subjects.sort()
    groups: list[list[tuple[int, int, str, list[dict[str, Any]]]]] = []
    for subject in subjects:
        if groups and subject[0] - max(item[1] for item in groups[-1]) <= gap_s * 1000:
            groups[-1].append(subject)
        else:
            groups.append([subject])
    result = []
    for group in groups[:MAX_HISTORY_GROUPS]:
        start_ms = group[0][0] - 2000
        actors = []
        for _, _, key, items in group:
            path, last_t = [], 0.0
            for visit in items:
                zone = zones[visit["zone_id"]]
                x, y = interior_point(zone["polygon"])
                begin = max((visit["start_ms"] - start_ms) / 1000, last_t + 0.2)
                end = max(begin + 1.0, (visit["end_ms"] - start_ms) / 1000)
                path += [{"t": round(begin, 2), "camera": zone["camera_id"], "x": x, "y": y}, {"t": round(end, 2), "camera": zone["camera_id"], "x": x, "y": y}]
                last_t = end
            path.append({"t": round(last_t + 0.01, 2), "camera": None})
            actors.append({"id": key, "label": items[0]["label"], "path": path})
        duration = max(actor["path"][-1]["t"] for actor in actors) + 1
        result.append((start_ms, Scenario(name="recorded activity", duration_s=duration, actors=actors, loop=False)))
    return result


def config_with_rule(config: dict[str, Any], rule: dict[str, Any]) -> dict[str, Any]:
    trial = copy.deepcopy({key: value for key, value in config.items() if not key.startswith("_") or key == "_config_dir"})
    candidate = dict(rule, enabled=True)
    trial["rules"] = [existing for existing in trial.get("rules", []) if existing["id"] != candidate["id"]] + [candidate]
    for channel in trial.get("alerts", {}).get("channels", []):
        channel["enabled"] = False
    trial["drone"] = {"enabled": False}
    validate_config(trial)
    return trial


def test_rule(
    config: dict[str, Any],
    rule: dict[str, Any],
    *,
    source: str,
    arm_mode: str = "away",
    start: datetime | None = None,
    steps: list[dict[str, Any]] | None = None,
    label: str = "person",
    scenario: str | None = None,
    visits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rule = dict(rule)
    rule.setdefault("id", "candidate_rule")
    trial = config_with_rule(config, rule)
    if source == "walk":
        runs = [(start or datetime.now(), walk_scenario(trial, steps or [], label))]
    elif source == "scenario":
        path = SCENARIO_DIR / f"{scenario}.json"
        if not scenario or not path.is_file() or path.parent != SCENARIO_DIR:
            raise ValueError("unknown scenario")
        runs = [(start or datetime.now(), Scenario.load(path))]
    elif source == "history":
        runs = [(datetime.fromtimestamp(ms / 1000), item) for ms, item in history_scenarios(trial, visits or [])]
    else:
        raise ValueError("source must be walk, scenario or history")
    fired, incidents, notifications, others, notes = [], [], [], {}, []
    unlinked = 0
    for begin, item in runs:
        result = run_scenario(trial, item, fps=5 if source != "history" else 2, start=begin, arm_mode=arm_mode, tail_s=30)
        hits = [event for event in result["events"] if event["rule_id"] == rule["id"]]
        fired += hits
        hit_incidents = {event["incident_id"] for event in hits if event["incident_id"]}
        incidents += [incident for incident in result["incidents"] if incident["incident_id"] in hit_incidents]
        notifications += [n for n in result["notifications"] if n["incident_id"] in hit_incidents]
        for event in result["events"]:
            if event["rule_id"] and event["rule_id"] != rule["id"]:
                others[event["rule_id"]] = others.get(event["rule_id"], 0) + 1
        if source == "walk":
            people = [site for site in result["site_tracks"] if site["label"] == label]
            cameras = {step_camera for step_camera in (zone_index(trial)[step["zone_id"]]["camera_id"] for step in steps or [])}
            if len(cameras) > 1 and len(people) > 1:
                unlinked += 1
                notes.append("The subject was not linked between cameras: no camera connection matches these zones and travel times "
                             "(Setup → Connections), so rules that need one subject across cameras cannot fire.")
            if result["ambiguous_links"]:
                notes.append("A possible cross-camera link was too uncertain and was refused.")
    if source == "history":
        notes.append(f"Replayed {len(runs)} recorded activity period(s). Recorded visits keep zones and times, not exact positions, "
                     "so movement-based limits (such as staying within a few body heights) are estimated.")
        if not runs:
            notes.append("No recorded activity in the chosen period yet.")
    return {
        "rule_id": rule["id"], "source": source, "arm_mode": arm_mode, "runs": len(runs),
        "fired": [{key: event[key] for key in ("event_id", "timestamp_ms", "severity", "summary", "camera_id", "zone_id", "confidence", "reason_codes")}
                  for event in fired],
        "incidents": [{key: incident[key] for key in ("incident_id", "title", "severity", "opened_ms", "cameras")} for incident in incidents],
        "notifications": [{key: n[key] for key in ("title", "severity", "timestamp_ms")} for n in notifications],
        "other_rules_fired": others,
        "notes": notes,
        "verdict": _verdict(fired, notifications, source, len(runs)),
    }


def _verdict(fired: list, notifications: list, source: str, runs: int) -> str:
    if not fired:
        return "The rule did not fire on this activity." if runs else "There was no activity to test against."
    severities = sorted({event["severity"] for event in fired})
    text = f"The rule fired {len(fired)} time(s) ({', '.join(severities)})"
    text += f" and {len(notifications)} notification(s) would have been sent." if notifications else ", without sending a notification in this mode."
    if source == "history":
        text += " Review these against what really happened before enabling it."
    return text


def scenario_names() -> list[str]:
    return sorted(path.stem for path in SCENARIO_DIR.glob("*.json"))
