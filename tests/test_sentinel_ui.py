"""Browser tests of the Setup → Cameras troubleshooter, in Microsoft Edge via
Playwright, against a live server whose network diagnostics answer from the
simulated regression network (config/netsim/hikvision_wrong_subnet.json).

Skipped unless `playwright` (pip) and Edge or Chromium are available. Run alone:
    python -m unittest tests.test_sentinel_ui"""

import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

try:
    import uvicorn
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from sentinel.config import load_config
from sentinel.netdiag import SimulatedNetwork

SCENARIO = Path("config/netsim/hikvision_wrong_subnet.json")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipIf(sync_playwright is None, "playwright/uvicorn not installed")
class TroubleshooterUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sentinel.api import create_app
        from sentinel.service import SentinelService

        cls.directory = tempfile.TemporaryDirectory()
        config = load_config("config/sentinel.example.json", resolve_secrets=False)
        config["storage"] = {"dir": cls.directory.name}
        config["drone"]["enabled"] = False
        cls.service = SentinelService(config, start_cameras=False)
        cls.service.start()
        cls.port = free_port()
        cls.server = uvicorn.Server(uvicorn.Config(create_app(cls.service, api_token=None), host="127.0.0.1", port=cls.port, log_level="warning"))
        threading.Thread(target=cls.server.run, daemon=True).start()
        deadline = time.time() + 10
        while not cls.server.started and time.time() < deadline:
            time.sleep(0.05)
        cls.playwright = sync_playwright().start()
        cls.browser = None
        for options in ({"channel": "msedge"}, {}):
            try:
                cls.browser = cls.playwright.chromium.launch(headless=True, **options)
                break
            except PlaywrightError:
                continue
        if cls.browser is None:
            cls.tearDownClass()
            raise unittest.SkipTest("no Edge or Chromium browser available to Playwright")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "browser", None):
            cls.browser.close()
        cls.playwright.stop()
        cls.server.should_exit = True
        cls.service.stop()
        cls.directory.cleanup()

    def setUp(self):
        self.service.network = SimulatedNetwork.from_file(SCENARIO)
        self.page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        self.page.goto(f"http://127.0.0.1:{self.port}/")
        self.page.click('nav.tabs [data-tab="setup"]')

    def tearDown(self):
        self.page.close()

    def finding(self, code):
        return self.page.locator(f'#ts-findings .finding[data-code="{code}"]')

    def test_regression_recorder_on_wrong_subnet_is_found_fixed_and_added(self):
        page = self.page
        page.click("#open-troubleshooter")
        # 1. This computer's network
        expect(page.locator("#ts-adapters")).to_contain_text("10.0.0.57/24", timeout=15000)
        expect(page.locator("#ts-adapters")).not_to_contain_text("vEthernet")
        expect(page.locator("#ts-network-findings")).to_contain_text("Windows treats this network as Public")
        # 2. Discovery needs explicit permission
        page.click('[data-ts-step="find"]')
        expect(page.locator("#ts-search")).to_be_disabled()
        page.check('[data-discover="confirm"]')
        page.click("#ts-search")
        row = page.locator("#ts-devices tr", has_text="192.168.1.84")
        expect(row).to_contain_text("different network 192.168.1.0/24")
        expect(row).to_contain_text("ERI-K108-P8")
        # 3. Diagnose: the subnet mismatch is confirmed, with evidence and fix steps
        row.get_by_role("button", name="Check").click()
        expect(page.locator("#ts-summary")).to_contain_text("cannot be reached")
        mismatch = self.finding("subnet_mismatch")
        expect(mismatch).to_contain_text("Confirmed problem")
        expect(mismatch).to_contain_text("192.168.1.254")
        expect(mismatch).to_contain_text("Turn on DHCP")
        expect(self.finding("unreachable")).to_contain_text("proves little")
        # The person changes the recorder to DHCP on its own screen; it now has 10.0.0.84.
        self.service.network.update_device("192.168.1.84", ip="10.0.0.84", gateway="10.0.0.1", dhcp=True)
        page.click("#ts-diagnose")
        found = self.finding("found_elsewhere")
        expect(found).to_contain_text("10.0.0.84")
        found.get_by_role("button", name="Check 10.0.0.84").click()
        expect(page.locator("#ts-summary")).to_contain_text("Reachable")
        expect(self.finding("rtsp_open")).to_be_visible()
        expect(page.locator('#ts-body input[name="ts_host"]')).to_have_value("10.0.0.84")
        # 4. Channels: eight cameras answer; add two of them
        page.get_by_role("button", name="Next: test login and streams").click()
        expect(page.locator('#ts-body input[name="ts_l_host"]')).to_have_value("10.0.0.84")
        expect(page.locator('#ts-body select[name="ts_l_template"]')).to_have_value("hikvision")
        page.fill('#ts-body input[name="ts_l_password"]', "simulated-only")
        page.click("#ts-find-channels")
        expect(page.locator("#ts-channels")).to_contain_text("8 channel(s) answered")
        for channel in range(3, 9):
            page.uncheck(f'[data-channel-pick="{channel}"]')
        page.fill('[data-channel-name="1"]', "Front door")
        before = len(self.service.site.config["cameras"])
        page.click("#ts-add-channels")
        expect(page.locator("#toasts")).to_contain_text("2 camera(s) added")
        expect(page.locator("#setup-camera-list")).to_contain_text("Front door")
        cameras = self.service.site.config["cameras"]
        self.assertEqual(len(cameras), before + 2)
        self.assertEqual(cameras[-2]["source"]["channel"], 1)
        self.assertNotIn("simulated-only", page.content())
        self.assertNotIn("simulated-only", json.dumps(cameras))

    def test_failed_wizard_test_hands_over_to_the_troubleshooter(self):
        page = self.page
        page.click("#add-camera")
        page.click('[data-kind="dvr"]')
        page.click("#wizard-next")
        page.fill('#wizard-body input[name="host"]', "127.0.0.1")
        page.fill('#wizard-body input[name="port"]', str(free_port()))  # nothing listens: port_closed
        page.click("#wizard-next")
        page.click("#wizard-troubleshoot", timeout=15000)
        expect(page.locator("#troubleshooter")).to_be_visible()
        expect(page.locator('#ts-body input[name="ts_host"]')).to_have_value("127.0.0.1")
        expect(page.locator('#ts-body select[name="ts_brand"]')).to_have_value("hikvision")
        expect(page.locator("#ts-summary")).to_be_visible(timeout=15000)

    def test_public_addresses_are_refused_in_the_ui(self):
        page = self.page
        page.click("#open-troubleshooter")
        page.click('[data-ts-step="check"]')
        page.fill('#ts-body input[name="ts_host"]', "8.8.8.8")
        page.click("#ts-diagnose")
        expect(page.locator("#toasts")).to_contain_text("public internet address")
        self.assertFalse([host for _, host, _ in self.service.network.log if host == "8.8.8.8"])

    def test_fits_a_phone_screen(self):
        page = self.page
        page.set_viewport_size({"width": 390, "height": 844})
        page.click("#open-troubleshooter")
        expect(page.locator("#ts-adapters")).to_contain_text("10.0.0.57/24", timeout=15000)
        dialog_width = page.eval_on_selector("#troubleshooter", "d => d.scrollWidth <= d.clientWidth + 1")
        self.assertTrue(dialog_width, "troubleshooter scrolls sideways at phone width")


