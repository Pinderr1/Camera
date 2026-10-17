"""Security rules for people: plain-language descriptions, templates for common
residential and small-business situations, and the editable fields the rule
builder may set. The engine (`engine.py`) evaluates rules; nothing here changes
how a rule behaves. Standard library only."""

from __future__ import annotations

import copy
from typing import Any

RULE_FIELDS = {
    "id", "name", "description", "type", "enabled", "severity", "classes", "zones", "zone_types", "arm_modes",
    "active_hours", "active_days", "min_confidence", "min_observations", "cooldown_s", "notify",
    "min_dwell_s", "max_roam", "require_arrival", "count", "window_s", "within_s", "min_cameras",
    "rules", "min_matches", "scope", "within", "min_closing", "identity_filter", "identity_wait_s",
}
TYPE_LABELS = {
    "zone_entry": "Enters an area",
    "dwell": "Stays in an area",
    "repeated_presence": "Repeated visits",
    "sequence": "Moves through areas in order",
    "multi_camera": "Seen across several cameras",
    "combination": "Several observations together",
    "zone_approach": "Moves toward an area",
}
MODE_LABELS = {"home": "Home", "away": "Away", "night": "Night", "vacation": "Vacation", "disarmed": "Disarmed"}
CLASS_LABELS = {"person": "person", "vehicle": "vehicle", "bicycle": "bicycle", "animal": "animal", "bag": "bag", "unknown_motion": "unidentified moving object"}
DAY_LABELS = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"}


def _duration(seconds: float) -> str:
    seconds = float(seconds)
    if seconds < 90:
        return f"{seconds:g} s"
    if seconds < 5400:
        return f"{seconds / 60:g} min"
    return f"{seconds / 3600:g} h"


def _join(items: list[str], word: str = "or") -> str:
    items = [item for item in items if item]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" {word} " + items[-1]


def describe_rule(rule: dict[str, Any], zone_names: dict[str, str] | None = None, rule_names: dict[str, str] | None = None) -> dict[str, Any]:
    """{"what": the behaviour, "when": the conditions, "then": what happens}."""
    zone_names = zone_names or {}
    who = _join([CLASS_LABELS.get(name, name) for name in rule.get("classes", ["person"])])
    who = f"a {who}" if who and who[0] not in "aeiou" else f"an {who}"
    if rule.get("zones"):
        where = _join([zone_names.get(zone, zone) for zone in rule["zones"]])
    elif rule.get("zone_types"):
        where = "any " + _join(rule["zone_types"]) + " area"
    else:
        where = "any area on the property"
    kind = rule["type"]
    if kind == "zone_entry":
        what = f"{who.capitalize()} enters {where}"
    elif kind == "dwell":
        what = f"{who.capitalize()} stays in {where} for at least {_duration(rule.get('min_dwell_s', 0))}"
        if rule.get("max_roam"):
            what += f", staying within about {rule['max_roam']:g} body heights"
        if rule.get("require_arrival"):
            what += " (only after arriving, not something already there)"
    elif kind == "repeated_presence":
        what = f"{rule.get('count', 2)} separate visits to {where} within {_duration(rule.get('window_s', 0))} (by the same or different people)"
    elif kind == "sequence":
        steps = " then ".join(zone_names.get(zone, zone) for zone in rule.get("zones", []))
        what = f"{who.capitalize()} goes to {steps} within {_duration(rule.get('within_s', 0))}"
    elif kind == "zone_approach":
        what = (f"{who.capitalize()} moves toward {where}, at least {rule.get('min_closing', 1.0):g} body heights closer within "
                f"{_duration(rule.get('window_s', 4))}, and comes within {rule.get('within', 2.0):g} body heights of it")
    elif kind == "multi_camera":
        what = f"{who.capitalize()} is followed across {rule.get('min_cameras', 2)} or more cameras within {_duration(rule.get('window_s', 0))}"
    else:
        names = [(rule_names or {}).get(component, component) for component in rule.get("rules", [])]
        scope = "for the same linked subject" if rule.get("scope", "subject") == "subject" else "anywhere on the property"
        what = f"At least {rule.get('min_matches', 1)} of these are observed within {_duration(rule.get('within_s', 0))}, {scope}: {_join(names, 'and')}"
    when = []
    modes = rule.get("arm_modes")
    when.append(f"in {_join([MODE_LABELS.get(mode, mode) for mode in modes])} mode" if modes else "in every armed mode")
    if rule.get("active_hours"):
        when.append(f"between {rule['active_hours']['start']} and {rule['active_hours']['end']}")
    if rule.get("active_days"):
        when.append("on " + _join([DAY_LABELS.get(day, day) for day in rule["active_days"]]))
    if rule.get("min_observations", 1) > 1:
        when.append(f"after at least {rule['min_observations']} detections of the subject")
    if rule.get("min_confidence"):
        when.append(f"with detection confidence of at least {round(rule['min_confidence'] * 100)}%")
    if rule.get("identity_filter") == "not_known":
        when.append("unless household-member recognition is enabled and has already recognized the person "
                    "(people it has not recognized, including those whose face was not seen, still count)")
    notify = rule.get("notify", "auto")
    then = {"auto": f"Records a {rule['severity']} event; a notification follows the mode's alert level",
            "always": f"Records a {rule['severity']} event and always notifies in an armed mode",
            "never": f"Records a {rule['severity']} event without notifying"}[notify]
    if rule.get("cooldown_s"):
        then += f"; repeats within {_duration(rule['cooldown_s'])} are recorded without alerting"
    return {"what": what, "when": ", ".join(when), "then": then}


