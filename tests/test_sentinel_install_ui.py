"""Browser tests (Microsoft Edge via Playwright) of the Install tab: the
existing-camera journey on running simulated cameras, a drone-first install on
a site with no cameras, the property planner with save and resume, incomplete
installations, device removal, and the phone-width layout.

Skipped unless `playwright` and Edge or Chromium are available. Run alone:
    python -m unittest tests.test_sentinel_install_ui"""

import copy
import tempfile
import threading
import time
import unittest

try:
    import uvicorn
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from sentinel.config import load_config

from test_sentinel_ui import free_port


class _Server:
    """A live Sentinel server and a browser, shared by one test class."""

    @classmethod
    def start(cls, config, *, start_cameras):
        from sentinel.api import create_app
        from sentinel.service import SentinelService

        cls.directory = tempfile.TemporaryDirectory()
        config["storage"] = {"dir": cls.directory.name}
        cls.service = SentinelService(config, start_cameras=start_cameras)
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
            cls.stop()
            raise unittest.SkipTest("no Edge or Chromium browser available to Playwright")

    @classmethod
    def stop(cls):
        if getattr(cls, "browser", None):
            cls.browser.close()
        cls.playwright.stop()
        cls.server.should_exit = True
        cls.service.stop()
        time.sleep(0.3)
        cls.directory.cleanup()

    def open_install(self, width=1280, height=900):
        self.service.installation.reset(keep_plan=False)
        self.page = self.browser.new_page(viewport={"width": width, "height": height})
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(f"http://127.0.0.1:{self.port}/")
        expect(self.page.locator("#site-name")).not_to_have_text("Connecting…")
        self.page.click('nav.tabs [data-tab="install"]')
        expect(self.page.locator("[data-path]").first).to_be_visible()

    def tearDown(self):
        self.page.close()
        self.assertEqual(self.errors, [], "JavaScript errors in the page")

    def step(self, name):
        expect(self.page.locator("#install-step h3").first).to_have_text(name)

    def done(self, step):
        self.page.click(f'[data-mark="{step}"][data-status="done"]')

    def assert_no_protection_claims(self):
        text = self.page.locator("#tab-install").inner_text().lower()
        for word in ("protected", "secure", "safe from"):
            self.assertNotIn(word, text)


@unittest.skipIf(sync_playwright is None, "playwright/uvicorn not installed")
class ExistingCameraInstallUiTests(_Server, unittest.TestCase):
    """Running simulated cameras with the scripted detector: verification is earned from real frames."""

    @classmethod
    def setUpClass(cls):
        config = load_config("config/sentinel.example.json", resolve_secrets=False)
        config["drone"]["enabled"] = False
        cls.start(config, start_cameras=True)

    @classmethod
    def tearDownClass(cls):
        cls.stop()

    def test_existing_camera_journey_to_completion(self):
        self.open_install()
        page = self.page
        page.click('[data-path="existing_cameras"]')
        expect(page.locator('[data-path="existing_cameras"]')).to_have_class("choice selected")
        self.done("type")
        self.step("Property")
        page.fill("#property-form [name=name]", "Maple Street")
        page.click("#property-form button[type=submit]")
        self.step("Plan or discover")
        expect(page.locator("#site-name")).to_have_text("Maple Street")
        expect(page.locator("#plan-find")).to_be_visible()
        page.click('[data-mark="plan"][data-status="skipped"]')
        self.step("Register and connect hardware")
        expect(page.locator(".device")).to_have_count(4)  # this computer + three cameras
        expect(page.locator("#camera-guide")).to_be_visible()
        self.done("hardware")
        self.step("Verify devices")
        # Each simulated camera must earn "AI monitoring verified" from frames reaching the detector.
        for _ in range(20):
            if "3 / 3" in page.locator("#install-step .tiles").inner_text():
                break
            page.click("#verify-retest")
            page.wait_for_timeout(1000)
        expect(page.locator(".tile", has_text="AI monitoring verified")).to_contain_text("3 / 3")
        expect(page.locator(".preview-tile")).to_have_count(3)
        expect(page.locator("#install-statement")).to_contain_text("AI monitoring verified on 3 of 3 camera(s).")
        self.done("verify")
        self.step("Zones and privacy")
        expect(page.locator("[data-edit-zones]")).to_have_count(3)
        self.done("zones")
        self.step("AI monitoring and rules")
        self.done("rules")
        self.step("Alerts and notifications")
        page.click('[data-mark="alerts"][data-status="skipped"]')
        self.step("System health check")
        self.done("health")
        self.step("Finish")
        expect(page.locator("#finish-complete")).to_be_enabled()
        page.click("#finish-complete")
        expect(page.locator(".toast").last).to_contain_text("Please confirm")
        page.check("#finish-ack")
        page.click("#finish-complete")
        expect(page.locator("#install-step .banner.good")).to_contain_text("Installation completed")
        expect(page.locator("#install-badge")).to_be_hidden()
        self.assert_no_protection_claims()
        page.click("#finish-dashboard")
        expect(page.locator("#tab-live")).to_have_class("tab active")

    def test_zone_shortcut_opens_the_zone_editor_for_that_camera(self):
        self.open_install()
        self.page.click('[data-istep="zones"]')
        self.page.click('[data-edit-zones="cam_backyard"]')
        expect(self.page.locator("#sub-zones")).to_have_class("sub active")
        expect(self.page.locator("#zone-camera")).to_have_value("cam_backyard")

    def test_incomplete_installation_cannot_be_completed(self):
        self.open_install()
        self.page.click('[data-istep="finish"]')
        expect(self.page.locator("#install-step .banner.bad")).to_contain_text("Choose an installation type")
        expect(self.page.locator("#finish-complete")).to_be_disabled()
        expect(self.page.locator("#install-steps li").first).to_contain_text("1")