@unittest.skipIf(sync_playwright is None, "playwright/uvicorn not installed")
class SystemHealthUiTests(unittest.TestCase):
    """Live tiles and the System tab, against two simulated cameras where one
    has lost its connection: the dashboard must never show it as monitoring."""

    @classmethod
    def setUpClass(cls):
        from sentinel.api import create_app
        from sentinel.service import SentinelService
        from sentinel.simulation import reset_fault_clocks

        reset_fault_clocks()
        cls.directory = tempfile.TemporaryDirectory()
        scenario = Path(cls.directory.name) / "scenario.json"
        scenario.write_text(json.dumps({"duration_s": 60, "actors": []}))
        full = [[0, 0], [1, 0], [1, 1], [0, 1]]
        config = {
            "site": {"id": "ui_health", "name": "Health UI test"},
            "cameras": [
                {"id": "cam_ok", "name": "Driveway", "source": {"type": "simulated", "scenario": str(scenario)},
                 "zones": [{"id": "drive", "name": "Drive", "type": "driveway", "polygon": full}]},
                {"id": "cam_down", "name": "Back gate", "source": {"type": "simulated", "scenario": str(scenario),
                                                                     "faults": [{"type": "disconnect", "at_s": 0}]},
                 "zones": [{"id": "gate", "name": "Gate", "type": "perimeter", "polygon": full}]},
            ],
            "rules": [],
            "detector": {"type": "scripted"},
            "environment": {"enabled": False},
            "health": {"frame_timeout_s": 1, "startup_grace_s": 1, "notify_after_s": 1},
            "storage": {"dir": str(Path(cls.directory.name) / "store")},
            "drone": {"enabled": False},
        }
        cls.service = SentinelService(config)
        cls.service.start()
        cls.port = free_port()
        cls.server = uvicorn.Server(uvicorn.Config(create_app(cls.service, api_token=None), host="127.0.0.1", port=cls.port, log_level="warning"))
        threading.Thread(target=cls.server.run, daemon=True).start()
        deadline = time.time() + 10
        while not cls.server.started and time.time() < deadline:
            time.sleep(0.05)
        cls.playwright = sync_playwright().start()
        cls.browser = None
        for options in ({"channel": "msedge"}, {}):
            try:
                cls.browser = cls.playwright.chromium.launch(headless=True, **options)
                break
            except PlaywrightError:
                continue
        if cls.browser is None:
            cls.tearDownClass()
            raise unittest.SkipTest("no Edge or Chromium browser available to Playwright")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "browser", None):
            cls.browser.close()
        cls.playwright.stop()
        cls.server.should_exit = True
        cls.service.stop()
        time.sleep(0.3)
        cls.directory.cleanup()

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        self.page.goto(f"http://127.0.0.1:{self.port}/")

    def tearDown(self):
        self.page.close()

    def test_offline_camera_is_marked_not_monitoring_on_the_live_tab(self):
        page = self.page
        down = page.locator('[data-camera="cam_down"]')
        expect(down.locator(".camera-overlay")).to_be_visible(timeout=15000)
        expect(down.locator(".camera-overlay")).to_contain_text("NOT MONITORING: NO VIDEO")
        expect(down.locator(".camera-overlay")).to_contain_text("simulated disconnect")
        expect(page.locator('[data-camera="cam_ok"] .camera-overlay')).to_be_hidden()
        expect(page.locator('[data-camera="cam_ok"] .status')).to_contain_text("fps")
        expect(page.locator("#coverage")).to_contain_text("Coverage degraded 1/2", timeout=15000)
        expect(page.locator("#system-badge")).to_be_visible()

    def test_system_tab_shows_health_outages_and_makes_a_backup(self):
        page = self.page
        page.click('nav.tabs [data-tab="system"]')
        expect(page.locator("#coverage-banner")).to_contain_text("Security coverage is degraded", timeout=15000)
        expect(page.locator("#coverage-banner")).to_contain_text("Back gate: No video")
        expect(page.locator('[data-health-camera="cam_down"]')).to_contain_text("No video")
        expect(page.locator('[data-health-camera="cam_ok"]')).to_contain_text("Monitoring")
        expect(page.locator("#outage-table")).to_contain_text("Back gate", timeout=15000)
        expect(page.locator("#health-resources")).to_contain_text("CPU")
        page.click("#backup-now")
        expect(page.locator("#toasts")).to_contain_text("Backup written")
        expect(page.locator("#backup-list")).to_contain_text("manual")
        page.set_viewport_size({"width": 390, "height": 844})
        fits = page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
        self.assertTrue(fits, "System tab scrolls sideways at phone width")


if __name__ == "__main__":
    unittest.main()
