"""Network snapshot, routing, target policy and finding types.

Pure and standard-library only, so diagnosis rules can be tested against
simulated networks on any platform."""

from __future__ import annotations

import ipaddress
import re
import socket
import time
from dataclasses import asdict, dataclass, field
from typing import Any

# Adapter names/descriptions that identify VPN and virtual interfaces. Checked in
# this order: a "TAP-Windows" adapter is a VPN, a "Hyper-V" one is virtual.
_VPN_WORDS = (
    "vpn", "wireguard", "wintun", "tap-windows", "openvpn", "tailscale", "zerotier", "nordlynx", "anyconnect",
    "globalprotect", "pangp", "fortinet", "forticlient", "pulse secure", "sonicwall", "mullvad", "proton",
    "expressvpn", "surfshark", "hamachi", "softether", "ppp",
)
_VPN_LINUX = re.compile(r"^(tun|tap|wg|tailscale|zt|ppp|nordlynx|proton|mullvad)", re.I)
_VIRTUAL_WORDS = ("hyper-v", "vethernet", "virtualbox", "vmware", "docker", "wsl", "virtual", "bluetooth", "npcap", "loopback pseudo")
_VIRTUAL_LINUX = re.compile(r"^(docker|br-|veth|virbr|vmnet|vboxnet|lxc|cni|flannel)", re.I)
_WIFI_WORDS = ("wi-fi", "wifi", "wireless", "802.11", "wlan")


def classify_adapter(name: str, description: str = "") -> str:
    text = f"{name} {description}".lower()
    if name == "lo" or "loopback" in text and "pseudo" in text:
        return "loopback"
    if any(word in text for word in _VPN_WORDS) or _VPN_LINUX.match(name):
        return "vpn"
    if any(word in text for word in _VIRTUAL_WORDS) or _VIRTUAL_LINUX.match(name):
        return "virtual"
    if any(word in text for word in _WIFI_WORDS) or re.match(r"^wl", name):
        return "wifi"
    return "ethernet"


@dataclass
class Adapter:
    name: str
    description: str = ""
    kind: str = "ethernet"  # ethernet | wifi | vpn | virtual | loopback
    up: bool = True
    ipv4: list[str] = field(default_factory=list)  # CIDR, e.g. "10.0.0.57/24"
    gateway: str | None = None
    dhcp: bool | None = None
    dns: list[str] = field(default_factory=list)
    profile: str | None = None  # Windows network category: Public / Private / DomainAuthenticated
    metric: int = 0

    def interfaces(self) -> list[ipaddress.IPv4Interface]:
        result = []
        for cidr in self.ipv4:
            try:
                result.append(ipaddress.IPv4Interface(cidr))
            except ValueError:
                continue
        return result

    def usable_interfaces(self) -> list[ipaddress.IPv4Interface]:
        """Addresses that put this computer on a real network (not loopback or self-assigned)."""
        return [i for i in self.interfaces() if not i.ip.is_loopback and not i.ip.is_link_local]

    @property
    def self_assigned_only(self) -> bool:
        interfaces = self.interfaces()
        return bool(interfaces) and all(i.ip.is_link_local for i in interfaces)


@dataclass
class Route:
    destination: str  # CIDR
    interface: str  # adapter name
    next_hop: str | None = None  # None = on-link
    metric: int = 0


@dataclass
class NetworkSnapshot:
    platform: str
    adapters: list[Adapter]
    routes: list[Route]
    firewall: dict[str, Any] = field(default_factory=dict)  # {"Public": {"enabled": True}, ...}
    collected_at: float = field(default_factory=time.time)
    errors: list[str] = field(default_factory=list)

    def adapter(self, name: str) -> Adapter | None:
        return next((a for a in self.adapters if a.name == name), None)

    def local_networks(self, include_vpn: bool = False) -> list[tuple[Adapter, ipaddress.IPv4Interface]]:
        """(adapter, address) pairs for up adapters on real networks (no virtual/loopback)."""
        kinds = {"ethernet", "wifi"} | ({"vpn"} if include_vpn else set())
        return [(a, i) for a in self.adapters if a.up and a.kind in kinds for i in a.usable_interfaces()]

    def public_view(self) -> dict[str, Any]:
        """What the API returns. No MAC addresses, network names or public IPs are collected at all."""
        return {
            "platform": self.platform,
            "adapters": [asdict(a) for a in self.adapters if a.kind != "loopback"],
            "firewall": self.firewall,
            "collected_at_ms": int(self.collected_at * 1000),
            "errors": self.errors,
        }


