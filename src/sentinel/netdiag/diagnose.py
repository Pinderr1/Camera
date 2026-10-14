"""Turn network observations into findings a homeowner can act on.

`troubleshoot()` gathers evidence through a backend (real or simulated);
`analyze_network()` and `analyze_target()` are pure rules over that evidence.
Each finding says whether the fault was observed (`confirmed`), is a likely
cause the evidence cannot prove (`suspected`), or was checked and excluded
(`ruled_out`), and lists the evidence behind it. Rules never assume one cause:
a recorder on another address range might also be switched off or cabled to a
different router, and the findings say so."""

from __future__ import annotations

import ipaddress
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from . import guidance
from .model import (
    Finding, NetworkSnapshot, PingResult, RouteChoice, TargetRefused, check_target, route_for, sort_findings, sweep_networks,
)
from .probes import NetworkBackend, identify

PORT_ROLES = {80: "web page", 443: "secure web page", 554: "RTSP video", 8000: "Hikvision app port", 37777: "Dahua app port", 9000: "Reolink app port"}
SWEEP_PORTS = (80, 554, 8000, 37777, 9000)
TCP_TIMEOUT_S = 1.5
KIND_LABELS = {"ethernet": "Ethernet", "wifi": "Wi-Fi", "vpn": "VPN", "virtual": "virtual adapter"}


def _primary(snapshot: NetworkSnapshot, route: RouteChoice | None = None) -> dict[str, str] | None:
    """The local network a fix should target: the one traffic to the device leaves
    through, else the one with the default route."""
    networks = snapshot.local_networks()
    if route is not None:
        chosen = next(((a, i) for a, i in networks if a is route.adapter), None)
    else:
        chosen = None
    if chosen is None:
        with_gateway = sorted((pair for pair in networks if pair[0].gateway), key=lambda pair: pair[0].metric)
        chosen = (with_gateway or networks or [None])[0]
    if chosen is None:
        return None
    adapter, interface = chosen
    return {
        "adapter": adapter.name, "kind": adapter.kind, "address": str(interface.ip), "network": str(interface.network),
        "mask": str(interface.netmask), "gateway": adapter.gateway or "", "profile": adapter.profile or "",
    }


