"""Network diagnostics: OS parsers, target policy, and diagnosis under simulated
network conditions, including the Hikvision ERI-K108-P8 regression case (PC on
10.0.0.0/24, recorder set to 192.168.1.84 with gateway 192.168.1.254)."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from sentinel.netdiag import SimulatedNetwork, analyze_network, check_target, discover_devices, troubleshoot
from sentinel.netdiag.collect import parse_linux, parse_windows
from sentinel.netdiag.model import TargetRefused, classify_adapter, route_for, sweep_networks
from sentinel.netdiag.probes import identify, parse_ping_output
from sentinel.netdiag.sadp import parse_reply
from sentinel.onboarding import scan_channels

SCENARIO = json.loads(Path("config/netsim/hikvision_wrong_subnet.json").read_text(encoding="utf-8"))
RECORDER = "192.168.1.84"


def network(**overrides):
    scenario = copy.deepcopy(SCENARIO)
    scenario.update(overrides)
    return SimulatedNetwork(scenario)


def regression(sim, **kwargs):
    return troubleshoot(sim, RECORDER, brand="hikvision", device_mask="255.255.255.0", device_gateway="192.168.1.254", **kwargs)


def by_code(report):
    return {f["code"]: f for f in report["findings"]}


# ---- reading the computer's network ----------------------------------------------------


WINDOWS_PAYLOAD = {
    "adapters": [
        {"Name": "Wi-Fi", "InterfaceDescription": "Intel(R) Wi-Fi 6 AX201 160MHz", "ifIndex": 4, "Status": "Up"},
        {"Name": "vEthernet (WSL)", "InterfaceDescription": "Hyper-V Virtual Ethernet Adapter", "ifIndex": 41, "Status": "Up"},
        {"Name": "NordLynx", "InterfaceDescription": "NordLynx Tunnel", "ifIndex": 30, "Status": "Up"},
        {"Name": "Ethernet", "InterfaceDescription": "Realtek PCIe GbE Family Controller", "ifIndex": 7, "Status": "Disconnected"},
    ],
    "interfaces": [
        {"ifIndex": 4, "InterfaceAlias": "Wi-Fi", "InterfaceMetric": 45, "Dhcp": "Enabled", "ConnectionState": "Connected"},
        {"ifIndex": 41, "InterfaceAlias": "vEthernet (WSL)", "InterfaceMetric": 5000, "Dhcp": "Disabled", "ConnectionState": "Connected"},
        {"ifIndex": 30, "InterfaceAlias": "NordLynx", "InterfaceMetric": 1, "Dhcp": "Disabled", "ConnectionState": "Connected"},
        {"ifIndex": 7, "InterfaceAlias": "Ethernet", "InterfaceMetric": 25, "Dhcp": "Enabled", "ConnectionState": "Disconnected"},
        {"ifIndex": 9, "InterfaceAlias": "Local Area Connection* 1", "InterfaceMetric": 25, "Dhcp": "Enabled", "ConnectionState": "Disconnected"},
        {"ifIndex": 1, "InterfaceAlias": "Loopback Pseudo-Interface 1", "InterfaceMetric": 75, "Dhcp": "Disabled", "ConnectionState": "Connected"},
    ],
    "addresses": [
        {"ifIndex": 4, "IPAddress": "10.0.0.57", "PrefixLength": 24},
        {"ifIndex": 41, "IPAddress": "172.20.160.1", "PrefixLength": 20},
        {"ifIndex": 30, "IPAddress": "10.5.0.2", "PrefixLength": 32},
        {"ifIndex": 9, "IPAddress": "169.254.52.221", "PrefixLength": 16},
        {"ifIndex": 1, "IPAddress": "127.0.0.1", "PrefixLength": 8},
    ],
    "routes": [
        {"ifIndex": 4, "DestinationPrefix": "0.0.0.0/0", "NextHop": "10.0.0.1", "RouteMetric": 0},
        {"ifIndex": 4, "DestinationPrefix": "10.0.0.0/24", "NextHop": "0.0.0.0", "RouteMetric": 256},
        {"ifIndex": 30, "DestinationPrefix": "0.0.0.0/0", "NextHop": "0.0.0.0", "RouteMetric": 0},
        {"ifIndex": 41, "DestinationPrefix": "172.20.160.0/20", "NextHop": "0.0.0.0", "RouteMetric": 256},
    ],
    "profiles": {"InterfaceIndex": 4, "NetworkCategory": "Public"},  # ConvertTo-Json: one item becomes an object
    "firewall": [{"Name": "Public", "Enabled": "True", "DefaultInboundAction": "NotConfigured"}],
    "dns": [{"InterfaceIndex": 4, "ServerAddresses": ["10.0.0.1"]}, {"InterfaceIndex": 7, "ServerAddresses": None}],
}


class CollectorTests(unittest.TestCase):
    def test_windows_payload(self):
        snapshot = parse_windows(WINDOWS_PAYLOAD)
        adapters = {a.name: a for a in snapshot.adapters}
        wifi = adapters["Wi-Fi"]
        self.assertEqual((wifi.kind, wifi.up, wifi.ipv4, wifi.gateway, wifi.dhcp, wifi.profile, wifi.dns),
                         ("wifi", True, ["10.0.0.57/24"], "10.0.0.1", True, "Public", ["10.0.0.1"]))
        self.assertEqual(adapters["NordLynx"].kind, "vpn")
        self.assertEqual(adapters["vEthernet (WSL)"].kind, "virtual")
        self.assertEqual(adapters["Local Area Connection* 1"].kind, "virtual", "hidden adapters are not camera networks")
        self.assertEqual(adapters["Loopback Pseudo-Interface 1"].kind, "loopback")
        self.assertFalse(adapters["Ethernet"].up)
        self.assertEqual(snapshot.firewall["Public"], {"enabled": True, "default_inbound": "NotConfigured"})
        # The VPN's default route has the lower metric, so it wins for anything off the local subnet.
        self.assertEqual(route_for(snapshot, RECORDER).adapter.name, "NordLynx")
        self.assertEqual(route_for(snapshot, "10.0.0.80").adapter.name, "Wi-Fi")
        self.assertTrue(route_for(snapshot, "10.0.0.80").on_link)
        self.assertNotIn("mac", json.dumps(snapshot.public_view()).lower())

    def test_linux_ip_json(self):
        links = [
            {"ifname": "lo", "flags": ["LOOPBACK", "UP"], "operstate": "UNKNOWN", "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}]},
            {"ifname": "eth0", "flags": ["UP"], "operstate": "UP", "addr_info": [{"family": "inet", "local": "192.168.1.20", "prefixlen": 24, "dynamic": True}]},
            {"ifname": "wg0", "flags": ["UP"], "operstate": "UNKNOWN", "addr_info": [{"family": "inet", "local": "10.8.0.2", "prefixlen": 24}]},
            {"ifname": "docker0", "flags": ["UP"], "operstate": "DOWN", "addr_info": [{"family": "inet", "local": "172.17.0.1", "prefixlen": 16}]},
        ]
        routes = [{"dst": "default", "gateway": "192.168.1.1", "dev": "eth0", "metric": 100},
                  {"dst": "192.168.1.0/24", "dev": "eth0"}, {"dst": "10.8.0.0/24", "dev": "wg0"}]
        snapshot = parse_linux(links, routes)
        adapters = {a.name: a for a in snapshot.adapters}
        self.assertEqual((adapters["eth0"].kind, adapters["eth0"].gateway, adapters["eth0"].dhcp), ("ethernet", "192.168.1.1", True))
        self.assertEqual((adapters["wg0"].kind, adapters["docker0"].kind, adapters["docker0"].up), ("vpn", "virtual", False))
        self.assertEqual(route_for(snapshot, "192.168.1.84").adapter.name, "eth0")
        self.assertEqual(route_for(snapshot, "10.8.0.9").adapter.name, "wg0")

    def test_adapter_classification(self):
        cases = {("Ethernet 2", "TAP-Windows Adapter V9"): "vpn", ("Tailscale", "Tailscale Tunnel"): "vpn",
                 ("Ethernet", "Intel(R) Ethernet Connection I219-V"): "ethernet", ("wlp2s0", ""): "wifi",
                 ("VirtualBox Host-Only Network", "VirtualBox Host-Only Ethernet Adapter"): "virtual"}
        for (name, description), kind in cases.items():
            self.assertEqual(classify_adapter(name, description), kind, name)


class PolicyTests(unittest.TestCase):
    def test_only_local_addresses_are_probed(self):
        for host in ("192.168.1.84", "10.0.0.5", "172.16.4.2", "100.64.1.1", "127.0.0.1", "169.254.1.2"):
            self.assertEqual(check_target(host), host)
        for host in ("8.8.8.8", "224.0.0.251", "0.0.0.0", "255.255.255.255", "bad host!", "", "fe80::1"):
            with self.assertRaises(TargetRefused, msg=host):
                check_target(host)
        self.assertEqual(check_target("nvr.local", resolve=lambda name: "192.168.1.84"), "192.168.1.84")
        with self.assertRaises(TargetRefused):
            check_target("example.com", resolve=lambda name: "93.184.216.34")

    def test_port_check_covers_own_networks_only_and_at_most_a_slash_24(self):
        sim = network()
        sim.update_adapter("Wi-Fi", ipv4=["172.16.5.10/16"])
        self.assertEqual([str(n) for n in sweep_networks(sim.snapshot())], ["172.16.5.0/24"])


class ParserTests(unittest.TestCase):
    def test_sadp_reply_keeps_settings_and_drops_identifiers(self):
        payload = b"""<?xml version="1.0" encoding="UTF-8"?><ProbeMatch><Uuid>X</Uuid><Types>inquiry</Types>
            <DeviceType>12345</DeviceType><DeviceDescription>ERI-K108-P8</DeviceDescription><DeviceSN>ERI-K108-P80820231234CCRR</DeviceSN>
            <CommandPort>8000</CommandPort><HttpPort>80</HttpPort><MAC>44-19-b6-00-00-01</MAC><IPv4Address>192.168.1.84</IPv4Address>
            <IPv4SubnetMask>255.255.255.0</IPv4SubnetMask><IPv4Gateway>192.168.1.254</IPv4Gateway><DHCP>false</DHCP>
            <AnalogChannelNum>0</AnalogChannelNum><DigitalChannelNum>8</DigitalChannelNum><SoftwareVersion>V4.30.085</SoftwareVersion>
            <Activated>true</Activated></ProbeMatch>"""
        device = parse_reply(payload)
        self.assertEqual((device["host"], device["model"], device["mask"], device["gateway"], device["dhcp"], device["channels"]),
                         ("192.168.1.84", "ERI-K108-P8", "255.255.255.0", "192.168.1.254", False, 8))
        self.assertNotIn("CCRR", json.dumps(device))
        self.assertNotIn("44-19", json.dumps(device))
        self.assertIsNone(parse_reply(b'<Probe><Types>inquiry</Types></Probe>'), "our own multicast probe is ignored")
        self.assertIsNone(parse_reply(b"not xml"))

    def test_ping_output(self):
        self.assertEqual(parse_ping_output("64 bytes from 10.0.0.1: icmp_seq=1 ttl=64 time=3.2 ms").kind, "reply")
        unreachable = parse_ping_output("From 10.0.0.57 icmp_seq=1 Destination Host Unreachable")
        self.assertEqual((unreachable.kind, unreachable.from_ip), ("host_unreachable", "10.0.0.57"))
        self.assertEqual(parse_ping_output("1 packets transmitted, 0 received, 100% packet loss").kind, "timeout")

    def test_brand_identification_is_only_likely_without_sadp(self):
        guess = identify({80: "open", 8000: "open"}, {"server": "App-webs/", "markers": ["doc/page/login.asp"]}, None)
        self.assertEqual((guess["brand"], guess["confidence"], len(guess["evidence"])), ("hikvision", "likely", 3))
        self.assertEqual(identify({37777: "open"}, None, None)["brand"], "dahua")
        self.assertIsNone(identify({80: "open"}, {"server": "nginx", "markers": []}, None))


# ---- diagnosis under simulated conditions -------------------------------------------------


class RegressionScenarioTests(unittest.TestCase):
    """The real case: browser and ping to 192.168.1.84 fail from 10.0.0.57."""

    def test_recorder_on_same_cable_with_stale_settings_is_confirmed_by_sadp(self):
        report = regression(network())
        findings = by_code(report)
        self.assertFalse(report["reachable"])
        self.assertEqual(findings["unreachable"]["status"], "confirmed")
        self.assertEqual(findings["subnet_mismatch"]["status"], "confirmed")
        evidence = " ".join(findings["subnet_mismatch"]["evidence"])
        for fact in ("SADP", "10.0.0.0/24", "192.168.1.0/24", "192.168.1.254", "10.0.0.1"):
            self.assertIn(fact, evidence)
        steps = " ".join(findings["subnet_mismatch"]["steps"])
        self.assertIn("DHCP", steps)
        self.assertIn("gateway 10.0.0.1", steps)
        self.assertIn("Configuration → Network", steps, "Hikvision menu path")
        self.assertIn("Sentinel never makes this change", steps)
        self.assertIn({"id": "device_gateway", "label": "Recorder's gateway 192.168.1.254", "result": "no answer"}, report["checks"])
        self.assertIn("proves little", " ".join(findings["unreachable"]["evidence"]), "a failed ping is not treated as proof")
        self.assertEqual(report["findings"][0]["status"], "confirmed")

    def test_without_discovery_the_mismatch_is_only_suspected_and_alternatives_are_listed(self):
        sim = network()
        sim.scenario["firewall"]["Public"]["blocks_discovery"] = True
        findings = by_code(regression(sim))
        self.assertEqual(findings["subnet_mismatch"]["status"], "suspected")
        self.assertEqual(findings["separate_network"]["status"], "suspected")
        self.assertEqual(findings["device_offline"]["status"], "suspected")
        self.assertEqual(findings["discovery_blocked"]["status"], "suspected")
        self.assertIn("Private", " ".join(findings["discovery_blocked"]["steps"]))
        self.assertNotIn("confirmed", {f["status"] for code, f in findings.items() if code != "unreachable"})

    def test_recorder_behind_the_providers_modem(self):
        """Double NAT: the recorder's network exists upstream of the PC's router, so the
        address range is not the cause; a powered-off recorder is."""
        sim = network()
        sim.scenario["routers"][0]["interfaces"].append({"ip": "192.168.1.65", "mask": "255.255.255.0", "segment": "isp"})
        sim.scenario["routers"].append({"name": "modem", "icmp": True, "ports": {"80": "open"},
                                        "interfaces": [{"ip": "192.168.1.254", "mask": "255.255.255.0", "segment": "isp"}]})
        sim.update_device(RECORDER, segment="isp", powered=False)
        findings = by_code(regression(sim))
        self.assertEqual(findings["subnet_mismatch"]["status"], "ruled_out")
        self.assertEqual(findings["device_offline"]["status"], "suspected")
        sim.update_device(RECORDER, powered=True)
        report = regression(sim)
        self.assertTrue(report["reachable"])
        self.assertEqual(by_code(report)["routed"]["status"], "info")

    def test_after_the_recorder_is_readdressed_retest_finds_it(self):
        sim = network()
        sim.update_device(RECORDER, ip="10.0.0.84", gateway="10.0.0.1", dhcp=True)
        stale = regression(sim)
        self.assertEqual(stale["findings"][1]["code"], "found_elsewhere")
        self.assertEqual(stale["findings"][1]["data"], {"host": "10.0.0.84"})
        fixed = troubleshoot(sim, "10.0.0.84", brand="hikvision")
        findings = by_code(fixed)
        self.assertTrue(fixed["reachable"])
        self.assertEqual(fixed["summary"], "Reachable. Next: test the login and streams.")
        self.assertEqual((findings["rtsp_open"]["status"], findings["web_ok"]["status"]), ("ok", "ok"))
        self.assertEqual(fixed["identity"]["model"], "ERI-K108-P8")
        self.assertFalse([f for f in fixed["findings"] if f["status"] in ("confirmed", "suspected")])

    def test_only_the_target_and_gateways_are_probed(self):
        sim = network()
        regression(sim)
        probed = {host for _, host, _ in sim.log if host}
        self.assertEqual(probed, {RECORDER, "10.0.0.1", "192.168.1.254"})


class ConditionTests(unittest.TestCase):
    def test_vpn_capturing_local_traffic(self):
        sim = network()
        sim.scenario["adapters"].append({"name": "NordLynx", "description": "NordLynx Tunnel", "kind": "vpn", "up": True,
                                         "ipv4": ["10.5.0.2/32"], "gateway": "10.5.0.1", "metric": 1, "segment": "vpn"})
        findings = by_code(regression(sim))
        self.assertEqual(findings["vpn_route"]["status"], "confirmed")
        self.assertIn("Allow LAN access", " ".join(findings["vpn_route"]["steps"]))
        network_findings = {f.code: f for f in analyze_network(sim.snapshot())}
        self.assertEqual(network_findings["vpn_active"].status, "suspected")

    def test_ping_blocked_is_ruled_out_when_ports_answer(self):
        findings = by_code(troubleshoot(network(), "10.0.0.30"))
        self.assertEqual(findings["reachable"]["status"], "ok")
        self.assertEqual(findings["ping_blocked"]["status"], "ruled_out")
        self.assertEqual(findings["identified"]["data"]["brand"], "reolink")

    def test_rtsp_turned_off(self):
        sim = network()
        sim.device("10.0.0.30")["ports"]["554"] = "refused"
        findings = by_code(troubleshoot(sim, "10.0.0.30", brand="reolink"))
        self.assertEqual(findings["rtsp_closed"]["status"], "confirmed")
        self.assertIn("turned off", " ".join(findings["rtsp_closed"]["steps"]))
        sim.device("10.0.0.30")["ports"]["554"] = "filtered"
        self.assertEqual(by_code(troubleshoot(sim, "10.0.0.30"))["rtsp_filtered"]["status"], "suspected")

    def test_nothing_at_an_address_on_the_local_network(self):
        sim = network()
        sim.scenario["firewall"]["Public"]["blocks_discovery"] = True
        report = troubleshoot(sim, "10.0.0.99")
        findings = by_code(report)
        self.assertIn("from this computer itself", " ".join(findings["unreachable"]["evidence"]))
        self.assertEqual(findings["device_offline"]["status"], "suspected")
        self.assertEqual(findings["wifi_isolation"]["status"], "suspected")
        self.assertNotIn("subnet_mismatch", findings)

    def test_mask_mismatch_on_the_same_range(self):
        sim = network()
        sim.update_device(RECORDER, ip="10.0.0.84", mask="255.255.255.252", gateway="192.168.1.254")
        findings = by_code(troubleshoot(sim, "10.0.0.84", brand="hikvision"))
        self.assertEqual(findings["mask_mismatch"]["status"], "confirmed")

    def test_disconnected_computer(self):
        sim = network()
        sim.update_adapter("Wi-Fi", up=False)
        self.assertEqual(analyze_network(sim.snapshot())[0].code, "no_connection")
        self.assertEqual(by_code(regression(sim))["no_route"]["status"], "confirmed")

    def test_dhcp_failure(self):
        sim = network()
        sim.update_adapter("Wi-Fi", ipv4=["169.254.12.7/16"], gateway=None)
        finding = analyze_network(sim.snapshot())[0]
        self.assertEqual((finding.code, finding.status), ("dhcp_failed", "confirmed"))

    def test_public_profile_is_explained_on_the_network_step(self):
        findings = {f.code: f for f in analyze_network(network().snapshot())}
        self.assertEqual(findings["connected"].status, "ok")
        self.assertEqual(findings["public_profile"].status, "info")
        self.assertIn("10.0.0.57", findings["connected"].explanation)

    def test_bad_inputs_are_refused(self):
        for kwargs in ({"device_mask": "255.0.255.0"}, {"device_gateway": "8.8.4.4"}):
            with self.assertRaises(TargetRefused):
                troubleshoot(network(), RECORDER, **kwargs)
        with self.assertRaises(TargetRefused):
            troubleshoot(network(), "1.1.1.1")


class DiscoveryTests(unittest.TestCase):
    def test_discovery_reports_wrong_network_and_port_check_stays_local(self):
        sim = network()
        result = discover_devices(sim, port_check=True)
        devices = {d["host"]: d for d in result["devices"]}
        self.assertEqual(devices[RECORDER]["network"], "192.168.1.0/24")
        self.assertFalse(devices[RECORDER]["on_local_network"])
        self.assertEqual(devices["10.0.0.30"]["found_by"], ["onvif", "ports"])
        self.assertEqual(result["networks_searched"], ["10.0.0.0/24"])
        probed = {host for kind, host, _ in sim.log if kind == "tcp"}
        self.assertTrue(all(host.startswith("10.0.0.") for host in probed))
        self.assertLessEqual(len(probed), 253)

    def test_blocked_discovery_explains_the_public_profile(self):
        sim = network()
        sim.scenario["firewall"]["Public"]["blocks_discovery"] = True
        result = discover_devices(sim)
        self.assertEqual(result["devices"], [])
        self.assertEqual(result["notes"][0]["code"], "public_profile")


class ChannelScanTests(unittest.TestCase):
    def setUp(self):
        self.sim = network()
        self.sim.update_device(RECORDER, ip="10.0.0.84", gateway="10.0.0.1")
        self.calls = []

    def probe(self, url):
        self.calls.append(url)
        return self.sim.rtsp_probe(url)

    def source(self, password):
        return {"type": "rtsp", "host": "10.0.0.84", "port": 554, "template": "hikvision", "username": "admin", "credential": "c"}, {"c": password}.get

    def test_finds_the_recorders_eight_channels(self):
        source, secrets = self.source("simulated-only")
        result = scan_channels(source, secrets, probe=self.probe, max_channels=16)
        found = [c for c in result["channels"] if c["main"]["kind"] == "ok"]
        self.assertEqual([c["channel"] for c in found], list(range(1, 9)))
        self.assertEqual((found[0]["main"]["codec"], found[0]["sub"]["codec"]), ("H265", "H264"))

    def test_wrong_password_is_tried_once(self):
        source, secrets = self.source("wrong")
        result = scan_channels(source, secrets, probe=self.probe)
        self.assertEqual((result["kind"], result["channels"], len(self.calls)), ("auth_failed", [], 1))


try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class NetworkApiTests(unittest.TestCase):
    def setUp(self):
        from sentinel.api import create_app
        from sentinel.config import load_config
        from sentinel.service import SentinelService

        self.directory = tempfile.TemporaryDirectory()
        config = load_config("config/sentinel.example.json", resolve_secrets=False)
        config["storage"] = {"dir": self.directory.name}
        config["drone"]["enabled"] = False
        self.service = SentinelService(config, start_cameras=False)
        self.service.network = network()
        self.client = TestClient(create_app(self.service, api_token="t"))
        self.client.headers.update({"Authorization": "Bearer t", "X-Sentinel-Operator": "installer"})

    def tearDown(self):
        self.service.stop()
        self.directory.cleanup()

    def test_overview_diagnosis_and_refusals(self):
        overview = self.client.get("/api/v1/onboarding/network").json()
        self.assertEqual(overview["network"]["adapters"][0]["ipv4"], ["10.0.0.57/24"])
        self.assertIn("hikvision", [b["id"] for b in overview["brands"]])
        report = self.client.post("/api/v1/onboarding/network/diagnose", json={"host": RECORDER, "brand": "hikvision"}).json()
        self.assertEqual(by_code(report)["subnet_mismatch"]["status"], "confirmed")
        self.assertEqual(self.client.post("/api/v1/onboarding/network/diagnose", json={"host": "8.8.8.8"}).status_code, 422)
        self.assertEqual(self.client.post("/api/v1/onboarding/network/discover", json={"port_check": True}).status_code, 422)
        found = self.client.post("/api/v1/onboarding/network/discover", json={"confirm": True}).json()
        self.assertIn(RECORDER, [d["host"] for d in found["devices"]])
        audit = [row["action"] for row in self.client.get("/api/v1/audit").json()]
        self.assertIn("network_diagnosis", audit)
        self.assertIn("network_discovery", audit)

    def test_channel_scan_adds_cameras_without_leaking_the_password(self):
        self.service.network.update_device(RECORDER, ip="10.0.0.84", gateway="10.0.0.1")
        source = {"type": "rtsp", "host": "10.0.0.84", "port": 554, "template": "hikvision", "username": "admin", "password": "simulated-only"}
        result = self.client.post("/api/v1/onboarding/channels", json={"source": source, "max_channels": 10}).json()
        drafts = [c["draft_id"] for c in result["channels"] if c.get("draft_id")]
        self.assertEqual(len(drafts), 8)
        camera_id = self.client.post("/api/v1/config/cameras", json={"name": "Porch", "draft_id": drafts[2]}).json()["id"]
        camera = next(c for c in self.service.site.config["cameras"] if c["id"] == camera_id)
        self.assertEqual((camera["source"]["channel"], camera["source"]["template"]), (3, "hikvision"))
        self.assertEqual(self.service.runtime.secrets.get(camera["source"]["credential"]), "simulated-only")
        everything = json.dumps([result, self.client.get("/api/v1/config").json(), self.client.get("/api/v1/audit").json()])
        self.assertNotIn("simulated-only", everything)
        self.assertNotIn("simulated-only", (Path(self.directory.name) / "site.json").read_text())
        bad = self.client.post("/api/v1/onboarding/channels", json={"source": dict(source, password="nope")}).json()
        self.assertEqual(bad["kind"], "auth_failed")
        public = self.client.post("/api/v1/onboarding/channels", json={"source": dict(source, host="8.8.8.8")})
        self.assertEqual(public.status_code, 422)


if __name__ == "__main__":
    unittest.main()