# Templates use zone types so they work on any site; the builder lets people
# pick specific zones instead. `scenario` is a business or residential hint.
TEMPLATES: list[dict[str, Any]] = [
    {"key": "restricted_entry", "title": "Person in a restricted area", "scenario": "residential",
     "summary": "Alert as soon as someone enters a place nobody should be, such as a shed or pool.",
     "rule": {"type": "zone_entry", "zone_types": ["restricted"], "classes": ["person"], "severity": "high", "min_observations": 2}},
    {"key": "linger_entrance", "title": "Someone lingering at a door, window or gate", "scenario": "residential",
     "summary": "A person staying in one spot near an entrance or fence line, not just walking past.",
     "rule": {"type": "dwell", "zone_types": ["entrance", "perimeter"], "classes": ["person"], "min_dwell_s": 60, "max_roam": 2,
              "severity": "medium", "arm_modes": ["away", "night", "vacation"], "cooldown_s": 600}},
    {"key": "vehicle_arrives_away", "title": "Vehicle arrives while away", "scenario": "residential",
     "summary": "A vehicle pulling into the driveway when nobody is expected.",
     "rule": {"type": "zone_entry", "zone_types": ["driveway"], "classes": ["vehicle"], "severity": "medium",
              "arm_modes": ["away", "vacation"], "min_observations": 3, "cooldown_s": 900}},
    {"key": "vehicle_parked", "title": "Vehicle parked for a long time", "scenario": "residential",
     "summary": "A vehicle that arrives and stays, for example while you are on vacation.",
     "rule": {"type": "dwell", "zone_types": ["driveway"], "classes": ["vehicle"], "min_dwell_s": 900, "require_arrival": True,
              "severity": "low", "arm_modes": ["away", "vacation"]}},
    {"key": "repeated_door", "title": "Repeated visits to the front door", "scenario": "residential",
     "summary": "Several separate visits to an entrance in a short time.",
     "rule": {"type": "repeated_presence", "zone_types": ["entrance"], "classes": ["person"], "count": 3, "window_s": 900, "severity": "medium"}},
    {"key": "gate_then_shed_night", "title": "Side gate after midnight, then the shed", "scenario": "residential",
     "summary": "Someone comes through a gate at night and reaches a restricted area within three minutes. Pick the two areas in order.",
     "rule": {"type": "sequence", "zones": [], "classes": ["person"], "within_s": 180, "active_hours": {"start": "00:00", "end": "05:00"},
              "severity": "high", "notify": "always"}},
    {"key": "around_house", "title": "Walked around the house", "scenario": "residential",
     "summary": "One person followed across several cameras in a few minutes.",
     "rule": {"type": "multi_camera", "classes": ["person"], "min_cameras": 3, "window_s": 240, "severity": "medium", "arm_modes": ["away", "night", "vacation"]}},
    {"key": "night_yard", "title": "Activity in the yard at night", "scenario": "residential",
     "summary": "A person in the yard or along the fence line during the night.",
     "rule": {"type": "zone_entry", "zone_types": ["yard", "perimeter"], "classes": ["person"], "severity": "medium",
              "active_hours": {"start": "23:00", "end": "05:30"}, "arm_modes": ["night", "away", "vacation"], "cooldown_s": 300}},
    {"key": "shop_after_hours", "title": "Person at the shop entrance after hours", "scenario": "business",
     "summary": "Someone at the storefront or back door outside opening hours.",
     "rule": {"type": "zone_entry", "zone_types": ["entrance"], "classes": ["person"], "severity": "high",
              "active_hours": {"start": "21:00", "end": "07:00"}, "min_observations": 3, "cooldown_s": 600}},
    {"key": "loading_bay_weekend", "title": "Vehicle in the loading area at the weekend", "scenario": "business",
     "summary": "A vehicle in the yard or loading bay on Saturday or Sunday.",
     "rule": {"type": "zone_entry", "zone_types": ["driveway", "yard"], "classes": ["vehicle"], "severity": "medium",
              "active_days": ["sat", "sun"], "cooldown_s": 1800}},
    {"key": "storefront_loiter", "title": "Lingering by the storefront at night", "scenario": "business",
     "summary": "Someone staying near the front of the business at night.",
     "rule": {"type": "dwell", "zone_types": ["entrance", "perimeter"], "classes": ["person"], "min_dwell_s": 120, "max_roam": 3,
              "severity": "medium", "active_hours": {"start": "22:00", "end": "06:00"}, "cooldown_s": 900}},
    {"key": "approach_restricted", "title": "Someone heading for a restricted area", "scenario": "residential",
     "summary": "A person moving clearly toward a place nobody should be, such as a pool gate or shed, before they reach it. Pick the areas.",
     "rule": {"type": "zone_approach", "zones": [], "classes": ["person"], "within": 2.0, "min_closing": 1.0, "window_s": 4,
              "severity": "medium", "arm_modes": ["away", "night", "vacation"], "min_observations": 3, "cooldown_s": 300}},
    {"key": "restricted_entry_not_household", "title": "Restricted area, unless a recognized household member",
     "scenario": "residential",
     "summary": "Like 'Person in a restricted area', but recorded without alerting when household-member recognition has recognized "
                "the person. Anyone not recognized, including people whose face was not seen, still alerts.",
     "rule": {"type": "zone_entry", "zone_types": ["restricted"], "classes": ["person"], "severity": "high", "min_observations": 2,
              "identity_filter": "not_known", "identity_wait_s": 3}},
    {"key": "two_concerns", "title": "Two separate concerns within ten minutes", "scenario": "residential",
     "summary": "Only alert when at least two of the chosen rules match close together. Set those rules to record only.",
     "rule": {"type": "combination", "rules": [], "min_matches": 2, "within_s": 600, "scope": "site", "severity": "high"}},
]


def templates() -> list[dict[str, Any]]:
    return copy.deepcopy(TEMPLATES)


def clean_rule(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep only rule-builder fields and normalise empty values. Validation is done
    on the whole config (`config.validate_config`)."""
    rule = {key: copy.deepcopy(value) for key, value in payload.items() if key in RULE_FIELDS}
    for key in ("zones", "zone_types", "arm_modes", "active_days", "rules"):
        if key in rule and not rule[key] and not (key == "zones" and rule.get("type") == "sequence") and not (key == "rules" and rule.get("type") == "combination"):
            rule.pop(key)
    if rule.get("active_hours") in (None, {}, ""):
        rule.pop("active_hours", None)
    if isinstance(rule.get("name"), str) and not rule["name"].strip():
        rule.pop("name")
    for key in ("notify", "description"):
        if rule.get(key) in ("", "auto" if key == "notify" else None):
            rule.pop(key, None)
    if rule.get("type") != "combination":
        for key in ("rules", "min_matches", "scope"):
            rule.pop(key, None)
    if rule.get("type") == "combination":
        for key in ("zones", "zone_types", "classes", "min_observations", "min_confidence"):
            rule.pop(key, None)
    return rule
