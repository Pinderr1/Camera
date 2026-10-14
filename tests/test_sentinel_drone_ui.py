"""Browser test (Microsoft Edge via Playwright) of the Drone tab with the
built-in simulator: readiness, approval, route export links, forms that keep
their input across live updates, maintenance records, and phone width.

Skipped unless `playwright` and Edge or Chromium are available."""

import copy
import unittest

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from sentinel.config import load_config

from test_sentinel_install_ui import _Server


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class DroneTabTests(_Server, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.start(copy.deepcopy(load_config("config/sentinel.example.json", resolve_secrets=False)), start_cameras=False)

    @classmethod
    def tearDownClass(cls):
        cls.stop()

    def open_drone(self, width=1280, height=900):
        self.page = self.browser.new_page(viewport={"width": width, "height": height})
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(f"http://127.0.0.1:{self.port}/")
        expect(self.page.locator("#site-name")).not_to_have_text("Connecting…")
        self.page.click('nav.tabs [data-tab="drone"]')
        expect(self.page.locator("#drone-readiness")).to_be_visible()

    def test_readiness_approval_and_exports(self):
        self.open_drone()
        page = self.page
        expect(page.locator("#drone-mode-notice")).to_contain_text("SIMULATION")
        expect(page.locator('#drone-readiness [data-check="battery"]')).to_have_attribute("data-state", "pass")
        expect(page.locator('#drone-readiness [data-check="fc_settings"]')).to_have_attribute("data-state", "na")
        page.fill('#drone-conditions [name="wind_mps"]', "4")
        page.wait_for_timeout(2500)  # a live update arrives meanwhile
        expect(page.locator('#drone-conditions [name="wind_mps"]')).to_have_value("4")
        request = self.service.drone.request_inspection(requested_by="sentinel", map_point=(35.5, 3), label="Shed", source="incident",
                                                        incident_id="inc_ui", reason="Person entered Shed")
        item = page.locator(f'[data-request="{request.request_id}"]')
        expect(item).to_contain_text("Person entered Shed")
        expect(item.locator("a", has_text="Route (.plan)")).to_have_attribute("href", f"/api/v1/drone/inspections/{request.request_id}/mission")
        page.once("dialog", lambda dialog: dialog.accept())
        item.locator("[data-approve]").click()
        expect(item).to_contain_text("in progress")
        expect(page.locator("#drone-commands-card")).to_be_visible()
        page.select_option('#drone-maintenance-form [name="kind"]', "propellers")
        page.click('#drone-maintenance-form button[type="submit"]')
        expect(page.locator("#toasts")).to_contain_text("Maintenance recorded")

    def test_phone_width(self):
        self.open_drone(width=390, height=844)
        overflow = self.page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        self.assertLessEqual(overflow, 1)

    def tearDown(self):
        self.page.close()
        self.assertEqual(self.errors, [], "JavaScript errors in the page")


if __name__ == "__main__":
    unittest.main()
