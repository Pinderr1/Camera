"""Property activity insights: transparent, descriptive statistics about what
is normal on this property, computed locally from the activity log (zone visits
and trips, which hold categories, zones and times, never images, positions or
identities).

- Patterns: activity by hour and weekday, vehicle arrivals and departures,
  entrances and routes used, delivery-like visits, animals, passers-by,
  environmental motion, and typical stays per zone.
- Changes: the last 7 days compared with the weeks before.
- Unusual observations: simple, explained indicators (a time of day, a length
  of stay, or a route rarely or never seen before). **Unusual is not a threat**:
  these are information only. They never raise alerts or change rules.
- Suggestions: rules or settings the owner may review. Nothing is applied
  without the owner saving it in the rule builder.

Every statistic needs enough evidence (`min_days` of activity and
`min_samples` observations) before Sentinel calls anything "typical"."""

from __future__ import annotations

import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

DAY_MS = 86_400_000
ENTRANCE_TYPES = {"entrance", "perimeter", "driveway"}
QUIET_TYPES = {"street", "sidewalk"}
DISCLAIMER = "Descriptive statistics of activity on this property. Unusual is not the same as suspicious, and nothing here identifies anyone."


@dataclass
class ActivitySettings:
    enabled: bool = True
    retention_days: float = 90
    window_days: float = 30
    min_days: int = 7
    min_samples: int = 10
    rarity: float = 0.05  # an hour seen on at most this share of days counts as unusual
    recent_hours: float = 48

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ActivitySettings":
        data = data or {}
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


def _hour(ms: int) -> int:
    return datetime.fromtimestamp(ms / 1000).hour


def _date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def _clock(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%a %H:%M")


def _hours_text(start: int, length: int) -> str:
    return f"{start:02d}:00–{(start + length) % 24:02d}:00"


def _duration(seconds: float) -> str:
    return f"{round(seconds)} s" if seconds < 90 else f"{round(seconds / 60)} min" if seconds < 5400 else f"{seconds / 3600:.1f} h"


def _percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round((len(ordered) - 1) * percent / 100)))]


def _busiest_window(counts: list[int], share: float = 0.8, max_hours: int = 10) -> tuple[int, int] | None:
    """Shortest run of hours (wrapping midnight) holding `share` of the counts."""
    total = sum(counts)
    if not total:
        return None
    for length in range(1, max_hours + 1):
        best = max(range(24), key=lambda start: sum(counts[(start + i) % 24] for i in range(length)))
        if sum(counts[(best + i) % 24] for i in range(length)) >= share * total:
            return best, length
    return None