def analyze_network(snapshot: NetworkSnapshot) -> list[Finding]:
    findings: list[Finding] = []
    if snapshot.errors:
        findings.append(Finding("inspection_failed", "info", "Some network settings could not be read", " ".join(snapshot.errors)))
    networks = snapshot.local_networks()
    physical = [a for a in snapshot.adapters if a.up and a.kind in ("ethernet", "wifi")]
    if not networks:
        self_assigned = [a for a in physical if a.self_assigned_only]
        if self_assigned:
            names = ", ".join(a.name for a in self_assigned)
            findings.append(Finding(
                "dhcp_failed", "confirmed", "This computer did not get an address from the router",
                f"'{names}' is connected but only has a self-assigned 169.254.x.x address, which means no router answered its request for an address.",
                [f"{a.name}: {', '.join(a.ipv4)}" for a in self_assigned],
                ["Check that the network cable is plugged into the router (or that Wi-Fi is connected to the right network).",
                 "Restart the router, wait two minutes, then click 'Check again'."],
            ))
        elif not snapshot.errors:
            findings.append(Finding(
                "no_connection", "confirmed", "This computer is not connected to a network",
                "No network adapter is connected with an address, so no camera can be reached.",
                [f"{a.name} ({KIND_LABELS.get(a.kind, a.kind)}): {'connected' if a.up else 'disconnected'}" for a in snapshot.adapters if a.kind in ("ethernet", "wifi")],
                ["Plug in a network cable or connect to Wi-Fi, then click 'Check again'."],
            ))
        return findings
    primary = _primary(snapshot)
    findings.append(Finding(
        "connected", "ok", f"Connected to {primary['network']} over {KIND_LABELS.get(primary['kind'], primary['kind'])}",
        f"This computer is {primary['address']} on '{primary['adapter']}'"
        + (f", and its router (default gateway) is {primary['gateway']}." if primary["gateway"] else "."),
        [f"{a.name}: {', '.join(str(i) for i in a.usable_interfaces())}" + (f", gateway {a.gateway}" if a.gateway else "")
         + (", address from DHCP" if a.dhcp else ", fixed address" if a.dhcp is False else "") for a in {id(a): a for a, _ in networks}.values()],
    ))
    if not any(a.gateway for a, _ in networks):
        findings.append(Finding("no_gateway", "info", "No router (default gateway) is set",
                                f"Only devices inside {', '.join(str(i.network) for _, i in networks)} can be reached."))
    distinct = {str(i.network) for _, i in networks}
    if len(distinct) > 1:
        findings.append(Finding(
            "multiple_networks", "info", "This computer is on more than one network",
            "Cameras must be on one of these networks (or reachable through its router). A recorder cabled to the other network will only "
            "be reachable through that adapter.",
            [f"{a.name}: {i.network}" for a, i in networks],
        ))
    for adapter in physical:
        if adapter.self_assigned_only:
            findings.append(Finding("adapter_no_address", "info", f"'{adapter.name}' is connected but has no address from a router",
                                    "If the recorder is plugged into this adapter, the computer and recorder are not on a working network together.",
                                    [f"{adapter.name}: {', '.join(adapter.ipv4)}"]))
    for vpn in (a for a in snapshot.adapters if a.up and a.kind == "vpn"):
        default_via_vpn = any(r.interface == vpn.name and r.destination == "0.0.0.0/0" for r in snapshot.routes) and \
            route_for(snapshot, "8.8.8.8") is not None and route_for(snapshot, "8.8.8.8").adapter is vpn
        findings.append(Finding(
            "vpn_active", "suspected" if default_via_vpn else "info", f"A VPN is connected: '{vpn.name}'",
            ("All of this computer's traffic goes through it. Many VPNs then block access to local devices such as recorders."
             if default_via_vpn else "It does not carry all traffic, but some VPN apps still block local devices with their own firewall."),
            [f"{vpn.name} ({vpn.description})".strip()] + (["Default route goes through the VPN"] if default_via_vpn else []),
            guidance.vpn_steps(vpn.name) if default_via_vpn else [],
        ))
    if primary["profile"] == "Public":
        findings.append(Finding(
            "public_profile", "info", "Windows treats this network as Public",
            "On a Public network the Windows firewall blocks incoming replies that automatic camera discovery relies on, so a search may find "
            "nothing even when cameras are present. Connecting to an address you type in is not affected.",
            [f"'{primary['adapter']}' network profile: Public"] + [f"Firewall ({name}) is on" for name, value in snapshot.firewall.items() if name == "Public" and value.get("enabled")],
            guidance.network_profile_steps(snapshot.platform),
        ))
    # Problems first, then the "connected" summary, then notes.
    order = ("confirmed", "suspected", "ok", "info", "ruled_out")
    return sorted(findings, key=lambda f: order.index(f.status))


@dataclass
class Observation:
    snapshot: NetworkSnapshot
    ip: str
    route: RouteChoice | None
    ping: PingResult
    ports: dict[int, str]
    brand: str | None = None
    device_mask: str | None = None
    device_gateway: str | None = None
    local_gateway_alive: bool | None = None
    device_gateway_alive: bool | None = None
    discovery_ran: bool = False
    discovered: list[dict[str, Any]] = field(default_factory=list)
    identity: dict[str, Any] | None = None
    rtsp_port: int = 554
    http_port: int = 80


def _ping_evidence(ping: PingResult, route: RouteChoice | None) -> str:
    if ping.kind == "reply":
        return f"Ping: reply in {ping.rtt_ms} ms" if ping.rtt_ms is not None else "Ping: reply"
    if ping.kind == "timeout":
        return "Ping: no reply within 1 s (on its own this proves little; many devices ignore ping)"
    if ping.kind in ("host_unreachable", "net_unreachable"):
        if route is not None and ping.from_ip == route.source_ip:
            return "Ping: 'destination host unreachable' from this computer itself: no device answered for that address on the local network"
        what = "host" if ping.kind == "host_unreachable" else "network"
        return f"Ping: 'destination {what} unreachable' reported by {ping.from_ip or 'the network'}"
    return "Ping could not be run on this computer"