def derive_routes(adapters: list[Adapter]) -> list[Route]:
    """Connected-subnet and default routes, for platforms or simulations without a route table."""
    routes = []
    for adapter in adapters:
        for interface in adapter.interfaces():
            routes.append(Route(str(interface.network), adapter.name, None, adapter.metric))
        if adapter.gateway:
            routes.append(Route("0.0.0.0/0", adapter.name, adapter.gateway, adapter.metric))
    return routes


@dataclass
class RouteChoice:
    adapter: Adapter
    source_ip: str
    next_hop: str | None  # None = target is on the adapter's own network

    @property
    def on_link(self) -> bool:
        return self.next_hop is None


def route_for(snapshot: NetworkSnapshot, ip: str) -> RouteChoice | None:
    """Longest-prefix match over the route table, like the OS does (lowest metric breaks ties)."""
    target = ipaddress.IPv4Address(ip)
    best: tuple[int, int, Route] | None = None
    for route in snapshot.routes:
        adapter = snapshot.adapter(route.interface)
        if adapter is None or not adapter.up or not adapter.interfaces():
            continue
        try:
            network = ipaddress.IPv4Network(route.destination, strict=False)
        except ValueError:
            continue
        if target not in network or network.is_multicast:
            continue
        key = (network.prefixlen, -route.metric)
        if best is None or key > best[:2]:
            best = (*key, route)
    if best is None:
        return None
    route = best[2]
    adapter = snapshot.adapter(route.interface)
    next_hop = route.next_hop if route.next_hop not in (None, "", "0.0.0.0") else None
    reference = ipaddress.IPv4Address(next_hop) if next_hop else target
    source = next((i for i in adapter.interfaces() if reference in i.network), adapter.interfaces()[0])
    return RouteChoice(adapter, str(source.ip), next_hop)


# ---- target policy ------------------------------------------------------------------


class TargetRefused(ValueError):
    pass


_HOST = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")


def check_target(host: str, resolve=socket.gethostbyname) -> str:
    """Sentinel only probes devices on local or private networks, one address at a time.
    Public internet addresses are refused, whoever asks."""
    host = (host or "").strip()
    if not _HOST.match(host):
        raise TargetRefused("Enter an IPv4 address (like 192.168.1.64) or a local host name.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            address = ipaddress.ip_address(resolve(host))
        except (OSError, UnicodeError):
            raise TargetRefused(f"The name '{host}' could not be resolved. Use the device's IP address.") from None
    if address.version != 4:
        raise TargetRefused("Only IPv4 addresses are supported.")
    if address.is_multicast or address.is_unspecified or address.is_reserved:
        raise TargetRefused(f"{address} is not a device address.")
    if address.is_global:
        raise TargetRefused(
            f"{address} is a public internet address. Sentinel only tests devices on your own local network; "
            "for a remote site, connect through a VPN first."
        )
    return str(address)


def sweep_networks(snapshot: NetworkSnapshot) -> list[ipaddress.IPv4Network]:
    """Networks a user-approved port check may cover: this computer's own private
    networks, capped at the /24 around its address (at most 254 hosts each)."""
    result = []
    for _, interface in snapshot.local_networks():
        network = interface.network if interface.network.prefixlen >= 24 else ipaddress.IPv4Interface(f"{interface.ip}/24").network
        if network.is_private and network not in result:
            result.append(network)
    return result


# ---- findings ---------------------------------------------------------------------

STATUSES = ("confirmed", "suspected", "info", "ruled_out", "ok")


@dataclass
class Finding:
    """`confirmed`: Sentinel observed the fault directly. `suspected`: a likely cause
    the evidence points to but cannot prove. `ruled_out`: checked and not the problem."""

    code: str
    status: str
    title: str
    explanation: str
    evidence: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def sort_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: STATUSES.index(f.status))


@dataclass
class PingResult:
    kind: str  # reply | timeout | host_unreachable | net_unreachable | failure | unavailable
    from_ip: str | None = None
    rtt_ms: int | None = None
