"""The assistant's tools: a fixed, small set of operations on existing Sentinel
services. Nothing here runs shell commands, reads files, returns secrets,
changes network settings, or commands a drone.

Three kinds:
- `read`: returns live data from the existing services (health, devices,
  installation readiness, diagnostics of one private address, rules, incidents).
- `ui`: adds a button or card to the reply (open a screen, a secure sign-in
  form, previews, quick replies). No side effects.
- `propose`: validates a change and shows it to the person as a proposal card.
  Nothing changes until they press Confirm; `apply_action` then runs the
  existing backend call, audited, and reports what the backend returned.

Rules can only be proposed for turning on after the same version passed
through the existing rule test in this conversation."""

from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .privacy import Aliases, untrusted

PROPOSAL_TTL_MS = 30 * 60 * 1000
SCREENS = {
    "live": "Live cameras", "map": "Property map", "timeline": "Incidents", "insights": "Activity insights", "drone": "Drone",
    "system": "System health", "settings": "Settings", "report": "Installation plan (print)",
    "install": "Installation", "setup": "Setup", "zones": "Zones and privacy masks", "troubleshooter": "Network troubleshooter",
    "camera_wizard": "Add a camera",
}
INSTALL_STEPS = ("type", "property", "plan", "hardware", "verify", "zones", "rules", "alerts", "health", "finish")
SETUP_SUBS = ("cameras", "zones", "propmap", "links", "categories", "rules")
BRANDS = ("hikvision", "dahua", "lorex", "reolink", "uniview", "other")


class ToolError(ValueError):
    """A problem the model can correct (bad argument, unknown id)."""


@dataclass
class Turn:
    conversation: dict[str, Any]
    actor: str
    aliases: Aliases
    cards: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)

    def real(self, value: str) -> str:
        return self.aliases.resolve(str(value or ""))

    def open(self, target: str, label: str = "", **params: Any) -> None:
        action = {"type": "open", "target": target, "label": label or SCREENS.get(target.split(":")[0], target)}
        action |= {key: value for key, value in params.items() if value not in (None, "")}
        if action not in self.actions:
            self.actions.append(action)


@dataclass
class Tool:
    name: str
    kind: str  # read | ui | propose
    description: str
    properties: dict[str, Any]
    required: list[str]
    run: Callable[[dict[str, Any], Turn], dict[str, Any]]

    def definition(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "input_schema": {"type": "object", "properties": self.properties, "required": self.required, "additionalProperties": False}}


def rule_fingerprint(rule: dict[str, Any]) -> str:
    from ..rules import clean_rule

    body = {key: value for key, value in clean_rule(rule).items() if key not in ("id", "enabled", "name", "description")}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def _validate(tool: Tool, args: Any) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    unknown = set(args) - set(tool.properties)
    if unknown:
        raise ToolError(f"unknown argument(s): {', '.join(sorted(unknown))}")
    missing = [name for name in tool.required if name not in args]
    if missing:
        raise ToolError(f"missing argument(s): {', '.join(missing)}")
    checks = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}
    for name, value in args.items():
        expected = tool.properties[name].get("type")
        if expected and not isinstance(value, checks[expected]) or (expected in ("integer", "number") and isinstance(value, bool)):
            raise ToolError(f"{name} must be a {expected}")
        allowed = tool.properties[name].get("enum")
        if allowed and value not in allowed:
            raise ToolError(f"{name} must be one of {allowed}")
        if isinstance(value, str) and len(value) > 2000:
            raise ToolError(f"{name} is too long")
    return args