def _port_evidence(ports: dict[int, str]) -> list[str]:
    words = {"open": "accepted a connection", "refused": "refused the connection (the device is there, the port is closed)",
             "timeout": "no answer", "no_route": "no route", "host_unreachable": "host unreachable", "error": "error"}
    return [f"Port {port} ({PORT_ROLES.get(port, 'TCP')}): {words.get(state, state)}" for port, state in sorted(ports.items())]


def analyze_target(o: Observation) -> list[Finding]:
    findings: list[Finding] = []
    snapshot, ip, route = o.snapshot, o.ip, o.route
    platform = snapshot.platform
    brand = o.brand if o.brand and o.brand != "other" else (o.identity or {}).get("brand")
    label = guidance.brand(brand)["label"] if brand else "device"
    answered = [p for p, s in o.ports.items() if s in ("open", "refused")]
    reachable = o.ping.kind == "reply" or bool(answered)
    sadp_hit = next((d for d in o.discovered if d["host"] == ip and d.get("method") == "sadp"), None)
    local = _primary(snapshot, route)
    networks = snapshot.local_networks()
    in_local = any(ipaddress.IPv4Address(ip) in i.network for _, i in networks)

    if route is None or local is None:
        findings.append(Finding(
            "no_route", "confirmed", f"This computer has no network path to {ip}",
            "It is not connected to any network that leads to that address.",
            [f"{a.name}: {', '.join(a.ipv4) or 'no address'} ({'connected' if a.up else 'disconnected'})" for a in snapshot.adapters if a.kind in ("ethernet", "wifi")],
            ["Connect this computer to the same network as the recorder (cable or Wi-Fi), then click Retest."],
        ))
        return findings

    adapter = route.adapter
    path = "directly on your network" if route.on_link else f"via your router {route.next_hop}"
    if adapter.kind == "vpn":
        findings.append(Finding(
            "vpn_route", "info" if reachable else "confirmed", f"Traffic to {ip} goes into the VPN '{adapter.name}'",
            "This computer sends traffic for that address into the VPN instead of the local network."
            + ("" if reachable else " The device did not answer through it."),
            [f"Route lookup: {ip} → {adapter.name} ({adapter.description})".replace(" ()", "")],
            [] if reachable else guidance.vpn_steps(adapter.name),
        ))

    if reachable:
        findings.append(Finding("reachable", "ok", f"The {label} at {ip} is reachable", f"It answered {path}.",
                                [_ping_evidence(o.ping, route)] + _port_evidence({p: s for p, s in o.ports.items() if s in ("open", "refused")})))
        if o.ping.kind != "reply":
            findings.append(Finding(
                "ping_blocked", "ruled_out", "Ping gets no reply, but that is not the problem",
                "This device ignores ping (many recorders and cameras do), yet it accepted a network connection, so the path to it works. "
                "This is also why a ping test can make a working device look offline.",
                [_ping_evidence(o.ping, route)] + _port_evidence({p: o.ports[p] for p in answered}),
            ))
        if not route.on_link:
            findings.append(Finding(
                "routed", "info", f"{ip} is on a different network, reached through your router",
                "That works. If video stutters, put the recorder on the same network as this computer.",
                [f"This computer: {local['network']}; route to {ip} goes via {route.next_hop}"]))
        http_state = o.ports.get(o.http_port)
        if http_state == "open":
            suffix = "" if o.http_port == 80 else f":{o.http_port}"
            findings.append(Finding("web_ok", "ok", "Its web page is available", f"Open http://{ip}{suffix}/ in a browser to reach its settings."))
        elif o.ports.get(443) == "open":
            findings.append(Finding("web_https", "info", "Its web page uses HTTPS", f"Open https://{ip}/ in a browser (accept the certificate warning for your own device)."))
        else:
            findings.append(Finding(
                "web_closed", "suspected", f"No web page on port {o.http_port}",
                "A browser cannot open it on that port: the web server may be turned off or use another port. Sentinel does not need it; "
                "video comes over RTSP.", _port_evidence({o.http_port: http_state or "timeout"})))
        rtsp_state = o.ports.get(o.rtsp_port)
        brand_rtsp = guidance.brand(brand)["rtsp"]
        if rtsp_state == "open":
            findings.append(Finding("rtsp_open", "ok", f"The RTSP video port ({o.rtsp_port}) is open",
                                    "Next: test the login and the main and sub streams."))
        elif rtsp_state == "refused":
            findings.append(Finding(
                "rtsp_closed", "confirmed", f"The {label} refused connections on port {o.rtsp_port} (RTSP)",
                "RTSP is turned off, or set to a different port. Sentinel needs RTSP to receive video.",
                _port_evidence({o.rtsp_port: rtsp_state}),
                brand_rtsp + ["If you know the RTSP port, enter it under 'More options' and click Retest."]))
        else:
            findings.append(Finding(
                "rtsp_filtered", "suspected", f"Port {o.rtsp_port} (RTSP) did not answer",
                "The device answered on other ports but not this one: RTSP may be disabled, or the device's IP filter or firewall blocks it.",
                _port_evidence({o.rtsp_port: rtsp_state or "timeout"}), brand_rtsp))
        if sadp_hit and sadp_hit.get("activated") is False:
            findings.append(Finding(
                "not_activated", "confirmed", "The recorder has not been activated",
                "Hikvision devices refuse logins until an admin password is set.", ["SADP reports: not activated"],
                ["Activate it from the recorder's own screen (it asks for a new admin password on first start), or with Hikvision SADP.", "Then click Retest."]))
        if o.identity:
            identity = o.identity
            findings.append(Finding(
                "identified", "info",
                f"{'Identified' if identity['confidence'] == 'confirmed' else 'Looks like'} a {guidance.brand(identity['brand'])['label']} device"
                + (f" ({identity['model']})" if identity.get("model") else ""),
                "Use the matching brand template when adding it.", identity["evidence"], data={"brand": identity["brand"]}))
        return sort_findings(findings)

    # ---- not reachable ----------------------------------------------------------------
    hard = o.ping.kind in ("host_unreachable", "net_unreachable") or (o.ports and all(s in ("no_route", "host_unreachable") for s in o.ports.values()))
    evidence = [_ping_evidence(o.ping, route), f"No answer on TCP ports {', '.join(str(p) for p in sorted(o.ports))} "
                f"(web, RTSP and the usual recorder app ports), {TCP_TIMEOUT_S:g} s each"]
    findings.append(Finding(
        "unreachable", "confirmed", f"{ip} cannot be reached from this computer",
        ("The network reports there is no way to reach it." if hard else
         "Nothing answered: not ping, and none of the usual camera and recorder ports. A failed ping alone would prove little, "
         "because many devices ignore ping; here no connection could be opened either."),
        evidence + [f"Traffic to {ip} leaves through '{adapter.name}' {path}"]))

    if o.local_gateway_alive is False and route.next_hop:
        findings.append(Finding(
            "gateway_down", "confirmed", f"Your router {route.next_hop} did not answer either",
            "This computer cannot reach its own router, so nothing beyond it can be reached.",
            [f"No answer from {route.next_hop} (ping and web port)"],
            ["Check the cable or Wi-Fi connection.", "Restart the router, wait two minutes, then click Retest."]))

    mask = (sadp_hit or {}).get("mask") or o.device_mask
    device_gateway = (sadp_hit or {}).get("gateway") or o.device_gateway
    device_net = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False) if mask else None
    offline_steps = [
        "Check that the recorder is switched on and its network (LAN) light is lit or blinking.",
        f"On the recorder's own monitor, open {guidance.brand(brand)['network_menu']} and read its current IP address.",
        "If the address shown is different, enter that address here and click Retest.",
    ]
    if not in_local:
        device_net_text = str(device_net or ipaddress.IPv4Network(f"{ip}/24", strict=False))
        detail = [
            f"This computer: {local['address']} on {local['network']} ('{local['adapter']}'), router {local['gateway'] or 'none'}",
            f"Recorder: {ip}" + (f", subnet mask {mask}" if mask else "") + (f", gateway {device_gateway}" if device_gateway else "")
            + f": network {device_net_text}",
            f"{ip} is outside {local['network']}, so this computer cannot talk to it directly and hands the traffic to its router instead",
        ]
        if device_gateway and not any(ipaddress.IPv4Address(device_gateway) in i.network for _, i in networks):
            detail.append(f"The recorder's gateway {device_gateway} is not on your network either (your router is {local['gateway'] or 'not set'}): "
                          "the recorder still has settings for another network, often a previous router or internet provider, or the installer's network")
        if o.device_gateway_alive is False:
            detail.append(f"{device_gateway} did not answer, so there is no sign of the {device_net_text} network from this computer")
        if o.discovery_ran and not sadp_hit and brand in (None, "hikvision"):
            detail.append("No Hikvision device answered SADP discovery with that address"
                          + (" (but Windows may be blocking discovery replies; see below)" if local.get("profile") == "Public" else ""))
        steps = guidance.readdress_steps(brand, local, ip) + guidance.temporary_alias_steps(platform, device_net_text)
        if sadp_hit:
            findings.append(Finding(
                "subnet_mismatch", "confirmed", "The recorder is on your network, but set up for a different one",
                f"It answered discovery on this computer's network, so the cable is fine, but its address {ip} belongs to {device_net_text} while "
                f"this computer and router use {local['network']}. Devices only talk directly inside the same network, which is why the browser and "
                "ping fail. Moving the recorder onto your network's address range fixes this.",
                ["The recorder answered Hikvision SADP discovery (multicast reaches it even when its address is wrong)"] + detail, steps))
        elif o.device_gateway_alive:
            findings.append(Finding(
                "subnet_mismatch", "ruled_out", "The different address range is not what blocks it",
                f"{device_gateway} answered, so your router can reach the {device_net_text} network. The recorder itself is not answering.",
                detail[:2] + [f"{device_gateway} answered"]))
            findings.append(Finding("device_offline", "suspected", "The recorder is off, unplugged, or has a different address",
                                    "Its network is reachable but the recorder is not answering on it.", [], offline_steps))
        else:
            findings.append(Finding(
                "subnet_mismatch", "suspected", "Most likely: the recorder is set up for a different network",
                f"Its address {ip} belongs to {device_net_text}, but this computer and router use {local['network']}. Unless a router connects the two "
                "networks, the recorder cannot be reached from here. Sentinel cannot prove this from here: the recorder did not answer anything.",
                detail, steps))
            findings.append(Finding(
                "separate_network", "suspected", "Or: the recorder is connected to a different router or modem",
                f"If the recorder is plugged into another box (for example the internet provider's modem) rather than the router this computer uses"
                f"{' (' + local['gateway'] + ')' if local['gateway'] else ''}, the two networks cannot see each other.",
                [], ["Follow the network cable from the recorder's LAN port and check it goes to the same router or switch as this computer.",
                     "If it goes to another box, move it to your main router, then click Retest."]))
            findings.append(Finding("device_offline", "suspected", "Or: the recorder is off or unplugged",
                                    "A recorder that is powered off or not cabled looks exactly the same from here.", [], offline_steps[:2]))
    else:
        if device_net and ipaddress.IPv4Address(local["address"]) not in device_net:
            findings.append(Finding(
                "mask_mismatch", "confirmed" if sadp_hit else "suspected", "The recorder's subnet mask does not match your network",
                f"With mask {mask}, the recorder's network is {device_net}, which does not include this computer ({local['address']}), "
                "so its replies go to its gateway instead of coming back here.",
                [f"Recorder: {ip} / {mask}", f"This computer: {local['address']} / {local['mask']}"],
                [f"In the recorder's network settings ({guidance.brand(brand)['network_menu']}), set the subnet mask to {local['mask']} "
                 f"and the gateway to {local['gateway']}, or turn on DHCP.", "Click Retest."]))
        explanation = "Recorders set to DHCP can get a new address after a power cut or a router change."
        if o.ping.kind == "host_unreachable" and o.ping.from_ip == route.source_ip:
            explanation = f"No device on {local['network']} is using {ip} right now. " + explanation
        findings.append(Finding("device_offline", "suspected", "The recorder is off, unplugged, or now has a different address", explanation, [], offline_steps))
        if adapter.kind == "wifi":
            findings.append(Finding(
                "wifi_isolation", "suspected", "Or: your Wi-Fi keeps wireless devices apart from wired ones",
                f"This computer is on Wi-Fi ('{adapter.name}'). Guest networks and 'AP/client isolation' stop Wi-Fi devices from reaching wired ones like a recorder.",
                [f"Connected over Wi-Fi: '{adapter.name}'"],
                ["Make sure this computer is on your main Wi-Fi network, not a guest network.",
                 "Or connect it with a network cable to the same router as the recorder, then click Retest."]))

    for device in o.discovered:
        if device["host"] == ip or not any(ipaddress.IPv4Address(device["host"]) in i.network for _, i in networks):
            continue
        if brand and device.get("brand") != brand:
            continue  # a known brand: other makes' cameras are not this recorder
        what = " ".join(x for x in (guidance.brand(device.get("brand"))["label"] if device.get("brand") else "Camera or recorder", device.get("model") or "") if x)
        findings.insert(1, Finding(  # right after "unreachable": the most useful lead
            "found_elsewhere", "suspected", f"{what} answered at {device['host']}",
            "This may be your recorder with a new address, already on your network.",
            [f"Found by {'Hikvision SADP' if device.get('method') == 'sadp' else 'ONVIF'} discovery on your network"],
            [f"Click 'Check {device['host']}' to test that address."], data={"host": device["host"]}))

    for vpn in (a for a in snapshot.adapters if a.up and a.kind == "vpn" and a is not adapter):
        findings.append(Finding(
            "vpn_interference", "suspected", f"Or: the VPN '{vpn.name}' blocks local devices",
            f"The route to {ip} does not use the VPN, but some VPN apps block local network access with their own firewall ('kill switch').",
            [f"{vpn.name} is connected"], guidance.vpn_steps(vpn.name)))

    if o.discovery_ran and not o.discovered and local.get("profile") == "Public":
        findings.append(Finding(
            "discovery_blocked", "suspected", "Automatic discovery may be blocked by Windows",
            f"Windows treats '{local['adapter']}' as a Public network, and its firewall usually drops the replies discovery depends on. "
            "So 'nothing found' says little about whether the recorder is there.",
            [f"'{local['adapter']}' network profile: Public", "Discovery found no devices at all"],
            guidance.network_profile_steps(platform)))
    return sort_findings(findings)


