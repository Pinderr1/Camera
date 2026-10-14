"""Sentinel's built-in guide: deterministic, local, and always available.

It answers when no AI provider is configured, when the provider fails, and for
quick actions. It recognises the common requests with plain keyword rules,
calls the same tools as the AI (so every answer comes from live system data),
and follows the same rules: one next step at a time, confirmed versus possible
causes kept apart, changes only through confirmation cards, passwords only in
the secure sign-in form, and never a claim that a property is protected."""

from __future__ import annotations

import re
from typing import Any

from .tools import Toolbox, Turn

_IP = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?![\d.])")
STARTERS = ["Set up my existing cameras", "Install new cameras", "Set up my Sentinel drone", "Set up a complete system", "Check my security system"]
BRAND_WORDS = ("hikvision", "dahua", "lorex", "reolink", "uniview", "amcrest")
ZONE_WORDS = {
    "driveway": ("driveway",), "garage": ("driveway", "garage"), "car": ("driveway",), "door": ("entrance",), "porch": ("entrance",),
    "entrance": ("entrance",), "front": ("entrance",), "yard": ("yard",), "garden": ("yard",), "backyard": ("yard",), "gate": ("perimeter", "entrance"),
    "fence": ("perimeter",), "shed": ("restricted",), "pool": ("restricted",), "sidewalk": ("sidewalk",), "street": ("street",),
}


def _has(text: str, *words: str) -> bool:
    return any(re.search(rf"\b{re.escape(word)}", text) for word in words)


def _bullets(lines: list[str]) -> str:
    return "\n".join(f"- {line}" for line in lines if line)