class Toolbox:
    def __init__(self, service: Any) -> None:
        self.service = service
        self.tools: dict[str, Tool] = {}
        self._define()

    # ---- running --------------------------------------------------------------------

    def definitions(self) -> list[dict[str, Any]]:
        return [tool.definition() for tool in self.tools.values()]

    def call(self, name: str, args: Any, turn: Turn) -> tuple[dict[str, Any], bool]:
        """(result, is_error). Unknown tools and bad arguments are errors the model sees; nothing raises."""
        tool = self.tools.get(name)
        if tool is None:
            return {"error": f"there is no tool named {untrusted(name, 40)!r}"}, True
        try:
            return tool.run(_validate(tool, args), turn), False
        except ToolError as error:
            return {"error": str(error)}, True
        except (KeyError, ValueError, TypeError) as error:
            return {"error": f"{type(error).__name__}: {str(error)[:300]}"}, True

    # ---- helpers ----------------------------------------------------------------------

    def _config(self) -> dict[str, Any]:
        return self.service.site.config

    def _zone_catalog(self) -> list[dict[str, Any]]:
        return [{"id": zone["id"], "name": untrusted(zone.get("name", zone["id"])), "type": zone["type"], "camera_id": camera["id"],
                 "camera": untrusted(camera.get("name", camera["id"]))}
                for camera in self._config().get("cameras", []) for zone in camera.get("zones", [])]

    def _device_summary(self, device: dict[str, Any]) -> dict[str, Any]:
        keep = ("id", "category", "support", "simulated", "level", "state", "detail", "warnings", "zones", "privacy_masks", "recorder",
                "host", "channels_added", "battery_pct", "docked", "charging", "primary", "safety_state", "physical", "flight_mode")
        summary = {key: device[key] for key in keep if key in device and device[key] not in (None, "", [])}
        summary["name"] = untrusted(device.get("name"))
        summary["profile"] = device.get("profile", "")
        return summary

    def _propose(self, turn: Turn, kind: str, params: dict[str, Any], title: str, summary: str, effects: list[str]) -> dict[str, Any]:
        action_id = uuid.uuid4().hex[:12]
        proposal = {"id": action_id, "kind": kind, "params": params, "title": title, "summary": summary, "effects": effects,
                    "status": "pending", "created_ms": int(time.time() * 1000)}
        turn.conversation["pending"][action_id] = proposal
        turn.cards.append({"type": "proposal", "action_id": action_id, "title": title, "summary": summary, "effects": effects, "status": "pending"})
        return {"proposal_id": action_id, "status": "awaiting_customer_confirmation",
                "note": "Nothing has changed. The customer sees a Confirm / Cancel card; tell them what it does and wait."}

    # ---- definitions ------------------------------------------------------------------

    def _define(self) -> None:
        def add(name, kind, description, properties=None, required=None):
            def register(function):
                self.tools[name] = Tool(name, kind, description, properties or {}, required or [], function)
                return function
            return register

        string = lambda description, **extra: {"type": "string", "description": description} | extra  # noqa: E731
        boolean = lambda description: {"type": "boolean", "description": description}  # noqa: E731
        number = lambda description: {"type": "number", "description": description}  # noqa: E731

        # ---- read: installation and devices ------------------------------------------

        @add("get_overview", "read", "Installation progress (chosen path, current step, step status) and the live readiness summary: "
             "what is verified, open issues, counts of cameras/drones/sensors. Call this first when unsure where the customer is.")
        def get_overview(args, turn):
            view = self.service.installation_view()
            state, readiness = view["state"], view["readiness"]
            return {
                "installation": {"path": state["path"] or None, "path_label": view["paths"].get(state["path"], "not chosen"),
                                 "current_step": state["current_step"], "completed": bool(state["completed_ms"]),
                                 "steps": {step: info["status"] for step, info in readiness["steps"].items()}},
                "readiness": {"statement": readiness["statement"]["text"], "extras": readiness["statement"]["extras"], "levels": readiness["levels"],
                              "blocking": readiness["blocking"], "open_issues": readiness["open_issues"][:10]},
                "camera_processing_running": view["processing"],
                "coverage_state": self.service.runtime.health.coverage(self.service.runtime.camera_status)["state"],
            }

        @add("list_devices", "read", "Every registered device with its connection level (registered, connected, video, ai_verified), "
             "state, support level (verified, simulated, expected, limited, planned) and warnings.",
             {"category": string("Optional filter", enum=["camera", "recorder", "drone", "dock", "contact_sensor", "motion_sensor", "environmental_sensor", "hub"])})
        def list_devices(args, turn):
            devices = [self._device_summary(d) for d in self.service.devices_view() if not args.get("category") or d["category"] == args["category"]]
            return {"devices": devices, "level_meaning": "registered = in the config only; connected = data received; video = frames arriving; "
                                                         "ai_verified = changing frames reach a working detector"}

        @add("get_camera_health", "read", "Live health of cameras: monitoring state and reason, warnings, frame rate, reconnects, "
             "connection error kind, liveness signals.", {"camera_id": string("Optional: one camera id")})
        def get_camera_health(args, turn):
            report = self.service.health_report()
            cameras = [c for c in report["cameras"] if not args.get("camera_id") or c["camera_id"] == args["camera_id"]]
            if args.get("camera_id") and not cameras:
                raise ToolError("unknown camera id")
            keep = ("camera_id", "connected", "fps", "target_fps", "error_kind", "reconnects", "stream", "resolution", "detector_state", "restarts_total")
            return {"coverage": {key: report["coverage"][key] for key in ("state", "monitoring", "total", "unverified")},
                    "cameras": [{key: c.get(key) for key in keep} | {"name": untrusted(c["name"]), "state": c["health"].get("state"),
                                "label": c["health"].get("label"), "reason": c["health"].get("reason"), "warnings": c["health"].get("warnings", []),
                                "monitoring": c["health"].get("monitoring")} for c in cameras]}

        @add("get_hardware_compatibility", "read", "Sentinel's hardware compatibility profiles (protocols, power, capabilities, support level, notes). "
             "Use these instead of guessing what a device supports.",
             {"category": string("Optional category filter"), "search": string("Optional text to match, e.g. a brand")})
        def get_hardware_compatibility(args, turn):
            from ..devices import PROFILES, SUPPORT_LEVELS

            term = (args.get("search") or "").lower()
            profiles = [p for p in PROFILES if (not args.get("category") or p["category"] == args["category"])
                        and (not term or term in json.dumps(p).lower())]
            return {"support_levels": SUPPORT_LEVELS, "profiles": profiles[:12]}

        @add("get_installation_guidance", "read", "Practical guidance for installing hardware: common PoE, Wi-Fi and login/codec problems, "
             "or the installation checklists for a path.", {"topic": string("Which guidance", enum=["poe", "wifi", "streams", "checklists"])}, ["topic"])
        def get_installation_guidance(args, turn):
            if args["topic"] == "checklists":
                from ..planner import checklists

                return checklists(self.service.installation.state["path"] or "existing_cameras")
            from ..netdiag.guidance import new_camera_guide

            return next(section for section in new_camera_guide() if section["id"] == args["topic"])

        @add("get_property_plan", "read", "The property plan's approximate coverage: priority spots in or out of view, gaps, privacy warnings, "
             "PoE and cable estimates, and the assumptions behind them. Coverage is approximate, never verified.")
        def get_property_plan(args, turn):
            from ..planner import estimate

            state = self.service.installation.state
            plan = state["plan"]
            if not plan.get("items"):
                return {"plan": "empty", "recommendation": state.get("recommendation")}
            coverage = self.service.coverage_analysis()
            return {"summary": coverage["summary"], "features": coverage["features"], "gaps": coverage["gaps"],
                    "privacy": [p["advice"] for p in coverage["privacy"]], "assumptions": coverage["assumptions"],
                    "estimate": estimate(plan, self._config().get("cameras", [])), "recommendation": state.get("recommendation"),
                    "items": [{"name": untrusted(i["name"]), "kind": i["kind"]} for i in plan["items"]][:60]}

        @add("recommend_cameras", "read", "Suggest how many cameras a property needs from a few facts. Returns lines with reasons; "
             "a starting point, not a guarantee.",
             {key: {"type": "integer", "minimum": 0, "maximum": 20} for key in ("exterior_doors", "garage_doors", "gates", "yard_sides", "loading_areas", "indoor_areas")}
             | {"driveway": boolean("Has a driveway or parking"), "want_sensors": boolean("Wants door sensors"), "want_drone": boolean("Wants a drone"),
                "property_type": string("Property type", enum=["house", "corner_lot", "townhouse", "storefront", "warehouse", "office", "other"]),
                "wired": string("Wiring", enum=["poe", "wifi", "mixed", "unsure"])})
        def recommend_cameras(args, turn):
            from ..planner import recommend

            result = recommend(args)
            self.service.installation.set_recommendation(args, result)
            turn.open("install:plan", "Open the property planner")
            return result

        # ---- read: network (read-only; private addresses only) -----------------------

        @add("get_network_overview", "read", "This hub's network connections (adapter type, address range, router) and network-level problems "
             "(no connection, several networks, VPN, Windows Public profile). Read-only.")
        def get_network_overview(args, turn):
            from .. import netdiag

            snapshot = self.service.network.snapshot()
            view = snapshot.public_view()
            adapters = [{key: adapter.get(key) for key in ("kind", "ipv4", "prefix", "gateway", "dhcp", "profile", "up") if key in adapter}
                        for adapter in view.get("adapters", [])]
            return {"adapters": adapters, "findings": [{key: f[key] for key in ("code", "status", "title", "explanation", "steps")}
                                                       for f in (finding.as_dict() for finding in netdiag.analyze_network(snapshot))]}

        @add("diagnose_device", "read", "Check one camera or recorder address on the local network: ping, camera ports, routing, the routers involved "
             "and local discovery. Returns findings labelled confirmed (observed), suspected (possible cause, unproven), ruled_out, info or ok, "
             "each with evidence and fix steps. Only private addresses. Read-only: it never changes any setting.",
             {"address": string("The device's IP address (or its alias such as [ip-1])"), "brand": string("Brand if known", enum=list(BRANDS)),
              "device_mask": string("Subnet mask shown on the recorder's own screen, if the customer read it"),
              "device_gateway": string("Gateway shown on the recorder's own screen, if known")}, ["address"])
        def diagnose_device(args, turn):
            from .. import netdiag

            host = turn.real(args["address"])
            try:
                report = netdiag.troubleshoot(self.service.network, host, brand=args.get("brand"), device_mask=args.get("device_mask"),
                                              device_gateway=turn.real(args["device_gateway"]) if args.get("device_gateway") else None)
            except netdiag.TargetRefused as error:
                raise ToolError(str(error)) from None
            self.service.runtime.store.audit(turn.actor, "network_diagnosis", {"host": report["host"], "reachable": report["reachable"], "via": "assistant",
                                                                              "findings": [f["code"] for f in report["findings"] if f["status"] in ("confirmed", "suspected")]})
            turn.conversation["guide"]["last_host"] = report["host"]
            turn.open("troubleshooter", "Open the full diagnosis", host=report["host"], brand=args.get("brand"))
            return {"address": report["host"], "reachable": report["reachable"], "summary": report["summary"],
                    "checks": [{"check": c["label"], "result": c["result"]} for c in report["checks"]],
                    "identity": report.get("identity"),
                    "found_by_discovery": [{k: d.get(k) for k in ("host", "method", "brand", "on_local_network", "network")} for d in report["discovered"]][:8],
                    "findings": [{key: f[key] for key in ("code", "status", "title", "explanation", "evidence", "steps")} for f in report["findings"]]}

        @add("test_camera_connection", "read", "Run Sentinel's connection test on a configured camera with its stored credentials: "
             "reports reachability, login, stream, codec, resolution and frame rate. Never returns addresses or passwords.",
             {"camera_id": string("Camera id")}, ["camera_id"])
        def test_camera_connection(args, turn):
            from .. import onboarding

            try:
                source = self.service.site.config_source(args["camera_id"])
            except KeyError:
                raise ToolError("unknown camera id") from None
            result = onboarding.test_source(source, self.service.runtime.secrets.get)
            self.service.runtime.store.audit(turn.actor, "camera_connection_test", {"camera_id": args["camera_id"], "result": result.get("kind"), "via": "assistant"})
            return {key: result.get(key) for key in ("ok", "kind", "message", "ai_stream")} | {
                "streams": {name: {k: v for k, v in (stream or {}).items() if k in ("ok", "kind", "resolution", "measured_fps", "codec", "hw_decode_active")}
                            for name, stream in (result.get("streams") or {}).items()}}

        # ---- read: security configuration and history --------------------------------

        @add("list_rules_and_zones", "read", "Existing security rules with plain-language descriptions and on/off state, rule templates, "
             "and the zones that rules can watch (with their type and camera).")
        def list_rules_and_zones(args, turn):
            view = self.service.rules_view()
            return {"rules": [{"id": rule["id"], "name": untrusted(rule["display_name"]), "type": rule["type"], "enabled": rule.get("enabled", True),
                               "severity": rule["severity"], "plain": rule["plain"]} for rule in view["rules"]],
                    "templates": [{"key": t["key"], "title": t["title"], "summary": t["summary"], "rule": t["rule"]} for t in view["templates"]],
                    "zones": self._zone_catalog(), "rule_types": view["catalog"]["types"], "arm_modes": view["catalog"]["modes"],
                    "push_levels_by_arm_mode": view["catalog"]["notify_thresholds"]}

        rule_schema = {"type": "object", "description": "Rule fields as in the rule builder: type (zone_entry, dwell, repeated_presence, sequence, "
                       "multi_camera), severity (info/low/medium/high), classes, zones or zone_types, arm_modes, active_hours {start,end HH:MM}, "
                       "active_days, min_dwell_s, max_roam, count, window_s, within_s, min_cameras, cooldown_s, notify (auto/always/never), name."}

        @add("check_rule_draft", "read", "Validate a draft rule against this site without saving it, and get Sentinel's plain-language "
             "description of exactly what it would detect.", {"rule": rule_schema}, ["rule"])
        def check_rule_draft(args, turn):
            return self.service.check_rule(args["rule"])

        @add("test_rule_draft", "read", "Run Sentinel's rule test (a simulated walk through zones, on a throwaway database) to see whether a "
             "draft rule fires, opens an incident and would notify. Required before a rule may be proposed for turning on.",
             {"rule": rule_schema, "walk_zone_ids": {"type": "array", "items": {"type": "string"}, "description": "Zones the simulated subject visits in order"},
              "stay_s": number("Seconds spent in each zone (default 15, or the dwell time plus 20)"),
              "arm_mode": string("Arm mode during the test", enum=["home", "away", "night", "vacation"]),
              "at_time": string("Local time of day HH:MM for the test (default now; use night hours for night rules)"),
              "subject": string("What walks", enum=["person", "vehicle", "bicycle", "animal"])}, ["rule", "walk_zone_ids"])
        def test_rule_draft(args, turn):
            rule = args["rule"]
            zones = {zone["id"] for zone in self._zone_catalog()}
            walk = [zone for zone in args["walk_zone_ids"] if zone in zones][:8]
            if not walk:
                raise ToolError("walk_zone_ids must list existing zone ids")
            stay = args.get("stay_s") or (float(rule.get("min_dwell_s", 0)) + 20 if rule.get("type") == "dwell" else 15)
            start = datetime.now().replace(second=0, microsecond=0)
            if args.get("at_time"):
                hour, minute = (int(part) for part in args["at_time"].split(":"))
                start = start.replace(hour=hour, minute=minute)
            arm_mode = args.get("arm_mode") or (rule.get("arm_modes") or ["away"])[0]
            result = self.service.test_rule({"rule": rule, "source": "walk", "arm_mode": arm_mode, "start": start.isoformat(),
                                             "steps": [{"zone_id": zone, "stay_s": min(float(stay), 3600), "gap_s": 5} for zone in walk],
                                             "label": args.get("subject") or (rule.get("classes") or ["person"])[0]})
            fingerprint = rule_fingerprint(rule)
            tested = turn.conversation.setdefault("tested_rules", [])
            if fingerprint not in tested:
                tested.append(fingerprint)
            self.service.runtime.store.audit(turn.actor, "rule_tested", {"rule_id": rule.get("id", "draft"), "source": "walk", "fired": len(result["fired"]), "via": "assistant"})
            return {"fired": len(result["fired"]), "fired_events": [e["summary"] for e in result["fired"]][:5], "incidents": len(result["incidents"]),
                    "notifications": [n["title"] for n in result["notifications"]], "verdict": result["verdict"], "notes": result["notes"],
                    "test_conditions": {"arm_mode": arm_mode, "start": start.strftime("%H:%M"), "walk": walk, "stay_s": stay}}

        @add("get_incidents", "read", "Incidents (grouped security events) in a recent period, newest first, with severity, title, cameras, "
             "whether a notification was sent, and review outcome.",
             {"hours": number("How far back (default 24, max 720)"), "min_severity": string("Lowest severity", enum=["info", "low", "medium", "high"])})
        def get_incidents(args, turn):
            hours = min(float(args.get("hours") or 24), 720)
            since = int(time.time() * 1000 - hours * 3_600_000)
            incidents = self.service.runtime.store.search_incidents(since_ms=since, min_severity=args.get("min_severity"), limit=30)
            activity = self.service.runtime.store.list_events(since_ms=since, limit=1000)
            turn.open("timeline", "Open incidents")
            return {"period_hours": hours, "incidents": [{"incident_id": i["incident_id"], "title": i["title"], "severity": i["severity"],
                                                           "opened": datetime.fromtimestamp(i["opened_ms"] / 1000).strftime("%a %H:%M"),
                                                           "cameras": i["cameras"], "notified": bool(i.get("notified_severity")),
                                                           "status": i["status"], "review": i.get("resolution") or "not reviewed"} for i in incidents],
                    "events_logged": len(activity), "note": "Titles describe observed behaviour, never intent or identity."}

        @add("explain_incident", "read", "Sentinel's own explanation of an incident: what was observed versus inferred, which rules matched, "
             "confidence and uncertainty, why it got its severity.", {"incident_id": string("Incident id")}, ["incident_id"])
        def explain_incident(args, turn):
            try:
                explanation = self.service.explain(args["incident_id"])
            except KeyError:
                raise ToolError("unknown incident id") from None
            turn.open("timeline", "Open this incident and its recordings", incident_id=args["incident_id"])
            return {"headline": explanation["headline"],
                    "sequence": [{k: s.get(k) for k in ("summary", "basis", "note")} for s in explanation["sequence"]][:12],
                    "rules": explanation["rules"], "confidence": explanation["confidence"], "severity": explanation["severity"],
                    "evidence_available": bool(explanation.get("evidence")), "statement": explanation["statement"]}

        @add("get_activity_insights", "read", "Descriptive activity statistics and unusual observations (information only: unusual does not mean "
             "a threat) and approval-only suggestions.", {"days": number("Period in days (default 30)")})
        def get_activity_insights(args, turn):
            insights = self.service.insights(args.get("days"))
            turn.open("insights", "Open activity insights")
            return {"enough_data": insights["window"]["enough_data"], "days_observed": insights["window"]["days_observed"], "totals": insights["totals"],
                    "unusual": [u["text"] for u in insights["unusual"]][:8], "changes": insights["changes"][:5],
                    "suggestions": [{"title": s["title"], "text": s["text"]} for s in insights["suggestions"]][:5], "notes": insights["notes"]}

        @add("get_system_health", "read", "Hub health: recording storage (free space, recordings size, paused), event database, backups, "
             "CPU/memory/GPU, coverage components.")
        def get_system_health(args, turn):
            report = self.service.health_report()
            storage = report["storage"]
            turn.open("system", "Open System health")
            return {"storage": {key: storage.get(key) for key in ("free_mb", "free_pct", "threshold_mb", "media_mb", "recording_paused", "low", "purged_total")},
                    "database_ok": report["database"]["ok"], "backups": {"count": report["backups"]["count"], "last_error": report["backups"]["last_error"]},
                    "resources": {key: (report["resources"] or {}).get(key) for key in ("system_cpu_pct", "process_rss_mb")},
                    "coverage": report["coverage"]["state"],
                    "components": {name: {"state": c["state"], "detail": c["detail"]} for name, c in report["coverage"]["components"].items()}}

        @add("get_drone_status", "read", "Registered drones: model (Sentinel Lite/Pro/Thermal or other), simulated or physical, link, battery, "
             "docked/charging, safety state, readiness checks that fail or warn, inspection locations and requests. "
             "Physical flight commands are disabled in this build.")
        def get_drone_status(args, turn):
            drones = [self._device_summary(d) for d in self.service.devices_view() if d["category"] in ("drone", "dock")]
            config = self._config().get("drone", {})
            primary = self.service.drone
            status = primary.status() if primary else None
            turn.open("drone", "Open the Drone tab")
            primary_view = None
            if status:
                primary_view = {"model": status["model"]["label"], "adapter": status["adapter"], "physical": status["physical"],
                                "preflight_blockers": status["preflight_blockers"], "safety_state": status["safety_state"],
                                "checks_not_passing": [{k: c[k] for k in ("id", "label", "state", "detail")} for c in status["readiness"]["checks"]
                                                       if c["state"] in ("fail", "warn")],
                                "pending_requests": sum(1 for r in status["requests"] if r["status"] == "pending_approval"),
                                "video": (status.get("video") or {}).get("detail") or "no drone video configured"}
            return {"drones": drones, "drone_features_on": bool(config.get("enabled")), "primary": primary_view,
                    "inspection_points": [{"id": p["id"], "name": untrusted(p["name"])} for p in config.get("inspection_points", [])],
                    "limits": ["Every flight needs an operator to approve it on the Drone tab; the assistant cannot approve flights.",
                               "Physical drones are telemetry-only: flight commands are refused, and the pilot flies the exported route.",
                               "Drone video is analysed on the hub only during an approved inspection; thermal video is not analysed.",
                               "Sentinel Lite, Pro and Thermal are reference designs: none has been built or flown yet."]}

        @add("get_drone_models", "read", "Sentinel's drone models (Lite, Pro, Thermal) side by side: capabilities, camera, estimated hover time "
             "and wind limit, what is shared, and honest limits. Pass a few needs to get a recommendation with reasons. No prices: parts "
             "estimates are in the documentation, never quoted as a price.",
             {"night": boolean("Needs inspections after dark"), "windy": boolean("Often windier than 6 m/s"),
              "zoom": boolean("Wants to inspect from farther or higher (zoom)"), "obstacles": boolean("Trees or wires near likely routes"),
              "property_size_m": number("Longest property dimension in metres"),
              "budget": string("Budget priority", enum=["lowest", "balanced", "capability"])})
        def get_drone_models(args, turn):
            from ..drone import catalog

            table = catalog.comparison()
            models = [{"id": m["id"], "name": m["name"], "cost": m["cost_label"], "summary": m["summary"], "best_for": m["best_for"],
                       "camera": m["hardware"]["camera"], "companion": m["hardware"]["companion"],
                       "hover_min_estimate": m["flight"]["endurance_min_est"], "wind_limit_mps_estimate": m["flight"]["max_wind_mps"],
                       "night_inspections": m["capabilities"]["night_inspection"], "options": m["options"], "limitations": m["limitations"]}
                      for m in table["models"]]
            result = {"models": models, "capabilities": {row["label"]: row["values"] for row in table["rows"]}, "shared": table["shared"],
                      "status": table["support_note"]}
            result["recommendation"] = catalog.recommend(args)
            return result

        @add("diagnose_drone", "read", "Troubleshoot a registered drone: every failing or warning readiness check with its likely cause and "
             "model-specific steps for the person at the drone or ground station (radio link, command lockout, stream rates, parameters, "
             "camera stream, conditions). Read-only: Sentinel never changes flight-controller settings.",
             {"drone_id": string("Drone device id (default: the primary drone)")})
        def diagnose_drone(args, turn):
            from ..drone.troubleshoot import diagnose

            manager = self.service.drone_manager(args.get("drone_id"))
            if manager is None:
                raise ToolError("no operable drone with that id (planned hardware has no connection)")
            turn.open("drone", "Open the Drone tab")
            return diagnose(manager.status(), manager.model)

        @add("get_notification_settings", "read", "Push notification channels (ntfy, MQTT), whether a topic is set (never the topic itself), "
             "push levels per arm mode, and the last test notification result.")
        def get_notification_settings(args, turn):
            return self.service.notification_view()

        # ---- ui: buttons and cards (no side effects) ----------------------------------

        @add("show_screen", "ui", "Add a button that takes the customer to an existing Sentinel screen. Targets: live, map, timeline, insights, drone, "
             "system, settings, report, install:<step> (type, property, plan, hardware, verify, zones, rules, alerts, health, finish), "
             "setup:<section> (cameras, zones, propmap, links, categories, rules), zones:<camera_id>, troubleshooter, camera_wizard.",
             {"target": string("Screen"), "label": string("Button text"), "incident_id": string("For timeline"),
              "address": string("For troubleshooter: device address or alias"), "brand": string("For troubleshooter", enum=list(BRANDS))}, ["target"])
        def show_screen(args, turn):
            target = args["target"]
            base, _, detail = target.partition(":")
            valid = (base in SCREENS and not detail) or (base == "install" and detail in INSTALL_STEPS) or (base == "setup" and detail in SETUP_SUBS) \
                or (base == "zones" and detail in {c["id"] for c in self._config().get("cameras", [])})
            if not valid:
                raise ToolError("unknown screen target")
            turn.open(target, untrusted(args.get("label"), 60), incident_id=args.get("incident_id"),
                      host=turn.real(args["address"]) if args.get("address") else None, brand=args.get("brand"))
            return {"shown": target}

        @add("offer_recorder_sign_in", "ui", "Show the secure sign-in form for a recorder or camera. The customer types the password there; it goes "
             "straight to Sentinel's credential store and the channel search, never into this conversation. Use it to find a recorder's channels "
             "and add cameras.", {"address": string("Device address or alias"), "brand": string("Brand", enum=list(BRANDS)),
                                  "port": {"type": "integer", "description": "RTSP port (default 554)"}}, ["address"])
        def offer_recorder_sign_in(args, turn):
            from .. import netdiag
            from ..netdiag import guidance

            host = turn.real(args["address"])
            try:
                netdiag.check_target(host)
            except netdiag.TargetRefused as error:
                raise ToolError(str(error)) from None
            brand = args.get("brand") or "other"
            turn.cards.append({"type": "recorder_login", "host": host, "brand": brand, "template": guidance.brand(brand)["template"],
                               "port": int(args.get("port") or 554)})
            return {"shown": "secure sign-in form", "note": "Never ask the customer to type a password in the chat."}

        @add("show_camera_previews", "ui", "Show live preview pictures of cameras in the conversation (displayed on this hub only; never sent to the AI).",
             {"camera_ids": {"type": "array", "items": {"type": "string"}, "description": "Cameras (default: all)"}})
        def show_camera_previews(args, turn):
            cameras = {c["id"]: untrusted(c.get("name", c["id"])) for c in self._config().get("cameras", [])}
            chosen = [cid for cid in (args.get("camera_ids") or list(cameras)) if cid in cameras][:12]
            if not chosen:
                raise ToolError("no matching cameras")
            turn.cards.append({"type": "preview", "cameras": [{"id": cid, "name": cameras[cid]} for cid in chosen]})
            return {"shown": chosen}

        @add("offer_choices", "ui", "Offer up to 5 short quick replies the customer can tap, for asking one question at a time.",
             {"options": {"type": "array", "items": {"type": "string"}, "description": "Short reply texts"}}, ["options"])
        def offer_choices(args, turn):
            options = [untrusted(option, 60) for option in args["options"] if str(option).strip()][:5]
            turn.cards.append({"type": "choices", "options": options})
            return {"shown": options}

        # ---- propose: changes need the customer's confirmation ----------------------------

        @add("propose_installation_path", "propose", "Propose setting the installation type. Applied only after the customer confirms.",
             {"path": string("Installation path", enum=["existing_cameras", "new_cameras", "drone_first", "complete_package"])}, ["path"])
        def propose_installation_path(args, turn):
            from ..installation import PATH_LABELS

            return self._propose(turn, "set_path", {"path": args["path"]}, f"Set up: {PATH_LABELS[args['path']]}",
                                 "Records how you are starting. Any hardware can still be added later.", ["Installation progress is updated"])

        @add("propose_step_status", "propose", "Propose marking an installation step done or skipped (not 'finish': completion happens on the Finish screen).",
             {"step": string("Step", enum=list(INSTALL_STEPS[:-1])), "status": string("New status", enum=["done", "skipped"])}, ["step", "status"])
        def propose_step_status(args, turn):
            from ..installation import STEPS

            label = dict(STEPS)[args["step"]]
            return self._propose(turn, "step_status", dict(args), f"Mark '{label}' as {args['status']}", "Updates the installation checklist only.",
                                 ["Installation progress is updated", "Readiness checks are unaffected: they always use live data"])

        @add("propose_network_search", "propose", "Ask the customer's permission to search their network for cameras and recorders (Hikvision SADP, "
             "ONVIF; optionally a port check of the local /24). Runs only after they confirm they own or manage the network.",
             {"include_port_check": boolean("Also check camera ports on nearby addresses (slower)")})
        def propose_network_search(args, turn):
            methods = ["Hikvision SADP", "ONVIF"] + (["camera ports on this computer's own /24 network"] if args.get("include_port_check") else [])
            return self._propose(turn, "network_search", {"port_check": bool(args.get("include_port_check"))}, "Search my network for cameras",
                                 "Only confirm if you own or manage this network. Sentinel only listens for answers and checks camera ports; "
                                 "it changes nothing.", [f"Searches with: {', '.join(methods)}", "No MAC addresses, Wi-Fi names or public addresses are collected"])

        @add("propose_save_rule", "propose", "Propose saving a security rule (new, or replacing an existing one). To turn it on, the same rule version "
             "must have passed test_rule_draft in this conversation first.",
             {"rule": rule_schema, "replaces_rule_id": string("Existing rule id to replace (omit for a new rule)"), "turn_on": boolean("Turn the rule on")},
             ["rule", "turn_on"])
        def propose_save_rule(args, turn):
            rule = dict(args["rule"])
            check = self.service.check_rule(rule)
            if not check["valid"]:
                raise ToolError("the rule is not valid: " + "; ".join(check["errors"]))
            if args["turn_on"] and rule_fingerprint(rule) not in turn.conversation.get("tested_rules", []):
                raise ToolError("test this exact rule with test_rule_draft before proposing to turn it on")
            replaces = args.get("replaces_rule_id")
            if replaces and replaces not in {r["id"] for r in self._config().get("rules", [])}:
                raise ToolError("unknown rule id to replace")
            plain = check["plain"] or {}
            name = untrusted(rule.get("name") or "New rule", 80)
            return self._propose(turn, "save_rule", {"rule": rule, "rule_id": replaces, "enable": bool(args["turn_on"])},
                                 f"{'Replace' if replaces else 'Add'} rule: {name}" + (" (turned on)" if args["turn_on"] else " (off)"),
                                 " ".join(str(plain.get(key, "")) for key in ("what", "when", "then")).strip(),
                                 ["Saved through the rule builder's validation and applied live", "Audited"]
                                 + (["Tested in this conversation"] if args["turn_on"] else ["Saved switched off"]))

        @add("propose_rule_switch", "propose", "Propose turning an existing rule on or off. Turning on needs a test of that rule in this conversation.",
             {"rule_id": string("Rule id"), "turn_on": boolean("On or off")}, ["rule_id", "turn_on"])
        def propose_rule_switch(args, turn):
            rule = next((r for r in self._config().get("rules", []) if r["id"] == args["rule_id"]), None)
            if rule is None:
                raise ToolError("unknown rule id")
            if args["turn_on"] and rule_fingerprint(rule) not in turn.conversation.get("tested_rules", []):
                raise ToolError("test this rule with test_rule_draft before proposing to turn it on")
            name = untrusted(rule.get("name") or rule["id"])
            return self._propose(turn, "rule_switch", dict(args), f"Turn {'on' if args['turn_on'] else 'off'}: {name}",
                                 "Applied live through the rule editor.", ["Audited"])

        @add("propose_notification_settings", "propose", "Propose changing push channels: ntfy on/off, server, snapshot attachment, MQTT on/off. "
             "The ntfy topic is a secret: it is entered on the Alerts screen, never in chat.",
             {"ntfy_enabled": boolean("ntfy on"), "server": string("ntfy server URL"), "attach_snapshot": boolean("Send snapshots to the push service"),
              "mqtt_enabled": boolean("MQTT on")})
        def propose_notification_settings(args, turn):
            if not args:
                raise ToolError("nothing to change")
            effects = []
            if "ntfy_enabled" in args:
                effects.append(f"Phone notifications (ntfy) {'on' if args['ntfy_enabled'] else 'off'}")
            if args.get("server"):
                effects.append(f"ntfy server: {untrusted(args['server'], 120)}")
            if "attach_snapshot" in args:
                effects.append("Snapshots are sent to the push service, outside your property" if args["attach_snapshot"] else "No snapshots are sent")
            if "mqtt_enabled" in args:
                effects.append(f"MQTT alerts {'on' if args['mqtt_enabled'] else 'off'}")
            turn.open("install:alerts", "Open notification settings (enter the topic there)")
            return self._propose(turn, "notifications", dict(args), "Change notification settings", "Applied without a restart.", effects)

        @add("propose_register_device", "propose", "Propose registering a recorder, drone, dock or sensor (cameras are added through the sign-in form "
             "or the camera wizard). Registration records the device; it never claims the device is connected. For a drone, give its model "
             "and how Sentinel connects: the built-in simulator (try everything now), ArduPilot SITL, or a real aircraft (telemetry only).",
             {"category": string("Device kind", enum=["recorder", "drone", "dock", "contact_sensor", "motion_sensor", "environmental_sensor"]),
              "name": string("Name"), "profile": string("Hardware profile id from get_hardware_compatibility"),
              "address": string("Recorder address or alias"), "channels": {"type": "integer", "description": "Recorder channel count"},
              "zone_id": string("Sensor zone"),
              "drone_model": string("Drone model", enum=["sentinel_lite", "sentinel_pro", "sentinel_thermal", "generic_ardupilot"]),
              "connect": string("How Sentinel connects to the drone", enum=["simulator", "sitl", "aircraft"]),
              "mavlink_connection": string("MAVLink endpoint for SITL or a real aircraft, e.g. udpin:0.0.0.0:14551"),
              "obstacle_sensor": boolean("Sentinel Pro: the optional lidar is installed")}, ["category", "name"])
        def propose_register_device(args, turn):
            from ..devices import PROFILE_INDEX, default_profile

            category = args["category"]
            if category == "drone" and args.get("connect"):
                args = args | {"profile": {"simulator": "simulated_drone", "sitl": "ardupilot_sitl", "aircraft": "mavlink_drone"}[args["connect"]]}
            if category == "drone" and args.get("connect") in ("sitl", "aircraft") and not args.get("mavlink_connection"):
                raise ToolError("a SITL or real drone needs mavlink_connection (ask the customer which address and port the radio sends to)")
            profile = args.get("profile") or default_profile(category, category != "recorder")
            info = PROFILE_INDEX.get(profile)
            if info is None or info["category"] != category or info["support"] == "unsupported":
                raise ToolError(f"profile must be a supported {category} profile")
            params = {"category": category, "name": untrusted(args["name"], 60), "profile": profile, "simulated": info["support"] == "simulated"}
            if args.get("address"):
                params["host"] = turn.real(args["address"])
            if args.get("channels"):
                params["channels"] = int(args["channels"])
            if args.get("zone_id"):
                params["zone_id"] = args["zone_id"]
            effects = [f"Hardware: {info['name']} ({info['support']})"]
            if category == "drone":
                from ..drone import catalog

                model = catalog.get_model(args.get("drone_model") or "sentinel_lite")
                params["drone_model"] = model["id"]
                if args.get("mavlink_connection"):
                    params["connection"] = args["mavlink_connection"]
                if args.get("obstacle_sensor"):
                    if "obstacle_sensor" not in model["options"]:
                        raise ToolError(f"{model['name']} has no optional obstacle sensor")
                    params["drone_options"] = {"obstacle_sensor": True}
                effects.append(f"Model: {model['name']} ({model['cost_label'].lower()}); readiness checks follow this model")
                if model["status"] == "reference_design":
                    effects.append("Reference design: no Sentinel drone has been built or flown yet")
            if info["support"] == "planned":
                effects.append("Planned hardware: registration only, Sentinel cannot connect to it yet")
            if info["support"] == "simulated":
                effects.append("Simulated: exercises the software, watches nothing real")
            if category == "drone":
                if not self._config().get("site", {}).get("map", {}).get("origin_geo"):
                    raise ToolError("the property location must be set first (installation step: property)")
                effects.append("Turns on drone features with a geofence over the property; every flight still needs an operator's approval")
            return self._propose(turn, "register_device", params, f"Register {params['name']}", "Adds the device to Sentinel's registry.", effects)

        @add("propose_remove_device", "propose", "Propose removing a camera or device (its stored password, zones in rules and links go with it; "
             "recorded history is kept).", {"device_id": string("Device or camera id")}, ["device_id"])
        def propose_remove_device(args, turn):
            device = next((d for d in self.service.devices_view() if d["id"] == args["device_id"]), None)
            if device is None or not device.get("removable"):
                raise ToolError("unknown or non-removable device")
            effects = ["Recorded history is kept"]
            if device["category"] == "camera":
                effects = ["Its stored password, zones and connections are removed", "Rules left without zones are switched off"] + effects
            return self._propose(turn, "remove_device", {"device_id": device["id"]}, f"Remove {untrusted(device['name'])}", "Cannot be undone from here.", effects)

        @add("propose_drone_inspection", "propose", "Propose creating a drone inspection REQUEST for an inspection point or a zone with a map position. "
             "This never flies: an operator must still approve the flight on the Drone tab after preflight checks.",
             {"point_id": string("Inspection point id"), "zone_id": string("Zone id")})
        def propose_drone_inspection(args, turn):
            if self.service.drone is None:
                raise ToolError("no operable drone is registered")
            config = self._config()
            if args.get("point_id"):
                point = next((p for p in config.get("drone", {}).get("inspection_points", []) if p["id"] == args["point_id"]), None)
                if point is None:
                    raise ToolError("unknown inspection point")
                label = untrusted(point["name"])
            elif args.get("zone_id"):
                zone = next((z for c in config.get("cameras", []) for z in c.get("zones", []) if z["id"] == args["zone_id"]), None)
                if zone is None or not zone.get("map_polygon"):
                    raise ToolError("unknown zone or the zone has no map position")
                label = untrusted(zone.get("name", zone["id"]))
            else:
                raise ToolError("give point_id or zone_id")
            return self._propose(turn, "drone_inspection", {k: args[k] for k in ("point_id", "zone_id") if args.get(k)}, f"Request a drone inspection of {label}",
                                 "Creates a pending request only. Nothing flies until an operator approves it on the Drone tab after preflight checks.",
                                 ["A request appears on the Drone tab", "Audited"])

    # ---- applying confirmed proposals ----------------------------------------------------

    def apply(self, proposal: dict[str, Any], actor: str) -> dict[str, Any]:
        """Runs a confirmed proposal through the existing backend. Returns
        {"ok", "message", "data"?}; never raises for expected failures."""
        from ..installation import InstallationError
        from ..site_config import SiteConfigError

        kind, params = proposal["kind"], copy.deepcopy(proposal["params"])
        service, site = self.service, self.service.site
        try:
            if kind == "set_path":
                service.installation.update({"path": params["path"]})
                return {"ok": True, "message": "Installation type saved."}
            if kind == "step_status":
                service.installation.update({"steps": {params["step"]: params["status"]}})
                return {"ok": True, "message": f"Step marked {params['status']}."}
            if kind == "network_search":
                from .. import netdiag

                result = netdiag.discover_devices(service.network, sadp=True, onvif=True, port_check=params["port_check"])
                service.runtime.store.audit(actor, "network_discovery", {"methods": result["methods"], "networks": result["networks_searched"],
                                                                         "found": len(result["devices"]), "via": "assistant"})
                devices = [{"host": d["host"], "brand": d.get("brand"), "model": untrusted(d.get("model")), "name": untrusted(d.get("name")),
                            "found_by": d.get("found_by"), "on_local_network": d.get("on_local_network"), "network": d.get("network"),
                            "channels": d.get("channels"), "activated": d.get("activated")} for d in result["devices"]]
                return {"ok": True, "message": f"Network search finished: {len(devices)} device(s) answered.",
                        "data": {"devices": devices, "networks_searched": result["networks_searched"], "notes": result.get("notes", [])}}
            if kind == "save_rule":
                rule = dict(params["rule"]) | {"enabled": params["enable"]}
                saved = site.save_rule(params.get("rule_id"), rule, actor)
                return {"ok": True, "message": f"Rule '{saved.get('name') or saved['id']}' saved{' and turned on' if params['enable'] else ' (off)'}.",
                        "data": {"rule_id": saved["id"]}}
            if kind == "rule_switch":
                site.set_rule_enabled(params["rule_id"], params["turn_on"], actor)
                return {"ok": True, "message": f"Rule turned {'on' if params['turn_on'] else 'off'}."}
            if kind == "notifications":
                body: dict[str, Any] = {}
                if any(key in params for key in ("ntfy_enabled", "server", "attach_snapshot")):
                    body["ntfy"] = {"enabled": params.get("ntfy_enabled", service.notification_view()["ntfy"]["enabled"])}
                    if params.get("server"):
                        body["ntfy"]["server"] = params["server"]
                    if "attach_snapshot" in params:
                        body["ntfy"]["attach_snapshot"] = params["attach_snapshot"]
                if "mqtt_enabled" in params:
                    body["mqtt"] = {"enabled": params["mqtt_enabled"]}
                site.set_notifications(body, actor)
                return {"ok": True, "message": "Notification settings saved."}
            if kind == "register_device":
                device = site.save_device(None, params, actor)
                if params["category"] == "drone":
                    first = not service.config.get("drone", {}).get("device")
                    site.set_drone({"enabled": True, **({"device": device["id"]} if first else {})}, actor)
                return {"ok": True, "message": f"{device['name']} registered.", "data": {"device_id": device["id"]}}
            if kind == "remove_device":
                result = service.remove_device(params["device_id"], actor)
                return {"ok": True, "message": "Removed. " + " ".join(result.get("notes", [])).strip()}
            if kind == "drone_inspection":
                config = service.config
                if params.get("point_id"):
                    point = next(p for p in config.get("drone", {}).get("inspection_points", []) if p["id"] == params["point_id"])
                    request = service.drone.request_inspection(requested_by=actor, map_point=tuple(point["map"]), label=point["name"])
                else:
                    zone = service.runtime.zones[params["zone_id"]]
                    polygon = zone["map_polygon"]
                    centroid = (sum(p[0] for p in polygon) / len(polygon), sum(p[1] for p in polygon) / len(polygon))
                    request = service.drone.request_inspection(requested_by=actor, map_point=centroid, label=zone.get("name", zone["id"]))
                return {"ok": True, "message": "Inspection requested. It is waiting for an operator to approve the flight on the Drone tab.",
                        "data": {"request_id": request.request_id, "status": request.status}}
            return {"ok": False, "message": f"unknown action {kind}"}
        except (SiteConfigError, InstallationError, ValueError, KeyError, StopIteration) as error:
            return {"ok": False, "message": f"Not applied: {str(error).splitlines()[0][:300]}"}
        except Exception as error:  # the drone manager raises FlightCommandRejected (a RuntimeError)
            return {"ok": False, "message": f"Not applied: {type(error).__name__}: {str(error)[:200]}"}