def _alive(backend: NetworkBackend, host: str) -> bool:
    if backend.ping(host).kind == "reply":
        return True
    return any(backend.tcp_connect(host, port, TCP_TIMEOUT_S) in ("open", "refused") for port in (80, 443))


def _mask(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.IPv4Network(f"0.0.0.0/{value.strip()}").netmask)
    except ValueError:
        raise TargetRefused(f"'{value}' is not a subnet mask (for example 255.255.255.0).") from None


def troubleshoot(backend: NetworkBackend, host: str, *, brand: str | None = None, device_mask: str | None = None,
                 device_gateway: str | None = None, rtsp_port: int = 554, http_port: int = 80, discover: bool = True) -> dict[str, Any]:
    snapshot = backend.snapshot()
    ip = check_target(host)
    mask = _mask(device_mask)
    gateway = check_target(device_gateway) if device_gateway else None
    route = route_for(snapshot, ip)
    ports = sorted({80, 443, 554, 8000, 37777, 9000, int(rtsp_port), int(http_port)})
    with ThreadPoolExecutor(max_workers=12) as pool:
        ping_job = pool.submit(backend.ping, ip)
        port_jobs = {port: pool.submit(backend.tcp_connect, ip, port, TCP_TIMEOUT_S) for port in ports}
        sadp_job = pool.submit(backend.discover_sadp, snapshot) if discover and route else None
        onvif_job = pool.submit(backend.discover_onvif, snapshot) if discover and route else None
        ping = ping_job.result()
        states = {port: job.result() for port, job in port_jobs.items()}
        reachable = ping.kind == "reply" or any(s in ("open", "refused") for s in states.values())
        local_alive = device_alive = None
        if not reachable and route is not None:
            if route.next_hop:
                local_alive = _alive(backend, route.next_hop)
            if gateway and gateway != route.next_hop:
                device_alive = _alive(backend, gateway)
        discovered = []
        for job in (sadp_job, onvif_job):
            try:
                discovered += job.result() if job else []
            except OSError:
                pass
    http = backend.http_identify(ip, http_port) if states.get(http_port) == "open" else None
    sadp_hit = next((d for d in discovered if d["host"] == ip and d.get("method") == "sadp"), None)
    identity = identify(states, http, sadp_hit) if reachable or sadp_hit else None
    observation = Observation(snapshot, ip, route, ping, states, brand, mask, gateway, local_alive, device_alive,
                              bool(sadp_job or onvif_job), discovered, identity, int(rtsp_port), int(http_port))
    findings = analyze_target(observation)
    headline = next((f for f in findings if f.status in ("confirmed", "suspected")), findings[0] if findings else None)
    return {
        "host": ip,
        "reachable": reachable,
        "summary": ("Reachable. Next: test the login and streams." if reachable and not any(f.status == "confirmed" for f in findings)
                    else headline.title if headline else ""),
        "route": {"adapter": route.adapter.name, "kind": route.adapter.kind, "source_ip": route.source_ip, "via": route.next_hop} if route else None,
        "checks": [{"id": "ping", "label": "Ping", "result": ping.kind}]
                  + [{"id": f"tcp_{p}", "label": f"Port {p} ({PORT_ROLES.get(p, 'TCP')})", "result": s} for p, s in sorted(states.items())]
                  + ([{"id": "local_gateway", "label": f"Your router {route.next_hop}", "result": "reply" if local_alive else "no answer"}] if local_alive is not None else [])
                  + ([{"id": "device_gateway", "label": f"Recorder's gateway {gateway}", "result": "reply" if device_alive else "no answer"}] if device_alive is not None else []),
        "identity": identity,
        "discovered": [_public_device(d, snapshot) for d in discovered],
        "findings": [f.as_dict() for f in findings],
    }


