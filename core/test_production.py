"""Production plumbing: the background job queue (retries, locking, background bots), file storage and error
reporting settings, and the CI workflow."""
import datetime as dt
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from accounts.models import Workspace
from core.test_deep import INTEG_BLUEPRINT

User = get_user_model()


class JobQueueTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        from flows import engine
        from flows.blueprint import build_workflow

        self.owner = User.objects.get(username="owner")
        self.ws = Workspace.objects.get(slug="demo")
        self.wf, _ = build_workflow(self.ws, self.owner, INTEG_BLUEPRINT, publish=True)
        self.packet = engine.start_packet(self.wf, user=self.owner, source="api",
                                          first_assignee={"email": "owner@paperlane.test", "name": "Owner", "user": self.owner})

    def bot(self, **cfg):
        return {"key": "b1", "bot_type": "add_comment", "name": "Note", "trigger": "flow_complete", "step_key": "",
                "config": {"text": "Hello", **cfg}, "condition": {}}

    def test_background_bot_is_queued_then_run_by_the_worker(self):
        from flows import jobs
        from flows.bots import run_bot
        from flows.models import BotJob

        log = run_bot(self.packet, self.bot(background=True), "flow_complete")
        self.assertEqual(log.message, "Queued to run in the background")
        self.assertEqual(BotJob.objects.filter(done=False).count(), 1)
        with mock.patch("flows.bots.HANDLERS", {"add_comment": lambda p, c, x: "done"}):
            self.assertEqual(jobs.process(), {"ran": 1, "retrying": 0, "failed": 0})
        self.assertFalse(BotJob.objects.filter(done=False).exists())

    def test_routing_bots_never_go_to_the_background(self):
        from flows.bots import run_bot

        bot = {**self.bot(background=True), "bot_type": "route_to_step"}
        with mock.patch("flows.bots.HANDLERS", {"route_to_step": lambda p, c, x: "routed"}):
            log = run_bot(self.packet, bot, "flow_complete")
        self.assertEqual(log.message, "routed")

    def test_failed_jobs_retry_with_backoff_then_stop(self):
        from flows import jobs

        job = jobs.enqueue(self.packet, self.bot(), "flow_complete", {})

        def boom(p, c, x):
            raise RuntimeError("provider down")

        with mock.patch("flows.bots.HANDLERS", {"add_comment": boom}):
            for expect in (1, 5, 15):
                self.assertEqual(jobs.process(now=timezone.now() + dt.timedelta(days=1))["retrying"], 1)
                job.refresh_from_db()
                self.assertFalse(job.done)
                self.assertIn("provider down", job.last_error)
                self.assertAlmostEqual((job.run_at - timezone.now()).total_seconds() / 60, expect, delta=0.5)
            self.assertEqual(jobs.process(now=timezone.now() + dt.timedelta(days=1))["failed"], 1)
        job.refresh_from_db()
        self.assertTrue(job.done)
        self.assertEqual(job.attempts, 4)

    def test_a_locked_job_is_not_taken_twice(self):
        from flows import jobs

        job = jobs.enqueue(self.packet, self.bot(), "flow_complete", {})
        job.locked_until = timezone.now() + dt.timedelta(minutes=5)
        job.save()
        self.assertEqual(jobs.process(), {"ran": 0, "retrying": 0, "failed": 0})
