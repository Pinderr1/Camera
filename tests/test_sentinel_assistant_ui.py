"""Browser tests (Microsoft Edge via Playwright) of Sentinel AI Guide: first-run
welcome, the Hikvision regression from diagnosis to retest, secure sign-in with
a wrong then a right password, adding the recorder's cameras, proposals
confirmed and cancelled, resuming a conversation after a reload, deleting it,
the provider-unavailable notice, and the phone layout. Built-in guide only: no
AI provider is called. Skipped unless playwright and Edge/Chromium exist."""

import copy
import json
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

from test_sentinel_ui import free_port

SCENARIO = json.loads(Path("config/netsim/hikvision_wrong_subnet.json").read_text(encoding="utf-8"))
FIXED = copy.deepcopy(SCENARIO)
FIXED["devices"][0].update(ip="10.0.0.84", gateway="10.0.0.1")


@unittest.skipIf(sync_playwright is None, "playwright/uvicorn not installed")
class AssistantUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sentinel.api import create_app
        from sentinel.service import SentinelService

        cls.directory = tempfile.TemporaryDirectory()
        config = load_config("config/sentinel.drone-first.example.json", resolve_secrets=False)
        config["storage"] = {"dir": cls.directory.name}
        config["devices"] = []
        config["drone"] = {"enabled": False}
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
        time.sleep(0.3)
        cls.directory.cleanup()

    def setUp(self):
        self.service.network = SimulatedNetwork(SCENARIO)
        for camera in list(self.service.site.config["cameras"]):
            self.service.site.delete_camera(camera["id"], "test")
        self.service.installation.reset(keep_plan=False)
        for item in self.service.assistant.store.list():
            self.service.assistant.store.delete(item["id"])
        self.page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(f"http://127.0.0.1:{self.port}/")

    def tearDown(self):
        self.page.close()
        self.assertEqual(self.errors, [], "JavaScript errors in the page")

    def say(self, text):
        before = self.page.locator(".guide-msg.assistant").count()
        self.page.fill("#guide-input", text)
        self.page.keyboard.press("Enter")
        expect(self.page.locator(".guide-msg.assistant")).to_have_count(before + 1, timeout=30000)
        return self.page.locator(".guide-msg.assistant").last

    def test_first_run_lands_on_the_guide(self):
        expect(self.page.locator("#tab-guide")).to_have_class("tab active")
        expect(self.page.locator(".guide-welcome h2")).to_have_text("Welcome to Sentinel. I'll help you get everything connected.")
        expect(self.page.locator(".guide-welcome [data-say]")).to_have_count(5)
        expect(self.page.locator("#guide-mode")).to_contain_text("Built-in guide (offline)")
        self.page.click("[data-say='Set up a complete system']")
        reply = self.page.locator(".guide-msg.assistant").last
        expect(reply).to_contain_text("A complete system can grow in any order")
        card = reply.locator("[data-proposal]")
        expect(card).to_contain_text("Waiting for your confirmation")
        card.locator("[data-decide='confirm']").click()
        expect(self.page.locator("[data-proposal]").first).to_contain_text("Done")
        self.assertEqual(self.service.installation.state["path"], "complete_package")

    def test_hikvision_recorder_from_diagnosis_to_added_cameras(self):
        page = self.page
        reply = self.say("I already have eight Hikvision cameras. The recorder is at 192.168.1.84 and they aren't showing up")
        expect(reply).to_contain_text("Confirmed: 192.168.1.84 cannot be reached")
        expect(reply).to_contain_text("Next step:")
        expect(reply.locator(".link-button")).to_contain_text("Open the full diagnosis")
        # Retest before anything changed, then after the owner re-addressed the recorder on its own screen.
        reply.locator("[data-say='Retest']").click()
        expect(page.locator(".guide-msg.assistant").last).to_contain_text("Still no answer")
        self.service.network = SimulatedNetwork(FIXED)
        reply = self.say("I switched it to DHCP; it now shows 10.0.0.84")
        form = reply.locator("form.recorder-login")
        expect(form).to_contain_text("never sent to the assistant")
        form.locator("[name=password]").fill("wrong-password")
        form.locator("button[type=submit]").click()
        expect(page.locator(".guide-note").last).to_contain_text("sign-in was refused")
        expect(form.locator("[name=password]")).to_have_value("")
        form.locator("[name=password]").fill("simulated-only")
        form.locator("button[type=submit]").click()
        expect(form.locator("[data-pick]")).to_have_count(8, timeout=20000)
        form.locator("[data-pick]").nth(7).uncheck()
        form.locator("[data-add-channels]").click()
        expect(page.locator(".guide-msg.assistant").last).to_contain_text("Sentinel now has 7 camera(s)", timeout=20000)
        self.assertEqual(len(self.service.site.config["cameras"]), 7)
        stored = "".join(path.read_text(encoding="utf-8") for path in (Path(self.directory.name) / "assistant").glob("*.json"))
        self.assertNotIn("simulated-only", stored)
        self.assertNotIn("wrong-password", stored)

    def test_rule_proposal_cancel_resume_and_delete(self):
        page = self.page
        self.service.site.save_camera(None, {"name": "Drive", "draft_id": self.service.site.make_draft({"type": "simulated", "scenario": "none.json"})[0],
                                             "zones": [{"id": "driveway", "name": "Driveway", "type": "driveway", "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]}]}, "test")
        reply = self.say("Watch my driveway at night")
        card = reply.locator("[data-proposal]")
        expect(card).to_contain_text("between 22:00 and 06:00")
        card.locator("[data-decide='cancel']").click()
        expect(page.locator("[data-proposal]").first).to_contain_text("Cancelled")
        self.assertFalse(any(rule.get("name", "").startswith("Person in Driveway") for rule in self.service.site.config.get("rules", [])))
        # After a reload the site keeps its Live view (it has a camera and no journey in progress); the conversation resumes in the Guide.
        page.reload()
        expect(page.locator("#tab-live")).to_have_class("tab active")
        page.click('nav.tabs [data-tab="guide"]')
        expect(page.locator(".guide-msg.user").first).to_have_text("Watch my driveway at night")
        expect(page.locator("#guide-conversations li")).to_have_count(1)
        page.once("dialog", lambda dialog: dialog.accept())
        page.click("#guide-delete")
        expect(page.locator(".guide-welcome")).to_be_visible()
        self.assertEqual(self.service.assistant.store.list(), [])

    def test_unavailable_provider_is_explained(self):
        self.page.locator(".guide-settings-box summary").click()
        self.page.select_option("#guide-settings [name=provider]", "anthropic")
        self.page.once("dialog", lambda dialog: dialog.accept())
        self.page.click("#guide-settings button[type=submit]")
        expect(self.page.locator(".toast").last).to_contain_text("Assistant settings saved")
        if self.service.assistant.status()["mode"] == "guide":  # no API key on this machine
            expect(self.page.locator("#guide-mode")).to_contain_text("Built-in guide")
            reply = self.say("Is my house actually protected?")
            expect(reply).to_contain_text("can't promise protection")
        self.service.assistant.provider()  # settings applied without restart
        self.page.request.put(f"http://127.0.0.1:{self.port}/api/v1/assistant/settings", data=json.dumps({"provider": "none"}),
                              headers={"Content-Type": "application/json"})

    def test_phone_layout(self):
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.say("Is my house actually protected?")
        self.assertLessEqual(self.page.evaluate("document.scrollingElement.scrollWidth - window.innerWidth"), 0)
        expect(self.page.locator("#guide-input")).to_be_visible()


if __name__ == "__main__":
    unittest.main()