def _public_device(device: dict[str, Any], snapshot: NetworkSnapshot) -> dict[str, Any]:
    keys = ("host", "method", "brand", "model", "name", "mask", "gateway", "dhcp", "activated", "channels", "xaddr", "ports", "found_by")
    view = {key: device[key] for key in keys if device.get(key) is not None}
    address = ipaddress.IPv4Address(device["host"])
    view["on_local_network"] = any(address in i.network for _, i in snapshot.local_networks())
    if device.get("mask"):
        try:
            view["network"] = str(ipaddress.IPv4Network(f"{device['host']}/{device['mask']}", strict=False))
        except ValueError:
            pass
    return view


def discover_devices(backend: NetworkBackend, *, onvif: bool = True, sadp: bool = True, port_check: bool = False) -> dict[str, Any]:
    """User-approved search of this computer's own networks. Multicast discovery
    stays on the local segment; the optional port check covers at most the /24
    around each local address, on a handful of camera ports."""
    snapshot = backend.snapshot()
    found: dict[str, dict[str, Any]] = {}

    def merge(device: dict[str, Any]) -> None:
        entry = found.setdefault(device["host"], {"host": device["host"], "found_by": []})
        entry["found_by"].append(device.get("method", "ports"))
        for key, value in device.items():
            if value is not None and key not in ("method", "found_by"):
                entry.setdefault(key, value)

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(backend.discover_sadp, snapshot)] if sadp else []
        jobs += [pool.submit(backend.discover_onvif, snapshot)] if onvif else []
        for job in jobs:
            try:
                for device in job.result():
                    merge(device)
            except OSError:
                continue
    searched = []
    if port_check:
        own = {str(i.ip) for _, i in snapshot.local_networks(include_vpn=True)}
        targets = [(str(host), port) for network in sweep_networks(snapshot) for host in network.hosts() if str(host) not in own for port in SWEEP_PORTS]
        searched = [str(network) for network in sweep_networks(snapshot)]
        with ThreadPoolExecutor(max_workers=128) as pool:
            states = list(pool.map(lambda pair: backend.tcp_connect(pair[0], pair[1], 0.6), targets))
        open_ports: dict[str, dict[int, str]] = {}
        for (host, port), state in zip(targets, states):
            if state == "open":
                open_ports.setdefault(host, {})[port] = state
        for host, ports in open_ports.items():
            guess = identify(ports, None, None)
            merge({"host": host, "method": "ports", "ports": sorted(ports), "brand": guess["brand"] if guess else None})
    for entry in found.values():
        entry["found_by"] = sorted(set(entry["found_by"]))
    devices = sorted((_public_device(d, snapshot) for d in found.values()), key=lambda d: ipaddress.IPv4Address(d["host"]))
    notes = [f.as_dict() for f in analyze_network(snapshot) if f.code == "public_profile"] if not devices else []
    return {"devices": devices, "networks_searched": searched, "methods": [m for m, on in (("sadp", sadp), ("onvif", onvif), ("ports", port_check)) if on], "notes": notes}
