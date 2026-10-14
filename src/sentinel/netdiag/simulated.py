"""Simulated network conditions for tests and demos.

A scenario describes this computer's adapters, the physical segments (cable/Wi-Fi
networks) they sit on, routers, and devices with their *own* IP settings. Packets
are delivered the way real networks do it: directly inside a subnet on the same
segment, otherwise via a router, and replies need a working path back. Multicast
discovery (SADP) reaches every device on the segment whatever its address;
ONVIF replies are unicast and need that path back. See config/netsim/*.json.

    python -m sentinel serve --simulate-network config/netsim/hikvision_wrong_subnet.json
"""

from __future__ import annotations

import copy
import ipaddress
import json
import threading
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from ..streams import BRAND_TEMPLATES
from .model import Adapter, NetworkSnapshot, PingResult, Route, derive_routes, route_for


class SimulatedNetwork:
    def __init__(self, scenario: dict[str, Any]) -> None:
        self._lock = threading.RLock()
        self.scenario = copy.deepcopy(scenario)
        self.log: list[tuple[str, str, int | None]] = []  # (probe, host, port): what a test may assert on

    @classmethod
    def from_file(cls, path: str | Path) -> "SimulatedNetwork":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    # ---- scenario editing (a test "fixing" the network) ------------------------

    def device(self, address: str) -> dict[str, Any]:
        return next(d for d in self.scenario.get("devices", []) if d["ip"] == address)

    def update_device(self, address: str, **changes: Any) -> None:
        """e.g. update_device("192.168.1.84", ip="10.0.0.84", gateway="10.0.0.1"): the recorder was re-addressed."""
        with self._lock:
            self.device(address).update(changes)

    def update_adapter(self, name: str, **changes: Any) -> None:
        with self._lock:
            next(a for a in self.scenario["adapters"] if a["name"] == name).update(changes)

    # ---- backend interface -------------------------------------------------------

    def snapshot(self, refresh: bool = False) -> NetworkSnapshot:
        with self._lock:
            adapters = [Adapter(**{k: v for k, v in a.items() if k != "segment"}) for a in self.scenario["adapters"]]
            routes = [Route(**r) for r in self.scenario["routes"]] if "routes" in self.scenario else derive_routes(adapters)
            return NetworkSnapshot(self.scenario.get("platform", "windows"), adapters, routes, copy.deepcopy(self.scenario.get("firewall", {})))

    def tcp_connect(self, host: str, port: int, timeout: float = 1.5) -> str:
        self.log.append(("tcp", host, port))
        outcome, node = self._deliver(host)
        if outcome != "delivered":
            return {"no_route": "no_route"}.get(outcome, "timeout")
        state = {int(p): s for p, s in node.get("ports", {}).items()}.get(int(port), node.get("default_port_state", "refused"))
        return {"open": "open", "refused": "refused"}.get(state, "timeout")

    def ping(self, host: str, timeout: float = 1.0) -> PingResult:
        self.log.append(("ping", host, None))
        outcome, node = self._deliver(host)
        if outcome == "delivered":
            return PingResult("reply", host, 2) if node.get("icmp", True) else PingResult("timeout")
        if outcome == "no_route":
            return PingResult("failure")
        if outcome == "arp_failed":
            return PingResult("host_unreachable", node)  # node = the address that gave up (this computer or a router)
        return PingResult("timeout")

    def discover_sadp(self, snapshot: NetworkSnapshot, timeout: float = 2.5) -> list[dict[str, Any]]:
        self.log.append(("sadp", "", None))
        if self._discovery_blocked():
            return []
        segments = self._up_segments()
        return [{
            "host": d["ip"], "method": "sadp", "brand": "hikvision", "model": d.get("model"), "mask": d.get("mask"),
            "gateway": d.get("gateway"), "dhcp": d.get("dhcp", False), "activated": d.get("activated", True),
            "http_port": 80, "command_port": 8000, "channels": d.get("channels"), "firmware": d.get("firmware"),
        } for d in self.scenario.get("devices", []) if d.get("sadp") and d.get("powered", True) and d.get("segment") in segments]

    def discover_onvif(self, snapshot: NetworkSnapshot, timeout: float = 2.5) -> list[dict[str, Any]]:
        self.log.append(("onvif", "", None))
        if self._discovery_blocked():
            return []
        segments = self._up_segments()
        return [{"host": d["ip"], "method": "onvif", "name": d.get("name"), "model": d.get("model"),
                 "xaddr": f"http://{d['ip']}/onvif/device_service"}
                for d in self.scenario.get("devices", [])
                if d.get("onvif") and d.get("powered", True) and d.get("segment") in segments and self._deliver(d["ip"])[0] == "delivered"]

    def http_identify(self, host: str, port: int = 80) -> dict[str, Any] | None:
        outcome, node = self._deliver(host)
        if outcome != "delivered" or str(node.get("ports", {}).get(str(port))) != "open":
            return None
        return {"status": 200, "server": "", "auth_scheme": "", "title": "", "markers": []} | node.get("http", {})

    def rtsp_probe(self, url: str) -> dict[str, Any]:
        from ..onboarding import ADVICE

        parts = urlsplit(url)
        result = lambda kind, **extra: {"kind": kind, "ok": kind == "ok", "message": ADVICE.get(kind, kind)} | extra
        state = self.tcp_connect(parts.hostname or "", parts.port or 554)
        if state != "open":
            return result({"refused": "port_closed", "timeout": "unreachable", "no_route": "unreachable"}.get(state, "unreachable"))
        node = self._deliver(parts.hostname or "")[1]
        if not parts.username:
            return result("auth_required")
        if (unquote(parts.username), unquote(parts.password or "")) != (node.get("username", "admin"), node.get("password", "")):
            return result("auth_failed")
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        template = BRAND_TEMPLATES.get(node.get("template", "hikvision"), {})
        for channel in range(1, int(node.get("channels", 1)) + 1):
            for which in ("main", "sub"):
                if template.get(which) and template[which].format(channel=channel) == path:
                    return result("ok", codec=node.get("codecs", {}).get(which, "H264"))
        return result("not_found")

    # ---- packet delivery -------------------------------------------------------------

    def _discovery_blocked(self) -> bool:
        firewall = self.scenario.get("firewall", {})
        return any(a.get("profile") == "Public" and a.get("up", True) for a in self.scenario["adapters"]) and \
            firewall.get("Public", {}).get("blocks_discovery", False)

    def _up_segments(self) -> set[str]:
        return {a.get("segment") for a in self.scenario["adapters"] if a.get("up", True) and a.get("ipv4")}

    def _nodes(self, segment: str) -> list[tuple[str, str, dict[str, Any]]]:
        """(ip, mask, node) for every powered device and router interface on a segment."""
        nodes = [(d["ip"], d.get("mask", "255.255.255.0"), d) for d in self.scenario.get("devices", [])
                 if d.get("segment") == segment and d.get("powered", True)]
        for router in self.scenario.get("routers", []):
            nodes += [(i["ip"], i.get("mask", "255.255.255.0"), router | {"_iface": i}) for i in router["interfaces"] if i["segment"] == segment]
        return nodes

    def _deliver(self, host: str) -> tuple[str, Any]:
        """('delivered', node) | ('no_route', None) | ('arp_failed', reporter_ip) | ('lost', None)"""
        with self._lock:
            snapshot = self.snapshot()
            route = route_for(snapshot, host)
            if route is None:
                return "no_route", None
            adapter = next(a for a in self.scenario["adapters"] if a["name"] == route.adapter.name)
            segment = adapter.get("segment")
            hop = route.next_hop or host
            first = next((n for n in self._nodes(segment) if n[0] == hop), None)
            if first is None:
                return "arp_failed", route.source_ip
            if route.on_link:
                ip, mask, node = first
                return ("delivered", node) if self._can_reply(node, ip, mask, route.source_ip, segment) else ("lost", None)
            router = first[2]
            target = ipaddress.IPv4Address(host)
            for interface in router["interfaces"]:
                if interface["ip"] == host:
                    return "delivered", router
                if target in ipaddress.IPv4Network(f"{interface['ip']}/{interface.get('mask', '255.255.255.0')}", strict=False):
                    node = next((n for n in self._nodes(interface["segment"]) if n[0] == host), None)
                    if node is None:
                        return ("arp_failed", interface["ip"]) if router.get("reports_unreachable") else ("lost", None)
                    # Home routers NAT: the device sees the router's own address on its segment as the sender.
                    return ("delivered", node[2]) if self._can_reply(node[2], node[0], node[1], interface["ip"], interface["segment"]) else ("lost", None)
            return "lost", None  # private address the router does not know: dropped upstream

    def _can_reply(self, node: dict[str, Any], ip: str, mask: str, sender: str, segment: str) -> bool:
        if ipaddress.IPv4Address(sender) in ipaddress.IPv4Network(f"{ip}/{mask}", strict=False):
            return True
        gateway = node.get("gateway")  # reply goes to the device's own gateway, which must exist on its segment and know the way
        return bool(gateway) and any(n[0] == gateway and "_iface" in n[2] for n in self._nodes(segment)) and node.get("_iface") is None \
            and self._router_reaches(gateway, sender, segment)

    def _router_reaches(self, gateway: str, sender: str, segment: str) -> bool:
        router = next(n[2] for n in self._nodes(segment) if n[0] == gateway)
        return any(ipaddress.IPv4Address(sender) in ipaddress.IPv4Network(f"{i['ip']}/{i.get('mask', '255.255.255.0')}", strict=False)
                   for i in router["interfaces"])
