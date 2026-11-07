"""Browser tests: a real Chromium (Playwright) against a live test server — the builder's tools, a recipient filling a
document, and no JavaScript errors on the main pages. Skipped when Playwright isn't installed
(pip install playwright && python -m playwright install chromium)."""
import os
import unittest

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.management import call_command

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

User = get_user_model()


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class BrowserTests(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"  # Playwright's sync API runs an event loop in this thread
        super().setUpClass()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as exc:  # pragma: no cover - browser not downloaded
            cls.pw.stop()
            super().tearDownClass()
            raise unittest.SkipTest(f"Chromium is not available: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        super().tearDownClass()

    def setUp(self):
        from accounts.models import Workspace
        from core.test_deep import INTEG_BLUEPRINT
        from flows.blueprint import build_workflow

        call_command("seed_demo", verbosity=0)
        self.owner = User.objects.get(username="owner")
        self.ws = Workspace.objects.get(slug="demo")
        self.wf, _ = build_workflow(self.ws, self.owner, INTEG_BLUEPRINT, publish=True)
        self.ctx = self.browser.new_context(viewport={"width": 1440, "height": 900})
        self.errors = []
        self.page = self.ctx.new_page()
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.set_default_timeout(15000)

    def tearDown(self):
        self.ctx.close()

    def login(self):
        self.client.force_login(self.owner)
        cookie = self.client.cookies[settings.SESSION_COOKIE_NAME]
        self.ctx.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": cookie.value, "url": self.live_server_url}])

    def go(self, path):
        self.page.goto(self.live_server_url + path)
        self.page.wait_for_load_state("networkidle")

    def test_main_pages_have_no_javascript_errors(self):
        self.login()
        for path in ("/", "/documents/", "/workflows/", f"/workflows/{self.wf.pk}/", f"/workflows/{self.wf.pk}/builder/",
                     "/documents/key-dates/", "/help/", "/workspace/ai/"):
            self.go(path)
        self.assertEqual(self.errors, [])

    def test_builder_find_checks_and_undo(self):
        self.login()
        self.go(f"/workflows/{self.wf.pk}/builder/")
        steps = self.page.locator("[data-ai-before] [data-step]")
        before = steps.count()
        first_name = self.page.locator("[data-ai-before] [data-step] .t > b").first.inner_text()
        # find a step
        self.page.keyboard.press("/")
        self.page.keyboard.type(first_name[:4])
        self.assertGreaterEqual(self.page.locator("#step-find-list [data-find]").count(), 1)
        self.page.keyboard.press("Escape")
        # checks panel opens
        self.page.click(".chk-btn")
        self.page.wait_for_selector("#drawer.open .drawer-head h2")
        self.page.keyboard.press("Escape")
        if self.page.locator("#drawer.open").count():
            self.page.locator("#drawer.open [data-close]").first.click()
        # duplicate the first step, then undo with the keyboard
        with self.page.expect_navigation():
            self.page.evaluate("document.querySelector('[data-ai-before] [data-step] form[action$=\"/duplicate/\"]').submit()")
        self.assertEqual(steps.count(), before + 1)
        self.page.locator(".builder-canvas").click(position={"x": 30, "y": 30})
        with self.page.expect_navigation():
            self.page.keyboard.press("Meta+z" if os.uname().sysname == "Darwin" else "Control+z")
        names = self.page.locator("[data-ai-before] [data-step] .t > b").all_inner_texts()
        self.assertEqual(len(names), before, (names, self.page.url))
        self.assertEqual(self.errors, [])

    def test_recipient_fills_and_completes_a_step(self):
        from flows import engine
        from flows.models import StepRun

        p = engine.start_packet(self.wf, user=self.owner, source="api",
                                first_assignee={"email": "client@example.com", "name": "Client"})
        run = p.runs.get(status__in=[StepRun.PENDING, StepRun.OPENED])
        self.go(run.fill_url())
        # fill every visible field (the server checks which are required), pick the first option of each choice, complete
        values = {"email": "client@example.com", "number": "250", "date": "2026-12-01", "tel": "5551234"}
        for el in self.page.locator("form input:visible, form textarea:visible").all():
            kind = (el.get_attribute("type") or "text").lower()
            if el.get_attribute("name") in (None, "reason"):
                continue
            if kind in ("radio", "checkbox"):
                group = el.get_attribute("name")
                if not self.page.locator(f'input[name="{group}"]:checked').count():
                    el.check()
            elif kind in ("text", "email", "number", "date", "tel") or el.evaluate("e => e.tagName") == "TEXTAREA":
                el.fill(values.get(kind, "Test value"))
        self.page.locator('button[name=action][value=submit]').click()
        self.page.wait_for_load_state("networkidle")
        run.refresh_from_db()
        self.assertEqual(run.status, StepRun.COMPLETED)
        self.assertEqual(self.errors, [])