class Guide:
    def __init__(self, toolbox: Toolbox) -> None:
        self.tools = toolbox

    def _call(self, name: str, turn: Turn, **args: Any) -> dict[str, Any]:
        result, is_error = self.tools.call(name, args, turn)
        if is_error:
            raise LookupError(result["error"])
        return result

    def choices(self, turn: Turn, options: list[str]) -> None:
        self.tools.call("offer_choices", {"options": options}, turn)

    # ---- routing --------------------------------------------------------------------------

    def respond(self, text: str, turn: Turn) -> str:
        lower = text.lower()
        state = turn.conversation["guide"]
        address = _IP.search(text)
        try:
            if address:
                return self.diagnose(turn, address.group(1), brand=next((b for b in BRAND_WORDS if b in lower), None))
            if state.get("last_host") and _has(lower, "retest", "test again", "try again", "check again", "done", "i did", "fixed", "changed", "plugged"):
                return self.diagnose(turn, state["last_host"], retest=True)
            if _has(lower, "drone"):
                return self.drone(turn, lower)
            if _has(lower, "not connect", "aren't connect", "arent connect", "not showing", "aren't showing", "can't find", "cannot find", "not working",
                    "offline", "no picture", "troubleshoot", "won't connect", "doesn't connect", "not found", "connection problem"):
                return self.troubleshoot(turn)
            if _has(lower, "new camera", "where should", "place them", "put them", "mount", "install new", "poe", "wi-fi", "wifi", "network cable")                     or (_has(lower, "bought") and _has(lower, "camera")):
                return self.new_cameras(turn, lower)
            if _has(lower, "existing", "already have", "connect them", "connect my", "nvr", "dvr", "recorder", *BRAND_WORDS):
                return self.existing_cameras(turn, lower)
            if _has(lower, "complete system", "complete sentinel", "full system", "everything"):
                return self.start_path(turn, "complete_package")
            if _has(lower, "don't notify", "dont notify", "stop notifying", "not every time", "too many alert", "too many notification", "stop alerting"):
                return self.reduce_alerts(turn, lower)
            if _has(lower, "why did", "why was i", "last night", "what happened", "while i was away", "incident", "alerted", "the alert", "that alert"):
                return self.history(turn, lower)
            if _has(lower, "watch", "alert me", "notify me", "let me know", "tell me if", "tell me when", "stays", "linger", "loiter", "rule",
                    "configure security", "security around"):
                return self.security_rule(turn, text, lower)
            if _has(lower, "storage", "disk", "space left", "recording space"):
                return self.storage(turn)
            if _has(lower, "unusual", "strange", "odd activity"):
                return self.unusual(turn)
            if _has(lower, "improve", "better", "recommend", "suggest"):
                return self.improve(turn)
            if _has(lower, "protected", "safe", "secure", "working", "status", "check my", "health", "camera"):
                return self.status(turn)
            if _has(lower, "set up", "setup", "set me up", "get started", "start", "just bought", "install", "help"):
                return self.setup(turn)
        except LookupError as error:
            return f"I couldn't check that: {error}."
        self.choices(turn, STARTERS)
        return ("I can help you set up cameras, a drone or a complete system, troubleshoot connections, configure what Sentinel watches, "
                "and explain alerts. What would you like to do?")

    # ---- installation -----------------------------------------------------------------------

    def setup(self, turn: Turn) -> str:
        overview = self._call("get_overview", turn)
        install = overview["installation"]
        if not install["path"]:
            self.choices(turn, STARTERS[:4])
            return "Welcome to Sentinel. I'll help you get everything connected. How are you starting?"
        if install["completed"]:
            return self.status(turn)
        step = install["current_step"]
        labels = {"type": "choosing the installation type", "property": "describing the property", "plan": "planning or discovering equipment",
                  "hardware": "connecting hardware", "verify": "verifying devices", "zones": "setting zones and privacy masks",
                  "rules": "choosing what Sentinel watches", "alerts": "setting up notifications", "health": "the system health check", "finish": "finishing"}
        self.tools.call("show_screen", {"target": f"install:{step}", "label": "Continue where you left off"}, turn)
        lines = [f"You're setting up **{install['path_label']}**, at the step for {labels.get(step, step)}.", overview["readiness"]["statement"]]
        if overview["readiness"]["blocking"]:
            lines.append("Still needed before the installation can be completed: " + "; ".join(overview["readiness"]["blocking"]) + ".")
        hints = {"hardware": "Next: add or find your cameras. Tell me if they don't show up.", "verify": "Next: check that each camera shows video.",
                 "zones": "Next: draw zones on each camera so Sentinel knows the driveway from the street.",
                 "rules": "Next: tell me what you'd like to be told about, for example \"watch my driveway at night\".",
                 "alerts": "Next: turn on phone notifications and send a test."}
        if step in hints:
            lines.append(hints[step])
        return "\n\n".join(lines)

    def start_path(self, turn: Turn, path: str) -> str:
        overview = self._call("get_overview", turn)
        if overview["installation"]["path"] != path:
            self.tools.call("propose_installation_path", {"path": path}, turn)
        if path == "complete_package":
            self.choices(turn, ["Set up my existing cameras", "Install new cameras", "Set up my Sentinel drone"])
            return ("A complete system can grow in any order: cameras, a drone, a dock and sensors, all managed together. "
                    "Confirm the setup type below. Which would you like to start with?")
        return "Confirm the setup type below and we'll continue."

    def existing_cameras(self, turn: Turn, lower: str) -> str:
        overview = self._call("get_overview", turn)
        if not overview["installation"]["path"]:
            self.tools.call("propose_installation_path", {"path": "existing_cameras"}, turn)
        devices = self._call("list_devices", turn, category="camera")["devices"]
        if devices:
            working = [d for d in devices if d["level"] == "ai_verified"]
            text = f"Sentinel already has {len(devices)} camera(s); AI monitoring is verified on {len(working)}."
            if len(working) < len(devices):
                return text + "\n\n" + self.troubleshoot(turn)
            self.tools.call("show_camera_previews", {}, turn)
            return text + " Here they are. To add more, I can search your network or you can tell me the recorder's address."
        self.tools.call("propose_network_search", {"include_port_check": False}, turn)
        turn.conversation["guide"]["expect"] = "address"
        brand = next((b for b in BRAND_WORDS if b in lower), None)
        lines = ["Let's find them. I can search your network for cameras and recorders: it only listens for answers and checks camera ports, "
                 "and it changes nothing. Please confirm below that you own or manage this network."]
        if brand == "hikvision":
            lines.append("Hikvision recorders answer Sentinel's SADP search even when their address belongs to another network, which is a common reason they don't show up.")
        lines.append("If you know the recorder's address (on its own screen under Menu → Network), you can also just type it here.")
        return "\n\n".join(lines)

    def troubleshoot(self, turn: Turn) -> str:
        health = self._call("get_camera_health", turn)
        config = self.tools.service.site.config
        hosts = {c["id"]: c.get("source", {}).get("host") for c in config.get("cameras", [])}
        if health["cameras"] and all(c.get("state") in (None, "not_running") for c in health["cameras"]):
            return ("Camera processing isn't running in this session, so I can't check the cameras' connections yet. "
                    "If a camera or recorder is missing, tell me its address and I'll check that.")
        failing = [c for c in health["cameras"] if not c.get("monitoring") and c.get("state") not in (None, "starting", "not_running")]
        for camera in failing:
            if hosts.get(camera["camera_id"]):
                intro = f"**{camera['name']}** is not being monitored ({camera.get('label') or camera.get('state')}). I checked its connection:"
                return intro + "\n\n" + self.diagnose(turn, hosts[camera["camera_id"]])
        recorder = next((d for d in self._call("list_devices", turn, category="recorder")["devices"] if d.get("host") and d["level"] == "registered"), None)
        if recorder:
            return f"Your recorder **{recorder['name']}** has no working channels yet. I checked its address:\n\n" + self.diagnose(turn, recorder["host"])
        if turn.conversation["guide"].get("last_host"):
            return self.diagnose(turn, turn.conversation["guide"]["last_host"], retest=True)
        if failing:
            names = ", ".join(c["name"] for c in failing)
            self.tools.call("show_screen", {"target": "system", "label": "See camera health"}, turn)
            return f"These cameras are not being monitored: {names}. They aren't network cameras I can probe, so check that they are plugged in, then look at System health."
        if health["cameras"] and not failing:
            self.tools.call("show_camera_previews", {}, turn)
            return f"All {len(health['cameras'])} camera(s) are running. If one is missing, tell me the camera's or recorder's address and I'll check it."
        turn.conversation["guide"]["expect"] = "address"
        self.tools.call("propose_network_search", {"include_port_check": False}, turn)
        return ("I don't see any cameras yet. Tell me the recorder's or camera's address (shown on its own screen, usually under Menu → Network) "
                "and I'll check the connection, or let me search your network (confirm below).")

    def diagnose(self, turn: Turn, host: str, *, brand: str | None = None, retest: bool = False) -> str:
        state = turn.conversation["guide"]
        before = state.get("last_reachable") if retest and state.get("last_host") == host else None
        args = {"address": host} | ({"brand": brand} if brand else {})
        report = self._call("diagnose_device", turn, **args)
        state["last_reachable"] = report["reachable"]
        state.pop("expect", None)
        findings = report["findings"]
        confirmed = [f for f in findings if f["status"] == "confirmed"]
        suspected = [f for f in findings if f["status"] == "suspected"]
        intro = ""
        if retest:
            intro = ("Retested. " + ("It answers now. " if report["reachable"] and before is False else
                                     "Still no answer. " if not report["reachable"] else "")).strip() + "\n\n"
        if report["reachable"] and not confirmed:
            open_ports = [c["check"] for c in report["checks"] if c["result"] == "open"]
            self.tools.call("offer_recorder_sign_in", {"address": host, "brand": brand or (report.get("identity") or {}).get("brand") or "other"}, turn)
            return intro + (f"The device at {host} answers ({', '.join(open_ports[:3]) or 'it replied to ping'}). "
                            "Next: sign in below so Sentinel can find its camera channels. The password goes straight to Sentinel's credential store, not into this chat.")
        if not findings:
            return intro + f"I couldn't reach {host}, and the checks found nothing specific."
        lines = [intro.strip()] if intro.strip() else []
        fact = confirmed[0] if confirmed else None
        # One next action, from the first finding that has a fix; that may be a cause that is only suspected.
        cause = next((f for f in confirmed + suspected if f["steps"]), None)
        if fact:
            lines.append(f"**Confirmed:** {fact['title']}")
            if fact["evidence"]:
                lines.append("What I observed:\n" + _bullets(fact["evidence"][:3]))
        if cause is not None and cause is not fact:
            label = "Confirmed cause" if cause["status"] == "confirmed" else "Most likely cause (not confirmed)"
            lines.append(f"**{label}:** {cause['title']}\n\n{cause['explanation']}")
        elif cause is None and fact is None:
            lines.append(f"**{findings[0]['title']}**\n\n{findings[0]['explanation']}")
        if cause is not None:
            # A first step that only opens a menu is completed by the change to make there.
            step = cause["steps"][0] + (f" Then: {cause['steps'][1]}" if len(cause["steps"]) > 1 and len(cause["steps"][0]) < 160 else "")
            lines.append(f"**Next step:** {step}")
        others = [f["title"] for f in suspected if f is not cause][:3]
        if others:
            lines.append("Other possible causes, if that doesn't help: " + "; ".join(others) + ".")
        lines.append("Sentinel never changes your router, computer or recorder settings. When you've done that step, tap Retest.")
        self.choices(turn, ["Retest"])
        return "\n\n".join(lines)

    def new_cameras(self, turn: Turn, lower: str) -> str:
        overview = self._call("get_overview", turn)
        if not overview["installation"]["path"]:
            self.tools.call("propose_installation_path", {"path": "new_cameras"}, turn)
        if _has(lower, "poe", "network cable", "ethernet", "wired") or _has(lower, "wi-fi", "wifi", "wireless"):
            topic = "wifi" if _has(lower, "wi-fi", "wifi", "wireless") else "poe"
            section = self._call("get_installation_guidance", turn, topic=topic)
            first = section["problems"][0]
            lines = [("Wi-Fi cameras: set each one up with its maker's app first, on a 2.4 GHz network, and check the signal where it will hang. "
                      if topic == "wifi" else
                      "PoE cameras: one network cable carries power and video. Plug each camera into a PoE port on a switch or recorder, "
                      "within about 90 m of cable. ") + "Then I can search your network for them.",
                     f"If one doesn't come up ({first['symptom'].lower()}):\n" + _bullets(first["checks"][:3]),
                     "Running cables through walls or mounting outdoors is physical work: if you'd rather not do it, a professional installer can, "
                     "and Sentinel will take over from there."]
            self.tools.call("propose_network_search", {"include_port_check": False}, turn)
            return "\n\n".join(lines)
        plan = self._call("get_property_plan", turn)
        if plan.get("plan") != "empty" and plan.get("gaps"):
            self.tools.call("show_screen", {"target": "install:plan", "label": "Open the property planner"}, turn)
            return ("Your property plan shows spots no camera covers yet:\n" + _bullets(plan["gaps"][:4]) +
                    "\n\nThose are the first places to consider. Place the new cameras on the planner to check what they would see "
                    "(coverage is an estimate, not a guarantee).")
        self.tools.call("show_screen", {"target": "install:plan", "label": "Open the property planner"}, turn)
        self.choices(turn, ["They use a network cable (PoE)", "They use Wi-Fi", "Not sure"])
        return ("Good placements usually cover each outside door and the driveway first, mounted about 2.5–3 m high, not facing the sun. "
                "Mark your doors and driveway on the property planner and place the cameras there to see what they'd cover.\n\n"
                "How do the new cameras connect?")

    def drone(self, turn: Turn, lower: str) -> str:
        status = self._call("get_drone_status", turn)
        drones = [d for d in status["drones"] if d["category"] == "drone"]
        if not drones:
            location = self.tools.service.site.config.get("site", {}).get("map", {}).get("origin_geo")
            text = ("No drone is registered. Sentinel's own drones are not available yet, but you can register a **simulated** drone now "
                    "to set up inspection locations and the approval workflow.")
            if location:
                self.tools.call("propose_register_device", {"category": "drone", "name": "Simulated drone", "profile": "simulated_drone"}, turn)
                return text + " Confirm below to add one."
            self.tools.call("show_screen", {"target": "install:property", "label": "Set the property location"}, turn)
            return text + " First set the property's location (it places the drone geofence; it stays on this hub)."
        lines = []
        for drone in drones:
            kind = "simulated" if drone.get("simulated") else "physical, telemetry only"
            battery = f", battery {round(drone['battery_pct'])}%" if drone.get("battery_pct") is not None else ""
            dock = ", docked" + (" and charging" if drone.get("charging") else "") if drone.get("docked") else ""
            lines.append(f"**{drone['name']}** ({kind}): {drone.get('detail', drone['state'])}{battery}{dock}.")
        primary = status.get("primary") or {}
        if primary.get("preflight_blockers"):
            lines.append("Before any inspection can launch: " + ", ".join(b.replace("_", " ") for b in primary["preflight_blockers"]) + ".")
        lines.append("Every inspection flight needs an operator's approval on the Drone tab. Physical drones are telemetry-only in this build: "
                     "flight commands are disabled, and drone video is not shown yet.")
        if status["inspection_points"]:
            lines.append("Inspection locations: " + ", ".join(p["name"] for p in status["inspection_points"]) + ".")
        return "\n\n".join(lines)

    # ---- security configuration ---------------------------------------------------------------

    def _zones_for(self, lower: str, zones: list[dict[str, Any]]) -> list[dict[str, Any]]:
        named = [z for z in zones if z["name"].lower() in lower or any(word in lower for word in z["name"].lower().split() if len(word) > 3)]
        if named:
            return named
        types = {t for word, kinds in ZONE_WORDS.items() if _has(lower, word) for t in kinds}
        return [z for z in zones if z["type"] in types]

    def security_rule(self, turn: Turn, text: str, lower: str) -> str:
        catalog = self._call("list_rules_and_zones", turn)
        zones = [z for z in catalog["zones"] if z["type"] not in ("ignore", "street", "sidewalk")]
        if not zones:
            self.tools.call("show_screen", {"target": "install:zones", "label": "Draw zones"}, turn)
            return "Sentinel needs zones before it can watch an area: draw them on a camera picture first (for example the driveway)."
        targets = self._zones_for(lower, zones)
        if not targets:
            self.choices(turn, [f"Watch the {z['name']}" for z in zones[:5]])
            return "Which area should I watch?"
        vehicle = _has(lower, "car", "vehicle", "truck", "van")
        dwell = _has(lower, "stay", "linger", "loiter", "hang", "wait", "near")
        night = _has(lower, "night", "dark")
        away = _has(lower, "away", "vacation", "holiday")
        names = " and ".join(z["name"] for z in targets[:3])
        rule: dict[str, Any] = {"type": "dwell" if dwell else "zone_entry", "zones": [z["id"] for z in targets[:3]],
                                "classes": ["vehicle" if vehicle else "person"], "severity": "medium",
                                "name": f"{'Vehicle' if vehicle else 'Person'} {'staying in' if dwell else 'in'} {names}{' at night' if night else ''}"[:80]}
        if dwell:
            rule |= {"min_dwell_s": 60, "max_roam": 2}
        else:
            rule["min_observations"] = 2
        if night:
            rule["active_hours"] = {"start": "22:00", "end": "06:00"}
        if away:
            rule["arm_modes"] = ["away", "vacation"]
        check = self._call("check_rule_draft", turn, rule=rule)
        if not check["valid"]:
            return "I couldn't build a valid rule for that: " + "; ".join(check["errors"])
        test = self._call("test_rule_draft", turn, rule=rule, walk_zone_ids=[targets[0]["id"]], at_time="23:30" if night else "12:00",
                          arm_mode="away" if away or not night else "night")
        plain = check["plain"] or {}
        lines = [f"Here's the rule I'd suggest: **{rule['name']}**.", f"It detects {plain.get('what', '')} {plain.get('when', '')}".strip() + ".",
                 f"Then: {plain.get('then', '')}"]
        fired = test["fired"] > 0
        lines.append(f"I tested it with a simulated {'vehicle' if vehicle else 'person'} in {targets[0]['name']}: "
                     + ("it fired" + (" and would notify you." if test["notifications"] else ", but at the test's arm mode it would only be recorded, not pushed.")
                        if fired else "it did not fire. " + " ".join(test["notes"][:1])))
        self.tools.call("propose_save_rule", {"rule": rule, "turn_on": fired}, turn)
        lines.append("Nothing changes until you confirm below.")
        return "\n\n".join(line for line in lines if line.strip())

    def reduce_alerts(self, turn: Turn, lower: str) -> str:
        catalog = self._call("list_rules_and_zones", turn)
        quiet = {z["id"] for z in catalog["zones"] if z["type"] in ("sidewalk", "street")}
        config_rules = {r["id"]: r for r in self.tools.service.site.config.get("rules", [])}
        explicit = [r for r in catalog["rules"] if r["enabled"] and (set(config_rules.get(r["id"], {}).get("zones", [])) & quiet
                                                                     or set(config_rules.get(r["id"], {}).get("zone_types", [])) & {"sidewalk", "street"})]
        if explicit:
            rule = explicit[0]
            self.tools.call("propose_rule_switch", {"rule_id": rule["id"], "turn_on": False}, turn)
            return (f"The rule **{rule['name']}** watches the sidewalk or street. Turning it off stops those alerts; "
                    "activity there is still logged. Confirm below if you'd like that.")
        self.tools.call("show_screen", {"target": "timeline", "label": "Review recent alerts"}, turn)
        return ("Sentinel already treats sidewalk and street zones as public: people walking past there never count as being on your property, "
                "and that activity is only logged, not pushed. If you still got an alert for someone on the sidewalk, the zone may be drawn too far "
                "onto your lawn or the sidewalk isn't marked: open the alert and tell me which one it was.")

    # ---- everyday questions ---------------------------------------------------------------------

    def status(self, turn: Turn) -> str:
        overview = self._call("get_overview", turn)
        health = self._call("get_camera_health", turn)
        readiness = overview["readiness"]
        lines = ["Sentinel can't promise protection. Here is what it has actually verified.", f"**{readiness['statement']}**"]
        lines += readiness["extras"]
        problems = [f"{c['name']}: {c.get('label') or c.get('state')}" + (f" ({c['reason']})" if c.get("reason") else "")
                    for c in health["cameras"] if not c.get("monitoring") and c.get("state") not in (None, "not_running")]
        if problems:
            lines.append("Not being monitored:\n" + _bullets(problems[:6]))
        issues = [issue for issue in readiness["open_issues"] if "simulated" not in issue.lower()][:3]
        if issues:
            lines.append("Open setup issues:\n" + _bullets(issues))
        self.tools.call("show_screen", {"target": "system", "label": "Open System health"}, turn)
        return "\n\n".join(lines)

    def history(self, turn: Turn, lower: str) -> str:
        hours = 18 if "last night" in lower else 72 if _has(lower, "away", "vacation", "weekend") else 24
        incidents = self._call("get_incidents", turn, hours=hours)["incidents"]
        if not incidents:
            return f"No incidents in the last {hours} hours. Ordinary activity is logged on the Timeline without alerting."
        if _has(lower, "why"):
            chosen = next((i for i in incidents if i["notified"]), incidents[0])
            explanation = self._call("explain_incident", turn, incident_id=chosen["incident_id"])
            observed = [s["summary"] for s in explanation["sequence"] if s.get("basis") == "observed"][:4]
            inferred = [f"{s['summary']} ({s['note']})" if s.get("note") else s["summary"] for s in explanation["sequence"] if s.get("basis") == "inferred"][:3]
            rules = [r.get("name") or r.get("rule_id", "") for r in explanation["rules"]][:3]
            lines = [f"**{chosen['title']}** ({chosen['severity']}, {chosen['opened']}).", explanation["headline"] if isinstance(explanation["headline"], str) else ""]
            if observed:
                lines.append("Observed by the cameras:\n" + _bullets(observed))
            if inferred:
                lines.append("Inferred by Sentinel (not directly seen):\n" + _bullets(inferred))
            if rules:
                lines.append("It alerted because these rules matched: " + ", ".join(str(rule) for rule in rules) + ".")
            lines.append(explanation["statement"])
            return "\n\n".join(line for line in lines if line)
        listed = [f"{i['opened']}: {i['title']} ({i['severity']}{', notified' if i['notified'] else ''})" for i in incidents[:6]]
        return f"In the last {hours} hours there were {len(incidents)} incident(s):\n" + _bullets(listed) + "\n\nAsk \"why\" about any of them for the details."

    def storage(self, turn: Turn) -> str:
        health = self._call("get_system_health", turn)
        storage = health["storage"]
        if storage.get("free_mb") is None:
            return "Storage hasn't been measured yet in this session; check again in a minute."
        lines = [f"Free disk space: {storage['free_mb'] / 1024:.1f} GB ({storage.get('free_pct')}%)."]
        if storage.get("media_mb") is not None:
            lines.append(f"Recordings use {storage['media_mb'] / 1024:.1f} GB.")
        lines.append("Recording is paused because the disk is nearly full." if storage.get("recording_paused") else
                     "Recording is running; the oldest unprotected recordings are removed automatically when space runs low.")
        return " ".join(lines)

    def unusual(self, turn: Turn) -> str:
        insights = self._call("get_activity_insights", turn)
        if not insights["enough_data"]:
            return f"Not enough activity history yet ({insights['days_observed']} day(s)); patterns start after about a week."
        if not insights["unusual"]:
            return "Nothing stood out from the usual activity in this period."
        return ("These differ from the usual pattern. Unusual doesn't mean a threat; it's information:\n" + _bullets(insights["unusual"][:5]))

    def improve(self, turn: Turn) -> str:
        overview = self._call("get_overview", turn)
        plan = self._call("get_property_plan", turn)
        ideas = [issue for issue in overview["readiness"]["open_issues"] if "simulated" not in issue.lower()][:4]
        ideas += (plan.get("gaps") or [])[:3]
        if not ideas:
            return "Nothing obvious to improve: every check passes. Review alerts now and then and mark false alarms, so you can see which rules are noisy."
        return "The most useful improvements right now:\n" + _bullets(ideas)
