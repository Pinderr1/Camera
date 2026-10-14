"""Read this computer's network configuration (read-only, never changed).

One collector per platform, each split into a command runner and a pure parser
so the parsers can be tested with recorded output. Windows uses the NetTCPIP /
NetConnection / NetSecurity PowerShell modules in a single call (the first call
can take several seconds while WMI warms up); Linux uses `ip -j`. Nothing that
identifies the network (MAC addresses, Wi-Fi names, public IP) is collected."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

from .model import Adapter, NetworkSnapshot, Route, classify_adapter

_WINDOWS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$out = @{
 adapters = @(Get-NetAdapter | Select-Object Name, InterfaceDescription, ifIndex, @{n='Status';e={"$($_.Status)"}})
 interfaces = @(Get-NetIPInterface -AddressFamily IPv4 | Select-Object ifIndex, InterfaceAlias, InterfaceMetric, @{n='Dhcp';e={"$($_.Dhcp)"}}, @{n='ConnectionState';e={"$($_.ConnectionState)"}})
 addresses = @(Get-NetIPAddress -AddressFamily IPv4 | Select-Object ifIndex, IPAddress, PrefixLength)
 routes = @(Get-NetRoute -AddressFamily IPv4 | Select-Object ifIndex, DestinationPrefix, NextHop, RouteMetric)
 profiles = @(Get-NetConnectionProfile | Select-Object InterfaceIndex, @{n='NetworkCategory';e={"$($_.NetworkCategory)"}})
 firewall = @(Get-NetFirewallProfile | Select-Object Name, @{n='Enabled';e={"$($_.Enabled)"}}, @{n='DefaultInboundAction';e={"$($_.DefaultInboundAction)"}})
 dns = @(Get-DnsClientServerAddress -AddressFamily IPv4 | Select-Object InterfaceIndex, ServerAddresses)
}
$out | ConvertTo-Json -Depth 4 -Compress
"""


def collect(timeout: float = 45.0) -> NetworkSnapshot:
    if sys.platform == "win32":
        return collect_windows(timeout)
    if sys.platform.startswith("linux"):
        return collect_linux(timeout)
    return NetworkSnapshot(sys.platform, [], [], errors=[f"Network inspection is not supported on {sys.platform} yet."])


def _run(command: list[str], timeout: float) -> str:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, creationflags=flags,
                               env=dict(os.environ, LC_ALL="C"))
    return completed.stdout


def collect_windows(timeout: float = 45.0) -> NetworkSnapshot:
    try:
        output = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_SCRIPT], timeout)
        return parse_windows(json.loads(output))
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return NetworkSnapshot("windows", [], [], errors=[f"Could not read network settings ({type(error).__name__})."])


def _list(value: Any) -> list[Any]:
    """ConvertTo-Json turns one-element arrays into objects and empty ones into null."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def parse_windows(payload: dict[str, Any]) -> NetworkSnapshot:
    interfaces = {item["ifIndex"]: item for item in _list(payload.get("interfaces"))}
    adapters_raw = {item["ifIndex"]: item for item in _list(payload.get("adapters"))}
    profiles = {item["InterfaceIndex"]: item.get("NetworkCategory") for item in _list(payload.get("profiles"))}
    dns = {item["InterfaceIndex"]: [s for s in _list(item.get("ServerAddresses")) if s] for item in _list(payload.get("dns"))}
    addresses: dict[int, list[str]] = {}
    for item in _list(payload.get("addresses")):
        addresses.setdefault(item["ifIndex"], []).append(f"{item['IPAddress']}/{item['PrefixLength']}")
    routes_raw = _list(payload.get("routes"))
    gateways = {item["ifIndex"]: item["NextHop"] for item in sorted(routes_raw, key=lambda r: r.get("RouteMetric") or 0, reverse=True)
                if item.get("DestinationPrefix") == "0.0.0.0/0" and item.get("NextHop") not in (None, "0.0.0.0")}
    names: dict[int, str] = {}
    adapters = []
    for index in sorted(set(interfaces) | set(adapters_raw)):
        raw = adapters_raw.get(index)
        interface = interfaces.get(index, {})
        name = (raw or {}).get("Name") or interface.get("InterfaceAlias") or f"interface {index}"
        description = (raw or {}).get("InterfaceDescription") or ""
        if raw is not None:
            up = raw.get("Status") == "Up"
        else:  # hidden adapters (e.g. Wi-Fi Direct): trust the IP interface state
            up = interface.get("ConnectionState") == "Connected"
        names[index] = name
        kind = classify_adapter(name, description)
        if raw is None and kind == "ethernet":
            kind = "virtual"  # hidden system adapters (Wi-Fi Direct, Teredo) are not where cameras live
        adapters.append(Adapter(
            name=name, description=description, kind=kind, up=up,
            ipv4=addresses.get(index, []), gateway=gateways.get(index),
            dhcp={"Enabled": True, "Disabled": False}.get(interface.get("Dhcp")),
            dns=dns.get(index, []), profile=profiles.get(index), metric=int(interface.get("InterfaceMetric") or 0),
        ))
    metrics = {index: int(item.get("InterfaceMetric") or 0) for index, item in interfaces.items()}
    routes = [
        Route(item["DestinationPrefix"], names[item["ifIndex"]], item.get("NextHop"), int(item.get("RouteMetric") or 0) + metrics.get(item["ifIndex"], 0))
        for item in routes_raw if item.get("ifIndex") in names
    ]
    firewall = {
        item["Name"]: {"enabled": item.get("Enabled") == "True", "default_inbound": item.get("DefaultInboundAction") or "NotConfigured"}
        for item in _list(payload.get("firewall")) if item.get("Name")
    }
    return NetworkSnapshot("windows", adapters, routes, firewall)


def collect_linux(timeout: float = 15.0) -> NetworkSnapshot:
    try:
        links = json.loads(_run(["ip", "-j", "addr", "show"], timeout) or "[]")
        routes = json.loads(_run(["ip", "-j", "-4", "route", "show", "table", "main"], timeout) or "[]")
        return parse_linux(links, routes)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return NetworkSnapshot("linux", [], [], errors=[f"Could not read network settings ({type(error).__name__}); is iproute2 installed?"])


def parse_linux(links: list[dict[str, Any]], routes_raw: list[dict[str, Any]]) -> NetworkSnapshot:
    gateways = {r.get("dev"): r.get("gateway") for r in routes_raw if r.get("dst") == "default" and r.get("gateway")}
    adapters = []
    for link in links:
        name = link.get("ifname", "")
        inet = [a for a in link.get("addr_info", []) if a.get("family") == "inet"]
        adapters.append(Adapter(
            name=name, description=link.get("link_type", ""), kind=classify_adapter(name),
            up="UP" in link.get("flags", []) and link.get("operstate") in ("UP", "UNKNOWN"),
            ipv4=[f"{a['local']}/{a['prefixlen']}" for a in inet], gateway=gateways.get(name),
            dhcp=any(a.get("dynamic") for a in inet) if inet else None,
        ))
    routes = [
        Route("0.0.0.0/0" if r.get("dst") == "default" else (r["dst"] if "/" in r["dst"] else r["dst"] + "/32"),
              r.get("dev", ""), r.get("gateway"), int(r.get("metric", 0)))
        for r in routes_raw if r.get("dst") and r.get("type", "unicast") == "unicast"
    ]
    return NetworkSnapshot("linux", adapters, routes)
