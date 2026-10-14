"""Browser tests (Microsoft Edge via Playwright) of the security-intelligence
dashboard: the rule builder and rule tester, incident investigation
(explanation, timeline, feedback, notes, search), missed-event reports,
activity insights, alert performance, and the Night/Vacation modes.

The server is seeded with a deterministic replay of the "side gate then shed
after midnight" scenario and three weeks of synthetic activity statistics.
Skipped unless `playwright` and Edge or Chromium are available."""

import copy
import tempfile
import threading
import time
import unittest
from datetime import datetime

try:
    import uvicorn
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from sentinel.config import load_config
from sentinel.replay import run_scenario
from sentinel.simulation import Scenario

from test_sentinel_intelligence import GATE_THEN_SHED, synthetic_history
from test_sentinel_ui import free_port


@unittest.skipIf(sync_playwright is None, "playwright/uvicorn not installed")
class IntelligenceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sentinel.api import create_app
        from sentinel.service import SentinelService

        cls.directory = tempfile.TemporaryDirectory()
        config = load_config("config/sentinel.example.json", resolve_secrets=False)
        config["storage"] = {"dir": cls.directory.name}
        config["drone"]["enabled"] = False
        config["rules"].append(GATE_THEN_SHED)
        replay_config = copy.deepcopy(config)
        night = datetime.now().replace(hour=0, minute=40, second=0, microsecond=0)
        run_scenario(replay_config, Scenario.load("config/scenarios/gate_then_shed.json"), start=night, arm_mode="home", storage_dir=cls.directory.name)
        cls.service = SentinelService(config, start_cameras=False)
        visits, trips = synthetic_history(datetime.now())
        cls.service.runtime.store.add_activity(visits, trips)
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
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(f"http://127.0.0.1:{self.port}/")
        expect(self.page.locator("#site-name")).not_to_have_text("Connecting…")

    def tearDown(self):
        self.page.close()
        self.assertEqual(self.errors, [], "JavaScript errors in the page")

    def open_rules(self):
        self.page.click('nav.tabs [data-tab="setup"]')
        self.page.click('.subtabs button[data-sub="rules"]')
        expect(self.page.locator("#rule-list-setup")).to_contain_text("restricted entry")

    def test_rule_builder_requires_a_test_before_turning_a_rule_on(self):
        page = self.page
        self.open_rules()
        page.click("#rule-new")
        page.click('[data-template="gate_then_shed_night"]')
        expect(page.locator("#rule-errors")).to_contain_text("at least two zones")
        for zone in ("side_gate", "shed"):
            page.click("#seq-add")
            page.locator("[data-seq]").last.select_option(zone)
            page.locator("[data-seq]").last.dispatch_event("input")
        expect(page.locator("#rule-plain")).to_contain_text("goes to Side gate then Shed within 3 min")
        expect(page.locator("#rule-plain")).to_contain_text("between 00:00 and 05:00")
        expect(page.locator("#rule-errors")).to_be_empty()
        expect(page.locator("#rule-save-on")).to_be_disabled()
        page.select_option("#test-mode", "home")
        page.fill("#test-start", datetime.now().strftime("%Y-%m-%dT00:40"))
        page.click("#rule-test")
        expect(page.locator("#test-verdict")).to_contain_text("The rule fired 1 time(s) (high) and 1 notification(s)")
        expect(page.locator("#rule-save-on")).to_be_enabled()
        page.click("#rule-save-on")
        expect(page.locator("#toasts")).to_contain_text("Rule saved and on")
        expect(page.locator("#rule-list-setup")).to_contain_text("Side gate after midnight, then the shed")
        saved = next(rule for rule in self.service.site.config["rules"] if rule.get("name") == "Side gate after midnight, then the shed")
        self.assertEqual((saved["zones"], saved["enabled"], saved["notify"]), (["side_gate", "shed"], True, "always"))
        self.assertIn(saved["id"], [rule["id"] for rule in self.service.runtime.engine.rules], "applied to the running engine")
        # Changing the rule makes the previous test stale again.
        page.fill('[data-f="within_s"]', "120")
        page.locator('[data-f="within_s"]').dispatch_event("input")
        expect(page.locator("#rule-save-on")).to_be_disabled()

    def test_rule_toggle_and_blank_rule_validation(self):
        page = self.page
        self.open_rules()
        page.once("dialog", lambda dialog: dialog.accept())
        toggle = page.locator('[data-rule-toggle="porch_dwell"]')
        toggle.uncheck()
        expect(page.locator("#toasts")).to_contain_text("Rule off")
        self.assertFalse(next(r for r in self.service.runtime.engine.rules if r["id"] == "porch_dwell")["enabled"])
        page.locator('[data-rule-toggle="porch_dwell"]').check()
        expect(page.locator("#toasts")).to_contain_text("Rule on")
        page.click("#rule-new")
        page.click('[data-template=""]')
        page.select_option('[data-f="type"]', "repeated_presence")
        page.fill('[data-f="count"]', "1")
        page.locator('[data-f="count"]').dispatch_event("input")
        expect(page.locator("#rule-errors")).to_contain_text("count must be an integer >= 2")
        expect(page.locator("#rule-save-off")).to_be_disabled()

    def test_incident_investigation(self):
        page = self.page
        page.click('nav.tabs [data-tab="timeline"]')
        item = page.locator("#incident-list .incident").first
        expect(item).to_contain_text("Side gate -> Shed")
        item.click()
        detail = page.locator("#incident-detail")
        expect(detail).to_contain_text("Sentinel observed person on 1 camera(s)")
        expect(detail).to_contain_text("Side gate then shed after midnight")
        expect(detail).to_contain_text("between 00:00 and 05:00")
        expect(detail.locator(".basis.observed").first).to_be_visible()
        expect(detail).to_contain_text("no cross-camera inference was needed")
        expect(detail).to_contain_text("A notification was sent at high severity.")
        markers = page.locator("#timeline-svg [data-step]")
        self.assertGreaterEqual(markers.count(), 2)
        markers.first.click()
        expect(page.locator("#toasts")).to_contain_text("no recording covers this moment")
        page.fill('#note-form input[name="text"]', "Checked with the neighbour: they were fetching a ladder")
        page.click('#note-form button[type="submit"]')
        expect(page.locator("#note-list")).to_contain_text("fetching a ladder")
        page.click('#incident-detail [data-outcome="real_concern"]')
        expect(page.locator("#incident-detail")).to_contain_text("Genuine concern")
        incident = self.service.runtime.store.list_incidents()[0]
        self.assertEqual(incident["resolution"], "real_concern")
        form = page.locator("#incident-search")
        form.locator('select[name="outcome"]').select_option("false_alarm")
        form.locator('button[type="submit"]').click()
        expect(page.locator("#incident-list")).to_contain_text("No incidents match")
        form.locator('select[name="outcome"]').select_option("real_concern")
        form.locator('select[name="zone_id"]').select_option("shed")
        form.locator('button[type="submit"]').click()
        expect(page.locator("#incident-list .incident")).to_have_count(1)
        with page.expect_download() as download:
            page.click("text=Export report")
        self.assertTrue(download.value.suggested_filename.endswith(".zip"))

    def test_missed_event_report_and_alert_performance(self):
        page = self.page
        page.click('nav.tabs [data-tab="timeline"]')
        page.click("#report-missed")
        form = page.locator("#missed-form")
        form.locator('select[name="camera_id"]').select_option("cam_front")
        form.locator('select[name="zone_id"]').select_option("front_porch")
        form.locator('select[name="category"]').select_option("vehicle")
        form.locator('input[name="description"]').fill("Van stopped at the porch")
        form.locator('button[type="submit"]').click()
        expect(page.locator("#toasts")).to_contain_text("Missed event recorded")
        page.click('nav.tabs [data-tab="insights"]')
        page.click('.subtabs-alt button[data-isub="performance"]')
        expect(page.locator("#performance-body")).to_contain_text("Van stopped at the porch")
        expect(page.locator("#performance-rules")).to_contain_text("Side gate then shed after midnight")

    def test_insights_show_patterns_unusual_items_and_suggestions(self):
        page = self.page
        page.click('nav.tabs [data-tab="insights"]')
        expect(page.locator("#insights-status")).to_contain_text("Unusual is not the same as suspicious")
        body = page.locator("#insights-body")
        expect(body).to_contain_text("usually 17:00–20:00")
        expect(body).to_contain_text("Front porch")
        expect(page.locator("#unusual-list")).to_contain_text("Vehicle in Driveway at")
        page.click('[data-suggest="0"]')
        expect(page.locator('#rule-form [data-f="name"]')).to_have_value("Vehicle arriving outside usual hours")
        expect(page.locator("#rule-plain")).to_contain_text("between 20:00 and 17:00")
        self.assertNotIn("vehicle_arriving_outside_usual_hours", [rule["id"] for rule in self.service.site.config["rules"]],
                         "a suggestion is never applied without the owner saving it")

    def test_night_and_vacation_modes(self):
        page = self.page
        page.click('.arm button[data-arm="night"]')
        expect(page.locator('.arm button[data-arm="night"]')).to_have_class("active")
        self.assertEqual(self.service.runtime.settings["arm_mode"], "night")
        page.click('.arm button[data-arm="vacation"]')
        expect(page.locator('.arm button[data-arm="vacation"]')).to_have_class("active")
        page.click('.arm button[data-arm="home"]')
        expect(page.locator('.arm button[data-arm="home"]')).to_have_class("active")

    def test_new_views_fit_a_phone_screen(self):
        page = self.page
        page.set_viewport_size({"width": 390, "height": 844})
        fits = lambda: page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")  # noqa: E731
        self.open_rules()
        page.click('[data-rule-edit="perimeter_walk"]')
        expect(page.locator("#rule-plain")).to_contain_text("within 3 min")
        self.assertTrue(fits(), "rule builder scrolls sideways at phone width")
        page.click('nav.tabs [data-tab="insights"]')
        expect(page.locator("#insights-body")).to_contain_text("Front porch")
        self.assertTrue(fits(), "insights scroll sideways at phone width")
        page.click('nav.tabs [data-tab="timeline"]')
        page.locator("#incident-list .incident").first.click()
        expect(page.locator("#incident-detail")).to_contain_text("What was detected")
        self.assertTrue(fits(), "incident view scrolls sideways at phone width")


if __name__ == "__main__":
    unittest.main()