def compute_insights(
    visits: list[dict[str, Any]],
    trips: list[dict[str, Any]],
    *,
    config: dict[str, Any],
    settings: ActivitySettings,
    now_ms: int | None = None,
    scene_events: list[dict[str, Any]] | None = None,
    reviewed_incidents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now_ms = now_ms or int(time.time() * 1000)
    since_ms = now_ms - int(settings.window_days * DAY_MS)
    recent_ms = now_ms - int(settings.recent_hours * 3_600_000)
    zones = {zone["id"]: zone | {"camera_id": camera["id"]} for camera in config.get("cameras", []) for zone in camera.get("zones", [])}
    cameras = {camera["id"]: camera.get("name", camera["id"]) for camera in config.get("cameras", [])}
    zone_name = lambda zone_id: zones.get(zone_id, {}).get("name", zone_id)  # noqa: E731
    visits = [visit for visit in visits if since_ms <= visit["start_ms"] <= now_ms]
    trips = [trip for trip in trips if since_ms <= trip["first_seen_ms"] <= now_ms]
    days = sorted({_date(visit["start_ms"]) for visit in visits} | {_date(trip["first_seen_ms"]) for trip in trips})
    enough = len(days) >= settings.min_days
    notes = [DISCLAIMER]
    if not enough:
        notes.append(f"Sentinel needs activity on at least {settings.min_days} days before it describes what is typical ({len(days)} so far). "
                     "Counts are shown, but no patterns, unusual observations or suggestions yet.")

    # ---- patterns ------------------------------------------------------------
    hourly: dict[str, list[int]] = defaultdict(lambda: [0] * 24)
    weekday: dict[str, list[int]] = defaultdict(lambda: [0] * 7)
    totals: Counter = Counter()
    for trip in trips:
        totals[trip["label"]] += 1
        hourly[trip["label"]][_hour(trip["first_seen_ms"])] += 1
        weekday[trip["label"]][datetime.fromtimestamp(trip["first_seen_ms"] / 1000).weekday()] += 1

    vehicle_visits = [visit for visit in visits if visit["label"] == "vehicle" and visit["zone_type"] == "driveway"]
    arrivals = [0] * 24
    departures = [0] * 24
    for visit in vehicle_visits:
        if visit["arrived"]:
            arrivals[_hour(visit["start_ms"])] += 1
        departures[_hour(visit["end_ms"])] += 1
    arrival_window = _busiest_window(arrivals) if enough and sum(arrivals) >= settings.min_samples else None
    parked = [visit["dwell_s"] for visit in vehicle_visits if visit["dwell_s"] >= 300]
    vehicles = {
        "arrivals_by_hour": arrivals, "departures_by_hour": departures, "arrivals": sum(arrivals),
        "typical_arrival_hours": _hours_text(*arrival_window) if arrival_window else None,
        "parked_stays": len(parked), "parked_median": _duration(statistics.median(parked)) if parked else None,
    }

    person_visits = [visit for visit in visits if visit["label"] == "person"]
    entrance_counts = Counter(visit["zone_id"] for visit in person_visits if visit["zone_type"] in ENTRANCE_TYPES)
    total_entries = sum(entrance_counts.values()) or 1
    entrances = [{"zone_id": zone_id, "name": zone_name(zone_id), "type": zones.get(zone_id, {}).get("type", ""), "visits": count,
                  "share": round(count / total_entries, 2)} for zone_id, count in entrance_counts.most_common(8)]

    routes = Counter(tuple(trip["path"]) for trip in trips if trip["label"] == "person" and len(trip["path"]) >= 2)
    pathways = [{"path": [zone_name(zone_id) for zone_id in path], "count": count} for path, count in routes.most_common(8)]

    by_trip = defaultdict(list)
    for visit in person_visits:
        by_trip[visit["site_track_id"]].append(visit)
    deliveries = []
    for trip in trips:
        if trip["label"] != "person" or not 7 <= _hour(trip["first_seen_ms"]) < 21:
            continue
        stays = [visit for visit in by_trip.get(trip["site_track_id"], [])
                 if visit["zone_type"] == "entrance" and trip["first_seen_ms"] <= visit["start_ms"] <= trip["last_seen_ms"]]
        duration = (trip["last_seen_ms"] - trip["first_seen_ms"]) / 1000
        if stays and duration <= 300 and max(visit["dwell_s"] for visit in stays) <= 120:
            deliveries.append((trip, duration))
    delivery_hours = [0] * 24
    for trip, _ in deliveries:
        delivery_hours[_hour(trip["first_seen_ms"])] += 1
    delivery_window = _busiest_window(delivery_hours) if enough and len(deliveries) >= 5 else None
    delivery = {
        "count": len(deliveries), "by_hour": delivery_hours,
        "typical_hours": _hours_text(*delivery_window) if delivery_window else None,
        "median_visit": _duration(statistics.median(duration for _, duration in deliveries)) if deliveries else None,
        "note": "Short daytime visits to an entrance (under 5 minutes, under 2 minutes at the door). This is a pattern, not a confirmed delivery.",
    }

    animal_trips = [trip for trip in trips if trip["label"] == "animal"]
    animal_zones = Counter(visit["zone_id"] for visit in visits if visit["label"] == "animal")
    animals = {"count": len(animal_trips), "by_hour": hourly["animal"] if "animal" in hourly else [0] * 24,
               "kinds": dict(Counter(trip.get("kind") or "animal" for trip in animal_trips)),
               "zones": [{"name": zone_name(zone_id), "visits": count} for zone_id, count in animal_zones.most_common(5)]}

    pedestrian_trips = [trip for trip in trips if trip["label"] == "person" and trip["path"]
                        and all(zones.get(zone_id, {}).get("type") in QUIET_TYPES for zone_id in trip["path"])]
    pedestrian_hours = [0] * 24
    for trip in pedestrian_trips:
        pedestrian_hours[_hour(trip["first_seen_ms"])] += 1
    pedestrians = {"count": len(pedestrian_trips), "by_hour": pedestrian_hours,
                   "note": "People seen only on the street or sidewalk, never on the property."}

    environment_counts: Counter = Counter()
    environment_days: dict[tuple[str, str], set] = defaultdict(set)
    for event in scene_events or []:
        if event["timestamp_ms"] < since_ms:
            continue
        for condition in event.get("reason_codes", []):
            environment_counts[(event["camera_id"], condition)] += 1
            environment_days[(event["camera_id"], condition)].add(_date(event["timestamp_ms"]))
    environment = [{"camera_id": camera_id, "camera": cameras.get(camera_id, camera_id), "condition": condition.replace("_", " "),
                    "count": count, "days": len(environment_days[(camera_id, condition)])}
                   for (camera_id, condition), count in environment_counts.most_common(10)]

    occupancy: dict[tuple[str, str], list[float]] = defaultdict(list)
    for visit in visits:
        occupancy[(visit["zone_id"], visit["label"])].append(visit["dwell_s"])
    zone_stats = [{"zone_id": zone_id, "zone": zone_name(zone_id), "category": label, "visits": len(values),
                   "typical_stay": _duration(statistics.median(values)), "long_stay_p90": _duration(_percentile(values, 90)) if len(values) >= settings.min_samples else None}
                  for (zone_id, label), values in sorted(occupancy.items(), key=lambda item: -len(item[1]))[:15]]

    # ---- changes over time -----------------------------------------------------
    changes = []
    if enough:
        week_ago = now_ms - 7 * DAY_MS
        baseline_weeks = (week_ago - max(since_ms, min(trip["first_seen_ms"] for trip in trips) if trips else week_ago)) / (7 * DAY_MS)
        if baseline_weeks >= 1:
            for label in sorted(totals):
                recent = sum(1 for trip in trips if trip["label"] == label and trip["first_seen_ms"] >= week_ago)
                baseline = sum(1 for trip in trips if trip["label"] == label and trip["first_seen_ms"] < week_ago) / baseline_weeks
                if baseline >= 3 or recent >= 3:
                    change = round(100 * (recent - baseline) / baseline) if baseline else None
                    changes.append({"category": label, "last_7_days": recent, "baseline_per_week": round(baseline, 1), "change_pct": change,
                                    "text": f"{label.capitalize()} activity: {recent} in the last 7 days against about {baseline:.0f} a week before"
                                            + (f" ({'+' if change > 0 else ''}{change}%)." if change is not None else ".")})

    # ---- unusual observations (information only) -------------------------------------
    unusual = []
    if enough:
        baseline_visits = [visit for visit in visits if visit["start_ms"] < recent_ms]
        baseline_days = {_date(visit["start_ms"]) for visit in baseline_visits}
        if len(baseline_days) >= settings.min_days:
            seen_hours: dict[tuple[str, str], dict[int, set]] = defaultdict(lambda: defaultdict(set))
            stays: dict[tuple[str, str], list[float]] = defaultdict(list)
            for visit in baseline_visits:
                seen_hours[(visit["label"], visit["zone_id"])][_hour(visit["start_ms"])].add(_date(visit["start_ms"]))
                stays[(visit["label"], visit["zone_id"])].append(visit["dwell_s"])
            for visit in (visit for visit in visits if visit["start_ms"] >= recent_ms and visit["label"] in ("person", "vehicle")):
                key = (visit["label"], visit["zone_id"])
                history = stays.get(key, [])
                if len(history) < settings.min_samples:
                    continue
                hour = _hour(visit["start_ms"])
                dates = set().union(*(seen_hours[key][(hour + offset) % 24] for offset in (-1, 0, 1)))
                rate = len(dates) / len(baseline_days)
                if rate <= settings.rarity:
                    unusual.append({"time_ms": visit["start_ms"], "kind": "time", "category": visit["label"], "zone": zone_name(visit["zone_id"]),
                                    "camera_id": visit["camera_id"],
                                    "text": f"{visit['label'].capitalize()} in {zone_name(visit['zone_id'])} at {_clock(visit['start_ms'])}: around this hour, "
                                            f"this was seen on {len(dates)} of the previous {len(baseline_days)} days.",
                                    "evidence": {"days_with_activity_at_this_hour": len(dates), "days_observed": len(baseline_days)}})
                high = _percentile(history, 95)
                if visit["dwell_s"] > max(high * 1.5, high + 60):
                    unusual.append({"time_ms": visit["start_ms"], "kind": "stay", "category": visit["label"], "zone": zone_name(visit["zone_id"]),
                                    "camera_id": visit["camera_id"],
                                    "text": f"{visit['label'].capitalize()} stayed {_duration(visit['dwell_s'])} in {zone_name(visit['zone_id'])} "
                                            f"({_clock(visit['start_ms'])}); 95% of {len(history)} earlier stays there were under {_duration(high)}.",
                                    "evidence": {"stay_s": visit["dwell_s"], "p95_s": round(high, 1), "samples": len(history)}})
            known_routes = {tuple(trip["path"]) for trip in trips if trip["first_seen_ms"] < recent_ms and trip["label"] == "person"}
            if sum(1 for trip in trips if trip["first_seen_ms"] < recent_ms and trip["label"] == "person") >= settings.min_samples:
                for trip in trips:
                    if trip["first_seen_ms"] >= recent_ms and trip["label"] == "person" and len(trip["path"]) >= 3 and tuple(trip["path"]) not in known_routes:
                        unusual.append({"time_ms": trip["first_seen_ms"], "kind": "route", "category": "person", "zone": zone_name(trip["path"][-1]),
                                        "camera_id": "", "text": f"Route not seen in the previous {len(baseline_days)} days ({_clock(trip['first_seen_ms'])}): "
                                                                 + " → ".join(zone_name(zone_id) for zone_id in trip["path"]) + "."
                                                                 + (" The cameras' sightings were linked by timing, which may be wrong." if len(trip.get("cameras", [])) > 1 else ""),
                                        "evidence": {"route": trip["path"], "known_routes": len(known_routes)}})
    unusual.sort(key=lambda item: -item["time_ms"])

    # ---- suggestions (applied only if the owner saves them) ---------------------------
    suggestions = []
    if arrival_window:
        start, length = arrival_window
        driveway = sorted({visit["zone_id"] for visit in vehicle_visits})
        suggestions.append({
            "id": "vehicle_outside_usual_hours", "kind": "rule",
            "title": "Vehicles arriving outside their usual hours",
            "text": f"{round(100 * sum(arrivals[(start + i) % 24] for i in range(length)) / sum(arrivals))}% of {sum(arrivals)} vehicle arrivals were between "
                    f"{_hours_text(start, length)}. You could be told about arrivals outside these hours while away.",
            "rule": {"name": "Vehicle arriving outside usual hours", "type": "zone_entry", "zones": driveway, "classes": ["vehicle"],
                     "active_hours": {"start": f"{(start + length) % 24:02d}:00", "end": f"{start:02d}:00"}, "arm_modes": ["away", "vacation"],
                     "severity": "medium", "min_observations": 3, "cooldown_s": 900},
        })
    rules = config.get("rules", [])
    entry_rules_on_doors = [rule for rule in rules if rule["type"] == "zone_entry" and "entrance" in rule.get("zone_types", [])
                            and rule.get("enabled", True) and rule.get("notify", "auto") != "never"]
    if delivery_window and entry_rules_on_doors:
        suggestions.append({"id": "deliveries_vs_entry_rules", "kind": "review", "title": "Most door visits look like deliveries",
                            "text": f"{len(deliveries)} short daytime visits to an entrance, mostly {_hours_text(*delivery_window)}. "
                                    f"Rules that alert on any entry ({', '.join(rule.get('name') or rule['id'] for rule in entry_rules_on_doors)}) may be noisy; "
                                    "a 'lingering' rule or a time window could be quieter."})
    for row in environment:
        if row["condition"] in ("recurring motion", "near camera object") and row["days"] >= max(3, settings.min_days // 2):
            suggestions.append({"id": f"environment_{row['camera_id']}_{row['condition'].replace(' ', '_')}", "kind": "review",
                                "title": f"Repeated environmental motion on {row['camera']}",
                                "text": f"'{row['condition']}' was detected on {row['days']} days ({row['count']} times). Consider an ignore zone or privacy mask "
                                        "over the moving plants or the area close to the lens (Setup → Zones & masks)."})
    nuisance: Counter = Counter()
    for incident in reviewed_incidents or []:
        if incident.get("resolution") in ("false_alarm", "expected_activity"):
            for rule_id in incident.get("rule_ids", []):
                nuisance[rule_id] += 1
    for rule_id, count in nuisance.most_common(5):
        if count >= 3:
            rule = next((item for item in rules if item["id"] == rule_id), None)
            if rule:
                suggestions.append({"id": f"nuisance_{rule_id}", "kind": "review", "rule_id": rule_id,
                                    "title": f"'{rule.get('name') or rule_id}' was marked as not a concern {count} times",
                                    "text": "Consider requiring more observations, adding a cooldown or a time window, or using it only in Away mode."})
    if not enough:
        unusual, suggestions = [], []

    return {
        "window": {"days": settings.window_days, "since_ms": since_ms, "until_ms": now_ms, "days_observed": len(days),
                   "min_days": settings.min_days, "enough_data": enough},
        "totals": dict(totals), "hourly": {label: counts for label, counts in hourly.items()},
        "weekday": {label: counts for label, counts in weekday.items()},
        "vehicles": vehicles, "entrances": entrances, "pathways": pathways, "deliveries": delivery, "animals": animals,
        "pedestrians": pedestrians, "environment": environment, "zones": zone_stats, "changes": changes,
        "unusual": unusual[:25], "suggestions": suggestions, "notes": notes,
    }
