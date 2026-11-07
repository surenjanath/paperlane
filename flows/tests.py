from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings

from accounts.models import Workspace

User = get_user_model()

BP = {
    "name": "Queue test",
    "documents": [{"key": "d", "name": "Request", "fields": [
        {"id": "pol", "type": "text", "label": "Policy Number", "required": True, "filled_by": ["req"]},
        {"id": "note", "type": "text", "label": "Note"}]}],
    "steps": [{"key": "req", "name": "Requester", "recipient": {"type": "anyone"}},
              {"key": "team", "name": "Front office", "recipient": {"type": "group", "group": "Front office",
                                                                    "members": ["sam@paperlane.test", "cal@paperlane.test"]}}],
}


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class QueueAndDuplicateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        from flows.blueprint import build_workflow
        cache.clear()
        self.owner, self.sam, self.cal = (User.objects.get(username=u) for u in ("owner", "sam", "cal"))
        self.ws = Workspace.objects.get(slug="demo")
        self.wf, problems = build_workflow(self.ws, self.owner, BP, publish=True)
        self.assertEqual(problems, [])

    def start(self, policy="P-1"):
        from flows import engine
        from flows.mcp import Server
        p = engine.start_packet(self.wf, user=self.owner, source="api",
                                first_assignee={"email": "owner@paperlane.test", "name": "Owner", "user": self.owner})
        Server(self.ws, self.owner).t_complete_step(p.pk, values={"Policy Number": policy})
        p.refresh_from_db()
        return p

    def group_run(self, p):
        return p.runs.get(step_name="Front office", status__in=["pending", "opened"])

    def test_claim_hides_from_teammates_and_blocks_them(self):
        from core.queries import my_runs
        p = self.start()
        run = self.group_run(p)
        self.assertTrue(my_runs(self.cal, self.ws).filter(pk=run.pk).exists())
        self.client.force_login(self.sam)
        self.client.post(f"/f/{run.token}/", {"action": "claim"})
        run.refresh_from_db()
        self.assertEqual(run.claimed_by, self.sam)
        self.assertFalse(my_runs(self.cal, self.ws).filter(pk=run.pk).exists())  # gone from Cal's queue
        self.assertTrue(my_runs(self.sam, self.ws).filter(pk=run.pk).exists())
        self.client.force_login(self.cal)
        r = self.client.get(f"/f/{run.token}/")
        self.assertContains(r, "is working on this", status_code=409)
        self.assertNotContains(r, "Take over", status_code=409)
        # a supervisor/owner can take over
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(f"/f/{run.token}/"), "Take over", status_code=409)
        self.client.post(f"/f/{run.token}/", {"action": "takeover"})
        run.refresh_from_db()
        self.assertEqual(run.claimed_by, self.owner)

    def test_release_and_auto_claim_on_draft(self):
        p = self.start()
        run = self.group_run(p)
        self.client.force_login(self.cal)
        self.client.post(f"/f/{run.token}/", {"action": "draft"})
        run.refresh_from_db()
        self.assertEqual(run.claimed_by, self.cal)  # saving work takes the task
        self.client.post(f"/f/{run.token}/", {"action": "release"})
        run.refresh_from_db()
        self.assertIsNone(run.claimed_by)

    def test_workload_page_and_bulk_reassign(self):
        p = self.start()
        run = self.group_run(p)
        self.client.force_login(self.owner)
        r = self.client.get("/team/workload/")
        self.assertContains(r, "Front office")
        self.client.post("/team/workload/", {"action": "reassign", "target": f"u:{self.cal.pk}", "run": [run.pk]})
        run.refresh_from_db()
        self.assertEqual((run.assignee_user, run.assignee_group), (self.cal, None))
        self.client.force_login(User.objects.get(username="fay"))
        self.assertEqual(self.client.get("/team/workload/").status_code, 403)
        self.client.force_login(User.objects.get(username="ana"))  # auditor: can look, can't change
        self.assertEqual(self.client.get("/team/workload/").status_code, 200)
        self.assertEqual(self.client.post("/team/workload/", {"action": "release", "run": [run.pk]}).status_code, 403)

    def test_duplicate_check(self):
        self.wf.options = {"duplicates": {"fields": ["Policy Number"], "days": 0, "notify": True}}
        self.wf.save()
        first = self.start("POL-1")
        self.assertFalse(first.flags.get("duplicates"))
        second = self.start(" pol-1 ")  # case and spaces don't matter
        self.assertEqual(second.flags["duplicates"][0]["id"], first.pk)
        self.assertIn("#Possible duplicate", set(second.tags.values_list("name", flat=True)))
        self.assertIn("This may duplicate", second.comments.get().text)
        from flows.engine import condition_context
        self.assertEqual(condition_context(second)["@duplicate"], "yes")
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(f"/documents/{second.pk}/"), "Possible duplicate")
        other = self.start("POL-2")
        self.assertFalse(other.flags.get("duplicates"))
        # settings modal saves
        self.client.post(f"/workflows/{self.wf.pk}/action/duplicates/", {"fields": "", "days": "0"})
        self.wf.refresh_from_db()
        self.assertNotIn("duplicates", self.wf.options)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class BrandingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        self.owner = User.objects.get(username="owner")
        self.ws = Workspace.objects.get(slug="demo")
        self.client.force_login(self.owner)

    def test_html_email_with_button_sender_and_cc(self):
        from django.core import mail
        from flows import mailer
        self.client.post("/workspace/email-templates/?key=invite", {"key": "invite", "action": "design", "from_name": "Acme Service",
                         "reply_to": "service@example.test", "header_color": "#003366", "header_text": "#ffffff",
                         "button_color": "#cc0000", "button_text": "#ffffff", "default_button": "Open", "footer": "1 Main St",
                         "html": "on", "show_logo": "on", "security_note": "on"})
        self.client.post("/workspace/email-templates/?key=invite", {"key": "invite", "action": "save", "enabled": "on",
                         "subject": "Hi {user_name}", "body": "Please sign.\n\nOpen documents: {link}", "cc": "boss@example.test",
                         "bcc": "", "button_label": "Review now"})
        mail.outbox.clear()
        self.ws.refresh_from_db()
        mailer.send(self.ws, "x@y.test", "invite", {"user_name": "Jo", "link": "https://app.test/f/abc/"})
        m = mail.outbox[0]
        self.assertEqual(m.subject, "Hi Jo")
        self.assertIn("Acme Service", m.from_email)
        self.assertEqual(m.cc, ["boss@example.test"])
        self.assertEqual(m.reply_to, ["service@example.test"])
        html = m.alternatives[0][0]
        self.assertIn("Review now", html)
        self.assertIn('href="https://app.test/f/abc/"', html)
        self.assertIn("#cc0000", html)
        self.assertIn("1 Main St", m.body)

    def test_switched_off_email_is_not_sent(self):
        from django.core import mail
        from flows import mailer
        from flows.models import OutboundEmail
        self.client.post("/workspace/email-templates/?key=opened", {"key": "opened", "action": "toggle"})
        mail.outbox.clear()
        self.assertFalse(mailer.send(self.ws, "x@y.test", "opened", {"actor_name": "A", "document_name": "D", "link": "/"}))
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(OutboundEmail.objects.filter(kind="opened:off").count(), 1)
        self.assertContains(self.client.get("/workspace/email-templates/?key=opened"), "Off")

    def test_email_preview_and_test_send(self):
        from django.core import mail
        r = self.client.post("/workspace/email-templates/?key=invite", {"key": "invite", "action": "preview",
                             "subject": "S {user_name}", "body": "Go: {link}", "button_label": ""})
        self.assertIn("<table", r.json()["html"])
        mail.outbox.clear()
        self.client.post("/workspace/email-templates/?key=invite", {"key": "invite", "action": "test"})
        self.assertEqual(mail.outbox[0].to, [self.owner.email])

    def test_theme_and_thank_you(self):
        self.client.post("/workspace/theme/", {"action": "save", "preset": "ocean", "primary": "#0b5cad", "accent": "#0b5cad",
                         "button_text": "#ffffff", "page_bg": "#eef4fb", "header_bg": "#0b3d73", "header_text": "#ffffff",
                         "font": "Lato", "radius": "pill", "field_style": "filled", "show_logo": "on", "show_name": "on"})
        self.ws.refresh_from_db()
        self.assertEqual((self.ws.theme["header_bg"], self.ws.theme["radius"], self.ws.theme["font"]), ("#0b3d73", "pill", "Lato"))
        self.client.post("/workspace/theme/", {"action": "save", "primary": "red; }</style><script>", "font": "Evil"})
        self.ws.refresh_from_db()
        self.assertEqual(self.ws.theme["primary"], "#0b5cad")  # invalid colours/fonts are ignored
        r = self.client.post("/workspace/thank-you/", {"action": "save", "title": "Thanks {document_name}", "body": "Ref {reference}",
                             "icon": "✅", "show_reference": "on", "next_steps": "We review it\n- We pay you", "button_url": "javascript:x"})
        self.ws.refresh_from_db()
        self.assertNotEqual(self.ws.thank_you.get("button_url"), "javascript:x")  # rejected
        self.client.post("/workspace/thank-you/", {"action": "save", "title": "Thanks {document_name}", "body": "Ref {reference}",
                         "icon": "✅", "show_reference": "on", "next_steps": "We review it\n- We pay you", "confetti": "on"})
        from flows.blueprint import build_workflow
        from flows import engine
        wf, _ = build_workflow(self.ws, self.owner, {"name": "T", "documents": [{"key": "d", "name": "D", "fields": [
            {"id": "a", "type": "text", "label": "A", "filled_by": ["s"]}]}], "steps": [{"key": "s", "name": "S", "recipient": {"type": "anyone"}}]},
            publish=True)
        p = engine.start_packet(wf, user=self.owner, first_assignee={"email": self.owner.email, "name": "O", "user": self.owner})
        run = p.runs.get()
        self.client.post(f"/f/{run.token}/", {"action": "submit", "consent": "on"})
        r = self.client.get(f"/f/{run.token}/done/")
        self.assertContains(r, f"Thanks {p.name}")
        self.assertContains(r, f"Your reference: <b>#{p.pk}</b>")
        self.assertContains(r, "<li>We pay you</li>")
        self.assertContains(r, "createElement(\"canvas\")")


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class MentionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def test_mentions_notify_and_render(self):
        from django.core import mail
        from core.models import Notification
        from flows.mentions import resolve
        from flows.models import Packet
        ws = Workspace.objects.get(slug="demo")
        owner, sam = User.objects.get(username="owner"), User.objects.get(username="sam")
        self.assertEqual(resolve('Ping @sam and @"Cal Creator" and @nobody and @fay@paperlane.test', ws),
                         [sam, User.objects.get(username="cal"), User.objects.get(username="fay")])
        p = Packet.objects.filter(workspace=ws).first()
        self.client.force_login(owner)
        mail.outbox.clear()
        self.client.post(f"/documents/{p.pk}/action/comment/", {"text": "Can you check the ID copy @sam?"})
        self.assertTrue(Notification.objects.filter(user=sam, title__contains="mentioned you").exists())
        self.assertTrue(any("mentioned you" in m.subject for m in mail.outbox))
        r = self.client.get(f"/documents/{p.pk}/?tab=comments")
        self.assertContains(r, '<span class="mention">@sam</span>')
        self.assertContains(r, 'id="mention-data"')


