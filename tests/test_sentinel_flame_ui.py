"""The existing FLAME dashboard against the simulated farm, in a real browser."""

import tempfile
import threading
import time
import unittest

from sentinel.config import load_config
from test_sentinel_ui import free_port, sync_playwright

if sync_playwright is not None:
    from test_sentinel_ui import PlaywrightError, uvicorn, expect


@unittest.skipIf(sync_playwright is None, "playwright/uvicorn not installed")
class FlameDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sentinel.api import create_app
        from sentinel.service import SentinelService

        cls.directory = tempfile.TemporaryDirectory()
        config = load_config("config/sentinel.farm.example.json", resolve_secrets=False)
        config["storage"] = {"dir": cls.directory.name}
        cls.service = SentinelService(config, start_cameras=False)
        cls.service.start()
        cls.port = free_port()
        cls.server = uvicorn.Server(uvicorn.Config(create_app(cls.service, api_token=None), host="127.0.0.1", port=cls.port, log_level="warning"))
        cls.server_thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.server_thread.start()
        deadline = time.monotonic() + 10
        while not cls.server.started and time.monotonic() < deadline:
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
            raise unittest.SkipTest("no Edge or Chromium browser available")

    @classmethod
    def tearDownClass(cls):
        if cls.browser:
            cls.browser.close()
        cls.playwright.stop()
        cls.server.should_exit = True
        cls.server_thread.join(5)
        cls.service.stop()
        cls.directory.cleanup()

    def test_map_records_configuration_faults_and_security_tab(self):
        page = self.browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(f"http://127.0.0.1:{self.port}/")
            page.locator('nav.tabs [data-tab="flame"]').click()
            kit = page.locator('[data-accessory="burn_kit"]')
            expect(kit).to_contain_text("attached")
            kit.locator("details").first.locator("summary").click()
            form = kit.locator(".flame-record")
            form.locator('[name="kind"]').select_option("commissioning")
            form.locator('[name="measured_mass_g"]').fill("262")
            form.locator('[name="cg_checked"]').check()
            form.locator('button[type="submit"]').click()
            expect(kit).to_contain_text("Commissioned")
            kit.locator("details").first.locator("summary").click()
            kit.locator('.flame-record [name="kind"]').select_option("inspection")
            kit.locator('.flame-record button[type="submit"]').click()
            switch = page.locator('[data-config="farm_drone"]')
            expect(switch).to_be_enabled()
            switch.click()
            expect(switch).to_contain_text("Switch to security")
            kit.locator('[data-action="latch_open"]').click()
            expect(kit).to_contain_text("OPEN")
            kit.locator('[data-action="clear"]').click()
            expect(kit.locator(".kv")).to_contain_text("closed")
            page.locator("#flame-draw").click()
            field_map = page.locator("#flame-map")
            field_map.scroll_into_view_if_needed()
            box = field_map.bounding_box()
            for x, y in ((0.2, 0.2), (0.6, 0.2), (0.6, 0.6), (0.2, 0.6)):
                field_map.click(position={"x": box["width"] * x, "y": box["height"] * y})
            self.assertEqual(errors, [], "browser errors while drawing the field")
            self.assertEqual(page.evaluate("flame.draft.points.length"), 4)
            page.locator('#flame-field-form [name="name"]').fill("Regression field")
            page.locator('#flame-field-form [name="watch_buffer_m"]').fill("20")
            with page.expect_response(lambda response: response.url.endswith("/api/v1/flame/fields") and response.request.method == "POST") as saved:
                page.locator('#flame-field-form button[type="submit"]').click()
            self.assertEqual(saved.value.status, 200, saved.value.text())
            expect(page.locator("#flame-fields")).to_contain_text("Regression field")
            page.locator('[data-config="farm_drone"]').click()
            expect(page.locator('[data-config="farm_drone"]')).to_contain_text("Switch to agricultural monitoring")
            page.locator("#flame-fields [data-plan]").click()
            note = page.locator('#op-note [name="text"]')
            note.fill("Unsent crew observation")
            page.evaluate("refreshOperation()")
            expect(note).to_have_value("Unsent crew observation")
            expect(note).to_be_focused()
            page.locator('nav.tabs [data-tab="live"]').click()
            expect(page.locator("#camera-grid")).to_contain_text("No cameras yet")
            self.assertEqual(errors, [])
        finally:
            page.close()