@unittest.skipIf(sync_playwright is None, "playwright/uvicorn not installed")
class DroneFirstInstallUiTests(_Server, unittest.TestCase):
    """A site that starts with no cameras and no devices at all."""

    @classmethod
    def setUpClass(cls):
        config = load_config("config/sentinel.drone-first.example.json", resolve_secrets=False)
        config["devices"] = []
        config["drone"] = {"enabled": False}
        config["site"]["map"].pop("origin_geo")
        cls.start(config, start_cameras=True)

    @classmethod
    def tearDownClass(cls):
        cls.stop()

    def place(self, tool, fx, fy):
        self.page.click(f'[data-tool="{tool}"]')
        box = self.page.locator("#plan-svg").bounding_box()
        self.page.mouse.click(box["x"] + box["width"] * fx, box["y"] + box["height"] * fy)

    def test_drone_first_journey(self):
        self.open_install()
        page = self.page
        page.click('nav.tabs [data-tab="live"]')
        expect(page.locator("#camera-grid")).to_contain_text("No cameras yet")
        expect(page.locator("#coverage")).to_have_text("No cameras")
        page.click('nav.tabs [data-tab="install"]')
        page.click('[data-path="drone_first"]')
        self.done("type")
        page.fill("#property-form [name=name]", "Hilltop lot")
        page.click("#use-test-location")
        page.click("#property-form button[type=submit]")
        self.step("Plan or discover")
        self.place("drone_dock", 0.1, 0.85)
        self.place("drone_point", 0.8, 0.15)
        page.click("#plan-save")
        expect(page.locator("#plan-status")).to_have_text("")
        self.assertEqual(len(self.service.installation.state["plan"]["items"]), 2)
        self.done("plan")
        self.step("Register and connect hardware")
        page.click('[data-register="drone"]')
        page.fill("#register-form [name=name]", "Yard drone")
        expect(page.locator("#register-form [name=profile]")).to_have_value("simulated_drone")
        page.click("#register-form button[type=submit]")
        drone = page.locator(".device", has_text="Yard drone")
        expect(drone).to_contain_text("Ready for operator-approved inspections")
        expect(drone.locator(".support")).to_have_text("Simulated")
        page.click('[data-register="contact_sensor"]')
        page.fill("#register-form [name=name]", "Back door")
        page.select_option("#register-form [name=profile]", "simulated_contact")
        page.click("#register-form button[type=submit]")
        sensor = page.locator(".device", has_text="Back door")
        expect(sensor).to_contain_text("Last report")
        sensor.locator("[data-device-test]").click()
        expect(page.locator(".toast").last).to_contain_text("Sensor test received")
        # Planned hardware registers, but is never shown as connected.
        page.click('[data-register="contact_sensor"]')
        page.fill("#register-form [name=name]", "Garage contact")
        page.select_option("#register-form [name=profile]", "mqtt_sensor")
        page.click("#register-form button[type=submit]")
        expect(page.locator(".device", has_text="Garage contact")).to_contain_text("Planned hardware: registration only")
        self.done("hardware")
        self.step("Verify devices")
        expect(page.locator(".tile", has_text="Drones ready")).to_contain_text("1")
        self.done("verify")
        self.step("Zones and privacy")
        page.click("#zones-use-points")
        expect(page.locator("#install-step")).to_contain_text("Drone point")
        expect(page.locator("#install-statement")).to_contain_text("No fixed cameras")
        self.assert_no_protection_claims()
        # The drone tab appears and offers the planned observation point for an operator-approved request.
        page.click('nav.tabs [data-tab="drone"]')
        expect(page.locator("#drone-points li")).to_have_count(1)
        page.click("[data-inspect-point]")
        expect(page.locator("#drone-requests")).to_contain_text("pending approval")
        # Removing the device later.
        page.click('nav.tabs [data-tab="install"]')
        page.click('[data-istep="hardware"]')
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator(".device", has_text="Back door").locator("[data-device-remove]").click()
        expect(page.locator(".device", has_text="Back door")).to_have_count(0)

    def test_planner_draws_saves_and_resumes(self):
        self.open_install(height=1600)  # mouse clicks need the whole map inside the viewport
        page = self.page
        page.click('[data-path="new_cameras"]')
        page.click('[data-istep="plan"]')
        self.step("Plan or discover")
        page.fill("#recommend-form [name=exterior_doors]", "1")
        page.click("#recommend-form button[type=submit]")
        expect(page.locator("#recommend-place")).to_be_visible()
        page.click("#recommend-place")
        page.locator("#recommend-box summary").click()  # fold the questions away
        page.locator("#plan-svg").scroll_into_view_if_needed()
        page.click('[data-tool="building"]')
        box = page.locator("#plan-svg").bounding_box()
        for fx, fy in ((0.3, 0.3), (0.7, 0.3), (0.7, 0.6), (0.3, 0.6)):
            page.mouse.click(box["x"] + box["width"] * fx, box["y"] + box["height"] * fy)
        page.keyboard.press("Enter")
        expect(page.locator("polygon.pl-building")).to_have_count(1)
        page.click('[data-tool="entrance"]')
        page.mouse.click(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.6)
        page.click('[data-tool="camera"]')
        page.mouse.click(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.75)
        expect(page.locator("#plan-props")).to_contain_text("Detection range")
        # Aim the selected camera at the door by dragging its handle upwards.
        handle = page.locator(".aim-handle").bounding_box()
        page.mouse.move(handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.62, steps=5)
        page.mouse.up()
        expect(page.locator("#plan-props [data-prop=heading_deg]")).to_have_value("0")
        page.click("#plan-check")
        expect(page.locator("#plan-coverage")).to_contain_text("Door: seen by Camera")
        expect(page.locator("#plan-coverage")).to_contain_text("Assumptions")
        page.click("#plan-save")
        expect(page.locator("#plan-status")).to_have_text("")
        # Delete the recommended (unplaced) camera with the keyboard, then resume after a reload.
        page.locator("circle.pt-camera").first.click()
        page.locator("#plan-svg").press("Delete")
        expect(page.locator("#plan-status")).to_have_text("Unsaved changes")
        page.click("#plan-save")
        saved = len(self.service.installation.state["plan"]["items"])
        page.reload()
        page.click('nav.tabs [data-tab="install"]')
        self.step("Plan or discover")
        expect(page.locator("#plan-svg [data-item]")).to_have_count(saved)
        report = page.request.get(f"http://127.0.0.1:{self.port}/api/v1/installation/report.html")
        self.assertIn("Installation plan", report.text())

    def test_phone_layout_has_no_horizontal_scroll(self):
        self.open_install(width=390)
        page = self.page
        page.click('[data-path="complete_package"]')
        for step in ("type", "property", "plan", "hardware", "finish"):
            page.click(f'[data-istep="{step}"]')
            page.wait_for_timeout(300)
            overflow = page.evaluate("document.scrollingElement.scrollWidth - window.innerWidth")
            self.assertLessEqual(overflow, 0, f"horizontal scroll on the {step} step")


if __name__ == "__main__":
    unittest.main()