BOT_BP = {
    "name": "Bot lab",
    "documents": [{"key": "d", "name": "Request", "fields": [
        {"id": "pol", "type": "text", "label": "Policy Number", "required": True, "filled_by": ["req"]},
        {"id": "first", "type": "text", "label": "First Name", "filled_by": ["req"]},
        {"id": "last", "type": "text", "label": "Last Name", "filled_by": ["req"]},
        {"id": "pay", "type": "date", "label": "Payment Date", "filled_by": ["req"]},
        {"id": "ref", "type": "text", "label": "Reference"}, {"id": "full", "type": "text", "label": "Full Name"},
        {"id": "due", "type": "text", "label": "Due Date"}]}],
    "steps": [{"key": "req", "name": "Requester", "recipient": {"type": "anyone"}},
              {"key": "team", "name": "Processing", "recipient": {"type": "group", "group": "Processing",
                                                                  "members": ["sam@paperlane.test", "cal@paperlane.test"]}}],
}


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class NewBotTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        from flows.blueprint import build_workflow
        self.owner = User.objects.get(username="owner")
        self.ws = Workspace.objects.get(slug="demo")
        self.wf, problems = build_workflow(self.ws, self.owner, BOT_BP, publish=True)
        self.assertEqual(problems, [])
        self.steps = {s.name: s.key for s in self.wf.ordered_steps()}

    def bot(self, bot_type, config, trigger="step_complete", step="Requester"):
        from flows import services
        from flows.models import Bot
        Bot.objects.create(workflow=self.wf, name=bot_type, bot_type=bot_type, trigger=trigger,
                           step_key=self.steps.get(step, ""), config=config)
        services.publish(self.wf, self.owner)

    def start(self, **vals):
        from flows import engine
        from flows.mcp import Server
        p = engine.start_packet(self.wf, user=self.owner, source="api",
                                first_assignee={"email": "owner@paperlane.test", "name": "Owner", "user": self.owner})
        Server(self.ws, self.owner).t_complete_step(p.pk, values={"Policy Number": "POL-1", "First Name": " jordan ",
                                                                  "Last Name": "SAMPLE", "Payment Date": "2026-10-16", **vals})
        p.refresh_from_db()
        return p

    def log(self, p, name):
        return p.bot_logs.get(bot_name=name)

    def test_reference_numbers_are_sequential(self):
        self.bot("reference_number", {"target": "Reference", "pattern": "WD-{yyyy}-{seq:4}", "counter": "wd", "rename": True})
        a, b = self.start(), self.start()
        from django.utils import timezone
        y = timezone.localdate().year
        self.assertEqual(a.flat_values()["Reference"], f"WD-{y}-0001")
        self.assertEqual(b.flat_values()["Reference"], f"WD-{y}-0002")
        self.assertTrue(b.name.startswith(f"WD-{y}-0002"))

    def test_transform_field(self):
        self.bot("transform_field", {"target": "Full Name", "operation": "template", "source": "{field:First Name} {field:Last Name}"})
        self.bot("transform_field", {"target": "Due Date", "operation": "add_business_days", "source": "Payment Date", "amount": "2"})
        p = self.start()
        self.assertEqual(p.flat_values()["Full Name"], "jordan SAMPLE")
        self.assertEqual(p.flat_values()["Due Date"], "2026-10-20")  # Fri 16 Oct + 2 business days = Tue 20 Oct

    def test_round_robin_assigns_least_busy_member(self):
        self.bot("assign_round_robin", {"strategy": "least"}, trigger="step_start", step="Processing")
        p = self.start()
        run = p.runs.get(step_name="Processing")
        self.assertIn(run.claimed_by.username, ("sam", "cal"))
        first = run.claimed_by
        q = self.start()
        self.assertNotEqual(q.runs.get(step_name="Processing").claimed_by, first)  # the other one has less work now

    def test_round_robin_rotates(self):
        self.bot("assign_round_robin", {"strategy": "rotate"}, trigger="step_start", step="Processing")
        holders = [self.start().runs.get(step_name="Processing").claimed_by.username for _ in range(3)]
        self.assertEqual(holders[0], holders[2])
        self.assertNotEqual(holders[0], holders[1])

    def test_start_workflow_chains_with_mapping(self):
        from flows.models import Packet
        self.bot("start_workflow", {"workflow": self.wf.pk, "mapping": "Policy Number = {field:Policy Number}", "link_back": True,
                                    "name": "Follow-up {field:Policy Number}"})
        p = self.start()
        child = Packet.objects.exclude(pk=p.pk).get(name="Follow-up POL-1")
        self.assertEqual(child.flat_values()["Policy Number"], "POL-1")
        self.assertIn(f"#{child.pk}", p.comments.get().text)
        self.assertEqual(Packet.objects.filter(workflow=self.wf).count(), 2)
        from flows import bots
        token = bots._CHAIN.set(frozenset({self.wf.pk}))  # as if we were already inside a chain started from this workflow
        try:
            with self.assertRaisesMessage(RuntimeError, "loop"):
                bots.h_start_workflow(child, {"workflow": self.wf.pk}, {})
        finally:
            bots._CHAIN.reset(token)

    def test_deadline_comment_notify_and_chat(self):
        from unittest import mock
        from core.models import Notification
        self.bot("set_deadline", {"date_field": "Payment Date"}, trigger="step_start", step="Processing")
        self.bot("add_comment", {"text": "Policy {field:Policy Number} received — {link}"})
        self.bot("notify_team", {"to": "sam@paperlane.test", "title": "New: {document_name}"})
        self.bot("post_chat", {"url": "https://chat.example/hook", "message": "New {field:Policy Number}"})
        with mock.patch("flows.bots._post_json", return_value=200) as post:
            p = self.start()
        run = p.runs.get(step_name="Processing")
        self.assertEqual(run.due_at.astimezone().strftime("%Y-%m-%d %H:%M"), "2026-10-16 17:00")
        self.assertIn("Policy POL-1 received — http", p.comments.get().text)
        self.assertTrue(Notification.objects.filter(user__username="sam", title__startswith="New:").exists())
        self.assertEqual(post.call_args[0][1], {"text": "New POL-1"})
        self.assertIn("HTTP 200", self.log(p, "post_chat").message)

    def test_new_bots_render_in_the_bot_form(self):
        self.client.force_login(self.owner)
        for t in ("reference_number", "assign_round_robin", "transform_field", "start_workflow", "set_deadline", "post_chat",
                  "add_comment", "notify_team"):
            r = self.client.get(f"/workflows/{self.wf.pk}/bots/new/?type={t}")
            self.assertEqual(r.status_code, 200, t)


class SeedStoriesTests(TestCase):
    """The story seed builds five clean workflows with documents at every stage and never in the future."""

    def test_stories(self):
        import io

        from django.core.management import call_command
        from django.utils import timezone

        from flows import checks
        from flows.models import Packet, Workflow

        call_command("seed_demo", verbosity=0, stdout=io.StringIO())
        call_command("seed_stories", stdout=io.StringIO())
        names = ["Expense claim", "Leave request", "Vendor onboarding", "Insurance claim", "Purchase request"]
        wfs = Workflow.objects.live().filter(name__in=names)
        self.assertEqual(wfs.count(), 5)
        for wf in wfs:
            self.assertFalse([i for i in checks.run(wf) if i["level"] == "error"], wf.name)
        docs = Packet.objects.filter(workflow__in=wfs)
        statuses = set(docs.values_list("status", flat=True))
        self.assertTrue({"completed", "in_progress", "declined"} <= statuses)
        self.assertFalse(docs.filter(created_at__gt=timezone.now()).exists())
        call_command("seed_stories", stdout=io.StringIO())  # idempotent without --replace
        self.assertEqual(Packet.objects.filter(workflow__in=wfs).count(), docs.count())


class AssigneeNameTests(TestCase):
    """A step sent to a teammate's address shows their name; an outsider (even a user of another workspace) keeps the address."""

    def test_member_named_outsider_not(self):
        import io

        from django.contrib.auth import get_user_model
        from django.core.management import call_command

        from accounts.models import Workspace
        from flows import engine
        from flows.blueprint import build_workflow

        call_command("seed_demo", verbosity=0, stdout=io.StringIO())
        ws = Workspace.objects.get(slug="demo")
        owner = get_user_model().objects.get(username="owner")
        get_user_model().objects.create_user("stranger", "stranger@elsewhere.test", "x", first_name="Secret", last_name="Name")
        spec = {"name": "Names", "documents": [{"key": "d", "name": "D", "fields": [{"id": "a", "type": "text", "label": "A", "filled_by": ["one"]}]}],
                "steps": [{"key": "one", "name": "One", "recipient": {"type": "email", "email": "sam@paperlane.test, stranger@elsewhere.test"}}]}
        wf, _ = build_workflow(ws, owner, spec, publish=True, strict=False)
        p = engine.start_packet(wf, user=owner)
        names = {r.assignee_email: r.assignee_name for r in p.runs.all()}
        self.assertEqual(names["sam@paperlane.test"], "Sam Supervisor")
        self.assertEqual(names["stranger@elsewhere.test"], "stranger@elsewhere.test")
