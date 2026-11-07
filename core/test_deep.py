"""Exhaustive route sweep: every URL pattern × every role × GET and POST.

Each request runs inside a rolled-back savepoint so destructive actions cannot affect the next route.
Asserts no request crashes (5xx) and that role restrictions hold for management pages.
"""
import os
import tempfile

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import URLResolver, get_resolver, reverse

from accounts.models import Contact, Group, Invitation, Membership, Workspace
from core.models import DashboardWidget, Notification
from flows.models import Bot, DataSheet, Packet, StepRun, Workflow

User = get_user_model()


class _Rollback(Exception):
    """Raised to undo whatever a single swept request changed."""

ACTIONS = {
    "flows:builder_history": ["undo", "redo", "bogus"],
    "flows:connector_action": ["test", "remove_action", "bogus", "delete"],
    "accounts:security_action": ["2fa-start", "2fa-cancel", "2fa-confirm", "2fa-disable", "2fa-codes", "sign-out-session",
                                 "sign-out-others", "bogus"],
    "api_item_action": ["publish", "start", "complete", "submit", "bogus"],
    "api_named": ["Policies", "list_team"],
    "flows:workflow_action": ["duplicate", "tags", "archive", "trash", "hide", "unhide", "restore", "delete", "rename",
                              "link", "regenerate_link", "welcome", "consent", "access", "restore_version", "bogus"],
    "flows:step_action": ["delete", "up", "down", "duplicate", "rename", "save_template", "bogus"],
    "flows:doc_action": ["rename", "copy", "remove", "condition", "up", "down", "bogus"],
    "flows:bot_action": ["delete", "toggle", "bogus"],
    "flows:packet_action": ["revise", "rename", "lock", "tags", "archive", "trash", "restore", "delete", "cancel",
                            "resend", "remind", "reassign", "comment", "hold", "bogus"],
    "accounts:team_member": ["role", "block", "activate", "remove", "groups", "bogus"],
    "flows:create": ["workflow", "template", "bogus"],
    "flows:create_preset": ["employee_onboarding", "bogus"],
    "core:page": ["release-notes", "terms", "privacy", "bogus"],
    "flows:form_action": ["publish", "unpublish", "duplicate", "regenerate_link", "archive", "delete", "bogus"],
}
SKIP = {"accounts:logout", "None"}  # logout ends the session mid-sweep; static media handler

# GET pages a Filler must not be able to open (management / configuration surfaces).
MANAGER_ONLY_GET = [
    "accounts:billing", "accounts:subscription", "accounts:usage", "accounts:workspace_settings", "accounts:theme",
    "accounts:email_templates", "accounts:domains", "accounts:email_log", "core:audit", "flows:builder",
    "flows:bots", "flows:bot_catalog", "flows:editor", "flows:workflow_audit",
]


def iter_routes():
    def walk(patterns, prefix_ns=""):
        for p in patterns:
            if isinstance(p, URLResolver):
                if p.namespace == "admin":
                    continue
                yield from walk(p.url_patterns, (p.namespace + ":") if p.namespace else prefix_ns)
            else:
                name = f"{prefix_ns}{p.name}" if p.name else "None"
                yield name, list(p.pattern.converters.keys())
    yield from walk(get_resolver().url_patterns)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class RouteSweepTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)
        cls.ws = Workspace.objects.get(slug="demo")
        cls.users = {u: User.objects.get(username=u) for u in ["owner", "sam", "ana", "cal", "fay"]}

    def samples(self):
        ws = self.ws
        wf = Workflow.objects.filter(workspace=ws, kind="workflow", trashed_at__isnull=True).first()
        packet = Packet.objects.filter(workspace=ws).first()
        run = StepRun.objects.filter(packet__workspace=ws).exclude(token="").first()
        rev = packet.revisions.first() if packet else None
        f = None
        try:
            from flows.models import PacketFile
            f = PacketFile.objects.filter(packet__workspace=ws).first()
        except ImportError:
            pass
        return {
            "wf": wf, "step": wf.steps.first(), "doc": wf.documents.first(), "bot": Bot.objects.filter(workflow=wf).first(),
            "packet": packet, "rev": rev, "run": run, "file": f,
            "contact": Contact.objects.filter(workspace=ws).first(),
            "member": Membership.objects.filter(workspace=ws, user__username="fay").first(),
            "invite": Invitation.objects.filter(workspace=ws).first(),
            "widget": DashboardWidget.objects.filter(workspace=ws).first(),
            "notif": Notification.objects.filter(user=self.users["owner"]).first(),
            "sheet": DataSheet.objects.filter(workspace=ws).first(),
            "form": self._form(ws),
        }

    def _form(self, ws):
        from flows.models import IntakeForm
        from flows.form_templates import TEMPLATES
        t = TEMPLATES["service_request"]
        return IntakeForm.objects.get_or_create(workspace=ws, name="Sweep form", defaults={
            "schema": {"fields": t["fields"]}, "options": t["options"], "status": "published",
            "lookups": [{"id": "lk1", "trigger": "policy", "source": "sheet", "sheet": "Policies", "key_column": "PolicyNumber", "map": []}]})[0]

    def build_urls(self):
        s = self.samples()
        wf = s["wf"]
        def pk(obj):
            return obj.pk if obj else 999999
        out = []
        for name, params in iter_routes():
            if name in SKIP:
                continue
            kw_sets = [{}]
            if params:
                kw = {}
                for p in params:
                    if p == "pk":
                        if name.startswith("flows:packet") or name in ("flows:document_send",):
                            kw[p] = pk(s["packet"]) if name != "flows:document_send" else pk(wf)
                        elif name == "accounts:switch":
                            kw[p] = self.ws.pk
                        elif name == "accounts:datasheet":
                            kw[p] = pk(s["sheet"])
                        elif name == "accounts:invite_cancel":
                            kw[p] = pk(s["invite"])
                        elif name in ("accounts:team_member", "accounts:team_member_panel"):
                            kw[p] = pk(s["member"])
                        elif name == "accounts:contact_panel":
                            kw[p] = pk(s["contact"])
                        elif name == "accounts:contact_edit":
                            kw[p] = pk(s["contact"])
                        elif name in ("core:widget_edit", "core:widget_data"):
                            kw[p] = pk(s["widget"])
                        elif name.startswith("flows:form_"):
                            kw[p] = pk(s.get("form"))
                        elif name == "core:notification_open":
                            kw[p] = pk(s["notif"])
                        else:
                            kw[p] = pk(wf)
                    elif p == "step_id":
                        kw[p] = pk(s["step"])
                    elif p == "doc_id":
                        kw[p] = pk(s["doc"])
                    elif p == "bot_id":
                        kw[p] = pk(s["bot"])
                    elif p == "number":
                        kw[p] = s["rev"].number if s["rev"] else 1
                    elif p == "file_id":
                        kw[p] = pk(s["file"])
                    elif p == "sub":  # a form submission; a missing one must 404, not crash
                        kw[p] = 999999
                    elif p == "idx":  # a one-tap decision choice
                        kw[p] = 0
                    elif p == "provider":
                        kw[p] = "google"
                    elif p == "token":
                        if name.startswith("flows:form_"):
                            kw[p] = s["form"].token
                        elif name.startswith("flows:fill"):
                            kw[p] = s["run"].token if s["run"] else "nope"
                        elif name == "flows:public_start":
                            kw[p] = wf.link_token
                        elif name == "flows:hook":
                            kw[p] = wf.hook_token
                        else:
                            kw[p] = "nope"
                    elif p in ("action", "kind", "key", "page"):
                        kw[p] = None
                    elif p == "resource":
                        kw[p] = "workflows"
                    elif p == "what":
                        kw[p] = "users"
                    elif p == "uidb64":
                        kw[p] = "MQ"  # password-reset link; the token below is invalid on purpose
                    elif p == "lookup_id":
                        kw[p] = "lk1"
                    elif p == "path":
                        kw[p] = "x"
                    elif p == "code":
                        kw[p] = "AAAA-BBBB-CCCC-DDDD"
                    elif p == "cat":
                        kw[p] = "workflows"
                    elif p == "slug":
                        kw[p] = "approvals"
                    elif p == "secret":
                        kw[p] = "not-the-secret"
                if "action" in kw or "kind" in kw or "key" in kw or "page" in kw:
                    slot = next(k for k in ("action", "kind", "key", "page") if k in kw)
                    kw_sets = [{**kw, slot: a} for a in ACTIONS.get(name, ["bogus"])]
                else:
                    kw_sets = [kw]
            for kw in kw_sets:
                try:
                    out.append((name, reverse(name, kwargs=kw) if kw else reverse(name)))
                except Exception as e:  # pragma: no cover - reported, not fatal
                    out.append((name, f"!reverse-failed {kw} {e}"))
        return out

    def sweep(self, client, label):
        rows = []
        for name, url in self.build_urls():
            if url.startswith("!"):
                rows.append((label, "REV", name, url, "-"))
                continue
            for method in ("GET", "POST"):
                code = None
                try:
                    with transaction.atomic():
                        try:
                            r = client.get(url) if method == "GET" else client.post(url, {})
                            code = r.status_code
                        except Exception as e:
                            code = f"EXC {type(e).__name__}: {str(e)[:120]}"
                        raise _Rollback
                except _Rollback:
                    pass
                rows.append((label, method, name, url, code))
        return rows

    def test_sweep_all_roles(self):
        rows = []
        for role, user in self.users.items():
            self.client.force_login(user)
            rows += self.sweep(self.client, role)
            self.client.logout()
        rows += self.sweep(self.client, "anon")
        report = os.environ.get("SWEEP_REPORT")
        if report:
            with open(report, "w") as fh:
                for r in rows:
                    fh.write("\t".join(str(x) for x in r) + "\n")
        crashes = [r for r in rows if not isinstance(r[4], int) or r[4] >= 500]
        self.assertEqual(crashes, [], "\n".join("\t".join(map(str, r)) for r in crashes[:60]))

    def test_filler_cannot_open_management_pages(self):
        self.client.force_login(self.users["fay"])
        urls = dict()
        for name, url in self.build_urls():
            if name in MANAGER_ONLY_GET and name not in urls:
                urls[name] = url
        leaks = []
        for name, url in urls.items():
            r = self.client.get(url)
            if r.status_code == 200:
                leaks.append(f"{name} {url}")
        self.assertEqual(leaks, [], "Filler can open: " + ", ".join(leaks))


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class RoleEnforcementTests(TestCase):
    """Mutations attempted by roles that may not perform them must leave the data untouched."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)
        cls.ws = Workspace.objects.get(slug="demo")

    def snapshot(self):
        from flows.models import Step
        return (
            list(Workflow.objects.values_list("pk", "name", "archived_at", "trashed_at", "is_hidden").order_by("pk")),
            list(Step.objects.values_list("pk", "name", "order").order_by("pk")),
            list(Bot.objects.values_list("pk", "enabled").order_by("pk")),
            list(Packet.objects.values_list("pk", "name", "status", "locked", "archived_at", "trashed_at").order_by("pk")),
            list(Membership.objects.values_list("pk", "role", "active").order_by("pk")),
            list(Contact.objects.values_list("pk", "name").order_by("pk")),
            list(Group.objects.values_list("pk", "name").order_by("pk")),
        )

    def attempts(self):
        wf = Workflow.objects.filter(workspace=self.ws, kind="workflow").first()
        step, bot = wf.steps.first(), Bot.objects.filter(workflow=wf).first()
        packet = Packet.objects.filter(workspace=self.ws).first()
        sam = Membership.objects.get(workspace=self.ws, user__username="sam")
        contact = Contact.objects.filter(workspace=self.ws).first()
        out = [
            (reverse("flows:workflow_action", args=[wf.pk, a]), {"name": "HACKED", "tags": "x"}) for a in ("rename", "archive", "trash", "hide", "delete")
        ] + [
            (reverse("flows:step_action", args=[wf.pk, step.pk, "delete"]), {}),
            (reverse("flows:step_action", args=[wf.pk, step.pk, "rename"]), {"name": "HACKED"}),
            (reverse("flows:publish", args=[wf.pk]), {}),
            (reverse("accounts:team_member", args=[sam.pk, "role"]), {"role": "filler"}),
            (reverse("accounts:team_member", args=[sam.pk, "block"]), {}),
            (reverse("accounts:team_member", args=[sam.pk, "remove"]), {}),
            (reverse("accounts:contact_delete"), {"ids": [contact.pk]}),
            (reverse("accounts:groups"), {"name": "HACKED"}),
            (reverse("core:empty_trash"), {}),
        ]
        if bot:
            out += [(reverse("flows:bot_action", args=[wf.pk, bot.pk, "toggle"]), {}),
                    (reverse("flows:bot_action", args=[wf.pk, bot.pk, "delete"]), {})]
        if packet:
            out += [(reverse("flows:packet_action", args=[packet.pk, a]), {"name": "HACKED"}) for a in ("rename", "lock", "archive", "trash", "delete", "cancel")]
        return out

    def assert_no_change(self, username, allowed=()):
        self.client.force_login(User.objects.get(username=username))
        for url, data in self.attempts():
            if any(a in url for a in allowed):
                continue
            before = self.snapshot()
            self.client.post(url, data)
            self.assertEqual(before, self.snapshot(), f"{username} changed data via POST {url}")

    def test_filler_cannot_mutate(self):
        self.assert_no_change("fay")

    def test_auditor_is_read_only(self):
        self.assert_no_change("ana")

    def test_creator_cannot_manage_team_or_others_workflows(self):
        # Creators may manage their own workflows/contacts; team administration stays owner/supervisor only.
        self.client.force_login(User.objects.get(username="cal"))
        sam = Membership.objects.get(workspace=self.ws, user__username="sam")
        for action, data in (("role", {"role": "filler"}), ("block", {}), ("remove", {})):
            before = self.snapshot()
            self.client.post(reverse("accounts:team_member", args=[sam.pk, action]), data)
            self.assertEqual(before, self.snapshot(), f"creator could {action} a teammate")


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   MEDIA_ROOT=tempfile.mkdtemp(prefix="paperlane-test-media-"))
class AttachmentResubmitTests(TestCase):
    """Files uploaded on a submit that fails validation must survive to the next submit."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def test_upload_survives_failed_validation(self):
        import json
        from django.core.files.uploadedfile import SimpleUploadedFile
        from flows.models import PacketFile
        c = self.client
        c.force_login(User.objects.get(username="owner"))
        c.post(reverse("flows:create", args=["workflow"]), {"name": "Upload check"})
        wf = Workflow.objects.get(name="Upload check")
        doc, step = wf.documents.first(), wf.steps.first()
        fields = [
            {"id": "att", "type": "attachment", "label": "Proof", "name": "Proof", "required": True, "role": step.key, "max_files": 5, "max_mb": 5},
            {"id": "pol", "type": "text", "label": "Policy", "name": "Policy", "required": True, "role": step.key},
        ]
        c.post(reverse("flows:editor_save", args=[wf.pk, doc.pk]), json.dumps({"schema": {"fields": fields}}), content_type="application/json")
        c.post(reverse("flows:step_settings", args=[wf.pk, step.pk]), {"name": "Claimant", "recipient_type": "current_user",
                                                                    f"access_{doc.key}": "fill", "rule_logic": "and"})
        c.post(reverse("flows:publish", args=[wf.pk]))
        c.post(reverse("flows:run", args=[wf.pk]), {"name": "Upload run"})
        run = Packet.objects.get(name="Upload run").runs.get()
        url, k = reverse("flows:fill", args=[run.token]), doc.key

        # 1st submit: file attached, required text missing → rejected, file shown again
        r = c.post(url, {f"f_{k}_att": SimpleUploadedFile("proof.pdf", b"%PDF-1.4 x", "application/pdf"), "action": "submit"})
        self.assertEqual(r.status_code, 400)
        pf = PacketFile.objects.get(packet=run.packet, field_id="att")
        self.assertContains(r, "proof.pdf", status_code=400)

        # 2nd submit: text filled, file NOT re-uploaded, keep-checkbox posted as the page renders it → completes
        r = c.post(url, {f"f_{k}_pol": "P-1", f"f_{k}_att__keep_present": "1", f"f_{k}_att__keep": str(pf.pk), "action": "submit"})
        self.assertEqual(r.status_code, 302, "resubmit without re-uploading was rejected")
        run.packet.refresh_from_db()
        self.assertEqual([x["file_id"] for x in run.packet.data[k]["att"]], [pf.pk])


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class BotConfigTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def test_saving_bot_form_keeps_hidden_config_and_skip_on_fail(self):
        owner = User.objects.get(username="owner")
        self.client.force_login(owner)
        wf = Workflow.objects.filter(kind="workflow").first()
        bot = Bot.objects.create(workflow=wf, bot_type="add_tags", name="Tagger", trigger="step_complete",
                                 config={"tags": "#A", "imported_target": "role-x"})
        self.client.post(reverse("flows:bot_edit", args=[wf.pk, bot.pk]),
                         {"name": "Tagger", "trigger": "step_complete", "enabled": "on", "cfg_tags": "#B", "skip_on_fail": "on"})
        bot.refresh_from_db()
        self.assertEqual(bot.config["tags"], "#B")
        self.assertEqual(bot.config["imported_target"], "role-x", "form save dropped a config key it does not show")
        self.assertTrue(bot.config["skip_on_fail"])

    def test_skip_on_fail_logs_skipped(self):
        from flows.bots import run_bot
        packet = Packet.objects.first()
        bad = {"key": "k1", "bot_type": "webhook", "name": "Broken hook", "config": {"url": "http://127.0.0.1:9/none", "skip_on_fail": True}}
        log = run_bot(packet, bad, "step_complete")
        self.assertEqual(log.status, "skipped")
        bad["config"]["skip_on_fail"] = False
        bad["key"] = "k2"
        self.assertEqual(run_bot(packet, bad, "step_complete").status, "failed")


class ConditionOperatorTests(TestCase):
    """Condition operators: like/regex/empty on text, after/until on dates, numeric revision, aggregates."""

    def test_operators(self):
        from flows.conditions import check_rule as c
        self.assertTrue(c({"field": "x", "op": "regex", "value": r"^QA-\d+$"}, {"x": "QA-0001"}))
        self.assertFalse(c({"field": "x", "op": "regex", "value": "(["}, {"x": "a"}))  # bad pattern never matches
        self.assertTrue(c({"field": "d", "op": "after", "value": "2026-01-01"}, {"d": "2026-10-14"}))
        self.assertTrue(c({"field": "d", "op": "until", "value": "2026-10-14"}, {"d": "2026-10-14"}))
        self.assertFalse(c({"field": "d", "op": "until", "value": "2026-10-13"}, {"d": "2026-10-14"}))
        self.assertTrue(c({"field": "d", "op": "eq", "value": "14/10/2026"}, {"d": "2026-10-14"}))
        self.assertTrue(c({"field": "t", "op": "after", "value": "09:00"}, {"t": "17:30"}))
        self.assertTrue(c({"field": "w", "op": "after", "value": "Monday"}, {"w": "Friday"}))
        self.assertTrue(c({"field": "m", "op": "eq", "value": "oct"}, {"m": "October"}))
        self.assertTrue(c({"field": "@revision", "op": "gte", "value": "2"}, {"@revision": 3}))
        self.assertTrue(c({"field": "@tag", "op": "eq", "value": "#VIP"}, {"@tag": ["#VIP", "#New"]}))
        self.assertTrue(c({"field": "@tag", "op": "neq", "value": "#Old"}, {"@tag": ["#VIP"]}))
        self.assertTrue(c({"field": "@all_required:d1", "op": "not_empty"}, {"@all_required:d1": ["a", "b"]}))
        self.assertFalse(c({"field": "@all_required:d1", "op": "not_empty"}, {"@all_required:d1": ["a", ""]}))
        self.assertTrue(c({"field": "@all_fillable:d1", "op": "empty"}, {"@all_fillable:d1": ["", None]}))


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ConditionTypeRoutingTests(TestCase):
    """Step conditions using the recipient / previous step / document data / filled-count types route the packet."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def test_step_conditions_use_context(self):
        import json
        from flows.models import Step
        c = self.client
        owner = User.objects.get(username="owner")
        Membership.objects.filter(user=owner).update(position="Underwriter")
        c.force_login(owner)
        c.post(reverse("flows:create", args=["workflow"]), {"name": "Ctx route"})
        wf = Workflow.objects.get(name="Ctx route")
        doc, s1 = wf.documents.first(), wf.steps.first()
        c.post(reverse("flows:editor_save", args=[wf.pk, doc.pk]), json.dumps({"schema": {"fields": [
            {"id": "a", "type": "text", "label": "A", "name": "A", "role": "anyone"},
            {"id": "b", "type": "text", "label": "B", "name": "B", "role": "anyone"}]}}), content_type="application/json")
        c.post(reverse("flows:step_settings", args=[wf.pk, s1.pk]), {"name": "First", "recipient_type": "current_user",
                                                                  f"access_{doc.key}": "fill", "rule_logic": "and"})
        def add(name, order, rules, logic="and"):
            return Step.objects.create(workflow=wf, name=name, order=order, recipient_email="sam@paperlane.test",
                                       condition={"logic": logic, "rules": rules})
        add("Underwriter review", 20, [{"field": "@user.position", "op": "eq", "value": "underwriter"},
                                       {"field": "@prev.status", "op": "eq", "value": "completed"}])
        add("Clerk review", 30, [{"field": "@user.position", "op": "eq", "value": "Clerk"}])
        add("Half filled", 40, [{"field": f"@filled:{doc.key}", "op": "eq", "value": "1"}])
        add("Revised", 50, [{"field": "@revision", "op": "gte", "value": "5"}])
        c.post(reverse("flows:publish", args=[wf.pk]))
        c.post(reverse("flows:run", args=[wf.pk]), {"name": "Ctx run"})
        packet = Packet.objects.get(name="Ctx run")
        run = packet.runs.get()
        r = c.post(reverse("flows:fill", args=[run.token]), {f"f_{doc.key}_a": "x", "action": "submit"})
        self.assertEqual(r.status_code, 302)
        packet.refresh_from_db()
        sent = list(packet.runs.order_by("id").values_list("step_name", flat=True))
        self.assertEqual(sent, ["First", "Underwriter review"], "recipient position + previous status routed wrong")
        # finish "Underwriter review" as sam → next: Clerk (no) → Half filled (1 field filled: yes)
        nxt = packet.runs.get(step_name="Underwriter review")
        c.force_login(User.objects.get(username="sam"))
        r = c.post(reverse("flows:fill", args=[nxt.token]), {f"f_{doc.key}_a": "x", "action": "submit", "consent": "on"})
        self.assertEqual(r.status_code, 302, r.content.decode()[:300] if r.status_code != 302 else "")
        packet.refresh_from_db()
        self.assertIn("Half filled", list(packet.runs.values_list("step_name", flat=True)))
        self.assertNotIn("Clerk review", list(packet.runs.values_list("step_name", flat=True)))

    def test_picker_offers_condition_types(self):
        self.client.force_login(User.objects.get(username="owner"))
        wf = Workflow.objects.filter(kind="workflow").first()
        r = self.client.get(reverse("flows:step_settings", args=[wf.pk, wf.steps.first().pk]))
        for label in ("When the recipient", "When the date", "When the previous step", "When document data",
                      "When the number of filled fields in", "@user.position", "@prev.status", "matches regex", "is after"):
            self.assertContains(r, label)


class AutostartScheduleTests(TestCase):
    """Due-time logic of the live "Scheduled start" options."""

    def at(self, s, tz="America/Port_of_Spain"):
        import datetime as dt
        import zoneinfo
        return dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=zoneinfo.ZoneInfo(tz))

    def test_once_multiple_schedule_and_stop(self):
        from flows.autostart import is_due, stopped
        once = {"type": "schedule", "repeat": "once", "start": "2026-10-20T09:00", "tz": "America/Port_of_Spain"}
        self.assertFalse(is_due(once, self.at("2026-10-20 08:59")))
        self.assertTrue(is_due(once, self.at("2026-10-20 09:00")))
        self.assertFalse(is_due({**once, "count": 1}, self.at("2026-10-21 09:00")))

        multi = {"type": "schedule", "repeat": "multiple", "every": 3, "unit": "hours", "start": "2026-10-20T09:00"}
        self.assertTrue(is_due(multi, self.at("2026-10-20 09:01")))
        ran = self.at("2026-10-20 09:01").isoformat()
        self.assertFalse(is_due({**multi, "last_run": ran}, self.at("2026-10-20 11:59")))
        self.assertTrue(is_due({**multi, "last_run": ran}, self.at("2026-10-20 12:02")))

        # Wed 21 Oct 2026; weekdays 0=Mon … 2=Wed
        weekly = {"type": "schedule", "repeat": "schedule", "schedule_type": "week", "weekdays": [2], "time": "10:00",
                  "months": list(range(1, 13)), "start": "2026-10-01T00:00"}
        self.assertFalse(is_due(weekly, self.at("2026-10-21 09:59")))
        self.assertTrue(is_due(weekly, self.at("2026-10-21 10:00")))
        self.assertFalse(is_due({**weekly, "last_run": self.at("2026-10-21 10:00").isoformat()}, self.at("2026-10-21 15:00")))
        self.assertFalse(is_due(weekly, self.at("2026-10-22 10:00")))  # Thursday
        self.assertFalse(is_due({**weekly, "months": [11]}, self.at("2026-10-21 10:00")))  # October excluded

        monthly = {"type": "schedule", "repeat": "schedule", "schedule_type": "month", "monthdays": [1, 15], "time": "08:00"}
        self.assertTrue(is_due(monthly, self.at("2026-10-15 08:30")))
        self.assertFalse(is_due(monthly, self.at("2026-10-16 08:30")))
        custom = {"type": "schedule", "repeat": "schedule", "schedule_type": "custom", "months": [10], "weekdays": [4],
                  "monthdays": [], "time": "07:00"}
        self.assertTrue(is_due(custom, self.at("2026-10-23 07:00")))  # Friday in October

        self.assertTrue(stopped({"stop": "count", "stop_count": 2, "count": 2}, self.at("2026-10-01 00:00")))
        self.assertFalse(stopped({"stop": "date", "stop_date": "2026-10-20"}, self.at("2026-10-20 23:00")))
        self.assertTrue(stopped({"stop": "date", "stop_date": "2026-10-20"}, self.at("2026-10-21 00:01")))
        self.assertFalse(is_due({**multi, "enabled": False}, self.at("2026-10-20 09:01")))
        # timezone: 10:00 in London is 05:00 in Port of Spain
        ldn = {**weekly, "tz": "Europe/London"}
        self.assertTrue(is_due(ldn, self.at("2026-10-21 05:00")))

    def test_legacy_format_is_converted(self):
        from flows.autostart import items
        wf = Workflow(autostart={"type": "schedule", "schedule": {"frequency": "weekly", "weekday": 2, "time": "10:00"}})
        (it,) = items(wf)
        self.assertEqual((it["repeat"], it["schedule_type"], it["weekdays"], it["time"]), ("schedule", "week", [2], "10:00"))
        self.assertEqual(items(Workflow(autostart={"type": "workflow", "source_workflow": 5}))[0]["source_workflow"], 5)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   MEDIA_ROOT=tempfile.mkdtemp(prefix="paperlane-test-media-"))
class AutostartFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def make_wf(self, name):
        import json
        c = self.client
        c.post(reverse("flows:create", args=["workflow"]), {"name": name})
        wf = Workflow.objects.get(name=name)
        doc, step = wf.documents.first(), wf.steps.first()
        c.post(reverse("flows:editor_save", args=[wf.pk, doc.pk]), json.dumps({"schema": {"fields": [
            {"id": "pol", "type": "text", "label": "Policy", "name": "Policy", "role": "anyone"}]}}), content_type="application/json")
        c.post(reverse("flows:step_settings", args=[wf.pk, step.pk]), {"name": "Clerk", "recipient_type": "email",
                                                                    "recipient_email": "cal@paperlane.test", f"access_{doc.key}": "fill", "rule_logic": "and"})
        c.post(reverse("flows:publish", args=[wf.pk]))
        wf.refresh_from_db()
        return wf, doc

    def setUp(self):
        self.client.force_login(User.objects.get(username="owner"))

    def test_drawer_save_validate_toggle_delete(self):
        wf, _ = self.make_wf("AS drawer")
        url = reverse("flows:autostart", args=[wf.pk])
        for q in ("", "?choose=1", "?tab=link", "?add=schedule", "?add=sheet", "?add=workflow", "?add=webhook&source=Salesforce"):
            self.assertEqual(self.client.get(url + q).status_code, 200, q)
        # weekly schedule without a weekday is rejected
        self.client.post(url, {"type": "schedule", "repeat": "schedule", "schedule_type": "week", "time": "10:00", "start": "2026-10-01T00:00"})
        wf.refresh_from_db()
        self.assertEqual(wf.autostart, {})
        self.client.post(url, {"type": "schedule", "repeat": "schedule", "schedule_type": "week", "weekdays": ["0", "4"],
                               "months": ["10", "11"], "time": "10:00", "start": "2026-10-01T00:00", "stop": "count", "stop_count": "5"})
        self.client.post(url, {"type": "webhook", "source": "Salesforce"})
        wf.refresh_from_db()
        its = wf.autostart["items"]
        self.assertEqual([i["type"] for i in its], ["schedule", "webhook"])
        self.assertEqual((its[0]["weekdays"], its[0]["months"], its[0]["stop_count"]), ([0, 4], [10, 11], 5))
        r = self.client.get(reverse("flows:builder", args=[wf.pk]))
        self.assertContains(r, "Mon, Fri at 10:00")
        self.assertContains(r, "Salesforce")
        self.client.post(url, {"action": "toggle", "item": its[0]["id"]})
        wf.refresh_from_db()
        self.assertFalse(wf.autostart["items"][0]["enabled"])
        self.client.post(url, {"action": "delete", "item": its[1]["id"]})
        wf.refresh_from_db()
        self.assertEqual(len(wf.autostart["items"]), 1)

    def test_excel_rows_start_prefilled_documents(self):
        import io
        from django.core.files.uploadedfile import SimpleUploadedFile
        from openpyxl import Workbook
        from flows.bots import tick
        wf, doc = self.make_wf("AS excel")
        book = Workbook()
        ws = book.active
        ws.append(["Policy", "document_name"])
        ws.append(["P-100", "Row one"])
        ws.append(["P-200", "Row two"])
        ws.append([None, None])
        buf = io.BytesIO()
        book.save(buf)
        up = SimpleUploadedFile("policies.xlsx", buf.getvalue(),
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.client.post(reverse("flows:autostart", args=[wf.pk]), {"type": "sheet", "repeat": "once",
                                                                  "start": "2020-01-01T00:00", "row_mode": "all", "sheet": up})
        wf.refresh_from_db()
        self.assertEqual(wf.autostart["items"][0]["rows"], 2)
        tick()
        made = Packet.objects.filter(workflow=wf).order_by("id")
        self.assertEqual([p.name for p in made], ["Row one", "Row two"])
        self.assertEqual(made[0].flat_values()["Policy"], "P-100")
        tick()  # "once" does not repeat
        self.assertEqual(Packet.objects.filter(workflow=wf).count(), 2)

    def test_next_row_mode_and_chaining(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from flows.bots import tick
        from flows import autostart as A
        wf, doc = self.make_wf("AS next row")
        csv_up = SimpleUploadedFile("p.csv", b"Policy\nA-1\nA-2\n", "text/csv")
        self.client.post(reverse("flows:autostart", args=[wf.pk]), {"type": "sheet", "repeat": "multiple", "every": "1",
                                                                  "unit": "minutes", "start": "2020-01-01T00:00",
                                                                  "row_mode": "next", "sheet": csv_up})
        import datetime as dt
        from django.utils import timezone as tz
        t0 = tz.now()
        tick(t0); tick(t0 + dt.timedelta(minutes=2)); tick(t0 + dt.timedelta(minutes=4))
        self.assertEqual(sorted(p.flat_values()["Policy"] for p in Packet.objects.filter(workflow=wf)), ["A-1", "A-2"])
        # chaining: child starts when parent completes
        child, _ = self.make_wf("AS child")
        self.client.post(reverse("flows:autostart", args=[child.pk]), {"type": "workflow", "source_workflow": str(wf.pk)})
        self.assertEqual([w.pk for w in A.chained_targets(wf.workspace, wf.pk)], [child.pk])


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   MEDIA_ROOT=tempfile.mkdtemp(prefix="paperlane-test-media-"))
class InsuranceClaimWorkflowTests(TestCase):
    """End to end: the branch-lane "Insurance Claim" workflow started from its public link by a signed-out claimant."""

    SIG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)
        call_command("build_claims_workflow", verbosity=0, stdout=open(os.devnull, "w"))
        cls.wf = Workflow.objects.live().get(name="Insurance Claim")
        cls.docs = {d.name: d.key for d in cls.wf.documents.all()}

    def f(self, doc, fid):
        return f"f_{self.docs[doc]}_{fid}"

    def upload(self, name="proof.pdf"):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return SimpleUploadedFile(name, b"%PDF-1.4 test", "application/pdf")

    def start(self, amount, ctype="Vehicle", policy="POL-123456", extra=None):
        c = self.client
        c.logout()
        url = reverse("flows:public_start", args=[self.wf.link_token])
        r = c.get(url)
        self.assertContains(r, "Insurance Claim")
        r = c.post(url, {"name": "Casey Claimant", "email": "casey@example.com"})
        self.assertEqual(r.status_code, 302)
        fill = r["Location"]
        data = {self.f("Claim form", "c_name"): "Casey Claimant", self.f("Claim form", "c_email"): "casey@example.com",
                self.f("Claim form", "c_policy"): policy, self.f("Claim form", "c_type"): ctype,
                self.f("Claim form", "c_date"): "2026-10-01", self.f("Claim form", "c_amount"): str(amount),
                self.f("Claim form", "c_desc"): "Rear-ended at a light.", self.f("Claim form", "c_docs"): self.upload(),
                self.f("Claim form", "c_sig"): self.SIG, "consent": "on", "action": "submit", **(extra or {})}
        if ctype == "Vehicle":
            data.setdefault(self.f("Claim form", "c_vehicle"), "PDA 1234")
            data.setdefault(self.f("Claim form", "c_police"), self.upload("police.pdf"))
        if ctype == "Medical":
            data.setdefault(self.f("Claim form", "c_hospital"), "General Hospital")
        return c.post(fill, data)

    def packet(self):
        return Packet.objects.filter(workflow__name="Insurance Claim").latest("id")

    def open_run(self, packet, step_name):
        run = packet.runs.filter(step_name=step_name, status__in=["pending", "opened"]).first()
        self.assertIsNotNone(run, f"no open run for {step_name}; runs: {list(packet.runs.values_list('step_name', 'status'))}")
        return run

    def submit(self, run, data):
        return self.client.post(reverse("flows:fill", args=[run.token]), {**data, "consent": "on", "action": "submit"})

    def test_validation_and_conditional_fields(self):
        r = self.start(500, policy="12345")
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "Use the format POL-123456.", status_code=400)
        r = self.start(500, extra={self.f("Claim form", "c_vehicle"): ""})  # vehicle claim needs the registration
        self.assertEqual(r.status_code, 400)
        r = self.start(500, ctype="Property")  # property claims do not ask for vehicle / hospital fields
        self.assertEqual(r.status_code, 302)

    def test_low_value_claim_is_assessed_approved_and_paid(self):
        from django.core import mail
        self.assertEqual(self.start(5000).status_code, 302)
        p = self.packet()
        self.assertEqual(p.name, "Claim POL-123456 – Casey Claimant")
        run = self.open_run(p, "Assessment")
        self.assertEqual(run.assignee_email, "sam@paperlane.test")
        self.assertFalse(p.runs.filter(step_name="Senior assessment").exists())
        a = lambda fid: self.f("Assessment", fid)  # noqa: E731
        r = self.submit(run, {a("a_decision"): "Approve", a("a_approved"): "5000", a("a_deduct"): "10", a("a_sig"): self.SIG})
        self.assertEqual(r.status_code, 302)
        p.refresh_from_db()
        self.assertEqual(float(p.flat_values()["Payout"]), 4500.0)
        fin = self.open_run(p, "Finance")
        pf = lambda fid: self.f("Payment", fid)  # noqa: E731
        bad = self.submit(fin, {pf("p_method"): "Bank transfer", pf("p_account"): "123", pf("p_ref"): "X1", pf("p_date"): "2026-10-14"})
        self.assertEqual(bad.status_code, 400)
        ok = self.submit(fin, {pf("p_method"): "Bank transfer", pf("p_account"): "123", pf("p_ref"): "PAY-0001", pf("p_date"): "2026-10-14"})
        self.assertEqual(ok.status_code, 302)
        p.refresh_from_db()
        self.assertEqual(p.status, "completed")
        self.assertTrue(p.locked)
        tags = set(p.tags.values_list("name", flat=True))
        self.assertTrue({"#NewClaim", "#Approve"} <= tags and "#HighValue" not in tags, tags)
        outcome = [m for m in mail.outbox if "casey@example.com" in m.to and "Approve" in m.subject]
        self.assertTrue(outcome, [m.subject for m in mail.outbox])
        from flows.models import DataSheet
        sheet = DataSheet.objects.get(workspace=p.workspace, name="Claims")
        self.assertTrue(sheet.rows.exists())

    def test_high_value_claim_loops_for_more_info_then_is_rejected(self):
        from django.core import mail
        self.assertEqual(self.start(25000).status_code, 302)
        p = self.packet()
        self.assertIn("#HighValue", set(p.tags.values_list("name", flat=True)))
        self.assertFalse(p.runs.filter(step_name="Assessment").exists())
        run = self.open_run(p, "Senior assessment")
        a = lambda fid: self.f("Assessment", fid)  # noqa: E731
        self.assertEqual(self.submit(run, {a("a_decision"): "Need more info", a("a_info"): "Send the repair estimate.",
                                           a("a_sig"): self.SIG}).status_code, 302)
        p.refresh_from_db()
        more = self.open_run(p, "Claimant – more information")
        self.assertEqual(more.assignee_email, "casey@example.com")
        self.assertTrue([m for m in mail.outbox if "casey@example.com" in m.to and "more information" in m.subject.lower()])
        c = lambda fid: self.f("Claim form", fid)  # noqa: E731
        self.assertEqual(self.submit(more, {c("c_desc"): "Estimate attached.", c("c_docs"): self.upload("estimate.pdf")}).status_code, 302)
        p.refresh_from_db()
        again = self.open_run(p, "Senior assessment")  # back through "Route by amount" → the high-value lane
        self.assertNotEqual(again.pk, run.pk)
        self.assertEqual(self.submit(again, {a("a_decision"): "Reject", a("a_reason"): "Not covered.", a("a_sig"): self.SIG}).status_code, 302)
        p.refresh_from_db()
        self.assertEqual(p.status, "completed")
        self.assertFalse(p.runs.filter(step_name="Finance").exists())
        self.assertIn("#Reject", set(p.tags.values_list("name", flat=True)))
        self.assertTrue([m for m in mail.outbox if "casey@example.com" in m.to and "Reject" in m.subject])
        names = list(p.runs.order_by("id").values_list("step_name", flat=True))
        self.assertEqual(names, ["Claimant", "Senior assessment", "Claimant – more information", "Senior assessment"])

    def test_builder_draws_lanes(self):
        self.client.force_login(User.objects.get(username="owner"))
        r = self.client.get(reverse("flows:builder", args=[self.wf.pk]))
        for text in ("Up to 10,000", "Over 10,000", "Needs more information", "Rejected", "Complete the workflow",
                     "Go to “Route by amount”", 'class="lanes"'):
            self.assertContains(r, text)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class BranchLaneBuilderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def test_add_move_delete_in_lanes_and_nested_routing(self):
        from flows import diagram
        c = self.client
        c.force_login(User.objects.get(username="owner"))
        c.post(reverse("flows:create", args=["workflow"]), {"name": "Lanes"})
        wf = Workflow.objects.get(name="Lanes")
        first = wf.steps.get()
        c.post(reverse("flows:step_add", args=[wf.pk]), {"kind": "branch", "after": first.order})
        br = wf.steps.get(kind="branch")
        self.assertEqual(len(br.branches), 2)  # a new Branches node starts with two lanes
        l1, l2 = (f"{br.key}:{b['id']}" for b in br.branches)
        for lane, name in ((l1, "A1"), (l1, "A2"), (l2, "B1")):
            last = [s for s in wf.ordered_steps() if diagram.lane_of(s) == tuple(lane.split(":"))]
            c.post(reverse("flows:step_add", args=[wf.pk]), {"kind": "recipient", "lane": lane,
                                                             "after": last[-1].order if last else br.order})
            new = wf.steps.filter(kind="recipient").order_by("-id").first()
            new.name = name
            new.save()
        steps = wf.ordered_steps()
        lane1 = [s.name for s in diagram.children(steps, tuple(l1.split(":")))]
        self.assertEqual(lane1, ["A1", "A2"])
        a2 = wf.steps.get(name="A2")
        c.post(reverse("flows:step_action", args=[wf.pk, a2.pk, "up"]))
        self.assertEqual([s.name for s in diagram.children(wf.ordered_steps(), tuple(l1.split(":")))], ["A2", "A1"])
        # nested branch inside lane B, then delete lane A from the settings drawer
        b1 = wf.steps.get(name="B1")
        c.post(reverse("flows:step_add", args=[wf.pk]), {"kind": "branch", "lane": l2, "after": b1.order})
        inner = wf.steps.filter(kind="branch").exclude(pk=br.pk).get()
        self.assertEqual(diagram.lane_of(inner), tuple(l2.split(":")))
        post = {"name": "Branches", "branch_count": "2"}
        for i, b in enumerate(br.branches):
            post.update({f"b{i}_id": b["id"], f"b{i}_label": b["label"], f"b{i}_target": "lane", f"b{i}_then": "merge",
                         f"b{i}_logic": "and"})
        post["b0_delete"] = "on"
        c.post(reverse("flows:step_settings", args=[wf.pk, br.pk]), post)
        self.assertFalse(wf.steps.filter(name__in=["A1", "A2"]).exists())
        self.assertTrue(wf.steps.filter(name="B1").exists())
        # deleting the Branches node removes everything nested under it
        c.post(reverse("flows:step_action", args=[wf.pk, br.pk, "delete"]))
        self.assertEqual(sorted(wf.steps.values_list("name", flat=True)), [first.name])

    def test_navigation_helpers(self):
        from flows import diagram as D
        steps = [
            {"key": "s1", "kind": "recipient"},
            {"key": "b", "kind": "branch", "branches": [{"id": "x", "target": "lane", "then": "merge"},
                                                         {"id": "y", "target": "lane", "then": "end"},
                                                         {"id": "z", "target": "s1"}]},
            {"key": "x1", "kind": "recipient", "settings": {"lane": {"branch": "b", "lane": "x"}}},
            {"key": "y1", "kind": "recipient", "settings": {"lane": {"branch": "b", "lane": "y"}}},
            {"key": "s2", "kind": "recipient"},
        ]
        self.assertEqual(D.successor(steps, "s1"), "b")
        self.assertEqual(D.successor(steps, "b"), "s2")  # after the whole branch block
        self.assertEqual(D.branch_target(steps, steps[1], 0), "x1")
        self.assertEqual(D.successor(steps, "x1"), "s2")  # lane x merges
        self.assertEqual(D.successor(steps, "y1"), D.END)  # lane y ends the workflow
        self.assertEqual(D.branch_target(steps, steps[1], 2), "s1")  # go-to lane
        self.assertEqual(D.successor(steps, "s2"), D.END)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class InboxBadgeTests(TestCase):
    """The Inbox badge, the waiting.json endpoint and the "Waiting for me" list count the same documents."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def test_badge_matches_list_and_ignores_trash(self):
        from django.utils import timezone
        from core.queries import waiting_packet_count
        sam = User.objects.get(username="sam")
        ws = Workspace.objects.get(slug="demo")
        from flows import engine
        wf = Workflow.objects.filter(workspace=ws, kind="workflow", status=Workflow.PUBLISHED).first()
        for _ in range(2):
            engine.start_packet(wf, user=User.objects.get(username="owner"),
                                first_assignee={"email": sam.email, "name": "Sam", "user": sam})
        self.client.force_login(sam)
        listed = len(self.client.get(reverse("core:inbox") + "?tab=waiting").context["packets"])
        self.assertEqual(waiting_packet_count(sam, ws), listed)
        self.assertEqual(self.client.get(reverse("flows:waiting_badge")).json()["waiting"], listed)
        self.assertGreater(listed, 0, "seed should leave something waiting for sam")
        Packet.objects.filter(runs__assignee_email="sam@paperlane.test", runs__status__in=["pending", "opened"]).update(trashed_at=timezone.now())
        self.assertEqual(waiting_packet_count(sam, ws), 0)
        self.assertEqual(len(self.client.get(reverse("core:inbox") + "?tab=waiting").context["packets"]), 0)

    def test_live_stamp_moves_and_work_lists_opt_in(self):
        from core.queries import waiting_packet_count
        from flows import engine

        sam = User.objects.get(username="sam")
        owner = User.objects.get(username="owner")
        ws = Workspace.objects.get(slug="demo")
        self.client.force_login(sam)
        before = self.client.get(reverse("core:live")).json()
        self.assertEqual(before["waiting"], waiting_packet_count(sam, ws))
        self.assertIn("unread", before)
        self.assertIn("tickets", before)
        wf = Workflow.objects.filter(workspace=ws, kind="workflow", status=Workflow.PUBLISHED).first()
        engine.start_packet(wf, user=owner, first_assignee={"email": sam.email, "name": "Sam", "user": sam})
        after = self.client.get(reverse("core:live")).json()
        self.assertNotEqual(before["rev"], after["rev"])
        self.assertGreater(after["waiting"], before["waiting"])
        self.client.force_login(owner)
        for name in ("core:inbox", "flows:workflows", "flows:documents", "flows:forms", "flows:approvals", "flows:workload", "core:archive"):
            page = self.client.get(reverse(name))
            self.assertContains(page, "data-live-url")
            self.assertContains(page, "data-live")
            self.assertContains(page, "live.js")
        detail = self.client.get(reverse("flows:workflow_detail", args=[wf.pk]))
        self.assertContains(detail, "data-live")
        packet = Packet.objects.filter(workspace=ws, workflow=wf).first()
        self.assertContains(self.client.get(packet.get_absolute_url()), "data-live")


class _FormParser:
    """Collect <form method=post> fields (with their current values) from a page."""

    def __init__(self, html):
        from html.parser import HTMLParser
        self.forms, cur, sel = [], None, [None]
        outer = self

        class P(HTMLParser):
            def handle_starttag(self, tag, attrs):
                nonlocal cur
                a = dict(attrs)
                if tag == "form" and (a.get("method") or "").lower() == "post":
                    cur = {"action": a.get("action") or "", "data": {}, "enctype": a.get("enctype", "")}
                    outer.forms.append(cur)
                if cur is None or not a.get("name"):
                    if tag == "option" and cur is not None and sel[0] and ("selected" in a):
                        if "value" in a:
                            cur["data"][sel[0]] = a["value"]
                        else:  # browsers submit the option text when there is no value attribute
                            sel[0] = ("__opt__", sel[0])
                    return
                n = a["name"]
                if tag == "input":
                    t = (a.get("type") or "text").lower()
                    if t in ("checkbox", "radio"):
                        if "checked" in a:
                            cur["data"].setdefault(n, []).append(a.get("value", "on"))
                    elif t not in ("submit", "button", "file", "image"):
                        cur["data"][n] = a.get("value", "")
                elif tag == "textarea":
                    cur["data"][n] = ""
                    sel[0] = ("__ta__", n)
                elif tag == "select":
                    sel[0] = n
                    cur["data"].setdefault(n, "")

            def handle_data(self, data):
                if cur is not None and isinstance(sel[0], tuple):
                    if sel[0][0] == "__opt__":
                        cur["data"][sel[0][1]] = data.strip()
                        sel[0] = sel[0][1]
                    else:
                        cur["data"][sel[0][1]] += data

            def handle_endtag(self, tag):
                nonlocal cur
                if tag in ("select", "textarea"):
                    sel[0] = None
                if tag == "form":
                    cur = None
        P().feed(html)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class SettingsRoundTripTests(TestCase):
    """Every settings form re-submitted with its own current values must save cleanly and change nothing."""

    PAGES = ["accounts:profile", "accounts:settings", "accounts:notifications", "accounts:workspace_info",
             "accounts:workspace_settings", "accounts:theme", "accounts:thank_you", "accounts:email_templates",
             "accounts:domains"]

    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def state(self):
        from accounts.models import Profile
        ws = Workspace.objects.get(slug="demo")
        owner = User.objects.get(username="owner")
        prof = Profile.objects.get(user=owner)
        strip = lambda d: {k: v for k, v in (d or {}).items() if k != "updated"}  # noqa: E731
        prefs = {k: v for k, v in (prof.notification_prefs or {}).items() if v is not True}  # all-on == default {}
        return (ws.name, strip(ws.settings), strip(ws.thank_you), strip(getattr(ws, "theme", None)), owner.first_name,
                owner.last_name, owner.email, prof.language, prefs, prof.phone)

    def test_round_trip(self):
        self.client.force_login(User.objects.get(username="owner"))
        problems = []
        for name in self.PAGES:
            url = reverse(name)
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200, url)
            for form in _FormParser(page.content.decode()).forms:
                action = form["action"] or url
                if any(x in action for x in ("logout", "delete", "leave", "password", "email-change", "reset", "test")):
                    continue
                if {"old_password", "confirm_password"} & set(form["data"]):  # change-password / delete-account need input
                    continue
                before = self.state()
                data = {k: v for k, v in form["data"].items()}
                r = self.client.post(action, data)
                if r.status_code >= 400 or (r.status_code == 200 and b"errorlist" in r.content):
                    problems.append(f"{url} → {action}: {r.status_code}")
                elif self.state() != before:
                    diff = [i for i, (a, b) in enumerate(zip(before, self.state())) if a != b]
                    problems.append(f"{url} → {action}: unchanged submit altered state fields {diff}")
        self.assertEqual(problems, [], "\n".join(problems))


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   MEDIA_ROOT=tempfile.mkdtemp(prefix="paperlane-test-media-"))
class StartFormTests(TestCase):
    """Custom start forms: templates, in-form bots (lookups), submission → workflow."""

    SIG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)
        call_command("build_claims_workflow", verbosity=0, stdout=open(os.devnull, "w"))

    def setUp(self):
        from flows.models import IntakeForm
        self.owner = User.objects.get(username="owner")
        self.client.force_login(self.owner)
        wf = Workflow.objects.live().get(name="Insurance Claim")
        r = self.client.post(reverse("flows:form_new"), {"template": "claim_intake", "workflow": str(wf.pk)})
        self.form = IntakeForm.objects.latest("id")
        self.assertRedirects(r, reverse("flows:form_settings", args=[self.form.pk]))
        self.wf = wf

    def publish(self, **opts):
        self.form.options = {**self.form.options, **opts}
        self.form.save()
        self.client.post(reverse("flows:form_action", args=[self.form.pk, "publish"]))
        self.form.refresh_from_db()
        self.assertTrue(self.form.is_live)

    def post_form(self, extra=None, anon=True):
        from django.core.files.uploadedfile import SimpleUploadedFile
        if anon:
            self.client.logout()
        data = {"f_form_policy": "POL-100002", "f_form_insured": "Jordan Sample", "f_form_email": "jordan@example.com",
                "f_form_ctype": "Property", "f_form_date": "2026-10-01", "f_form_amount": "4000",
                "f_form_desc": "Roof leak", "f_form_docs": SimpleUploadedFile("photo.pdf", b"%PDF-1.4 x", "application/pdf"),
                "f_form_sig": self.SIG, "consent": "on", **(extra or {})}
        return self.client.post(reverse("flows:form_public", args=[self.form.token]), data)

    def test_template_creates_form_sheet_and_lookup(self):
        from flows.models import DataSheet
        self.assertEqual(self.form.workflow, self.wf)
        self.assertTrue(DataSheet.objects.filter(workspace=self.wf.workspace, name="Policies").exists())
        self.assertEqual(len(self.form.lookups), 1)
        self.publish()
        lk = self.form.lookups[0]
        self.client.logout()
        url = reverse("flows:form_lookup", args=[self.form.token, lk["id"]])
        r = self.client.post(url, '{"value": "pol-100002"}', content_type="application/json")
        self.assertEqual(r.json()["found"], True)
        self.assertEqual(r.json()["values"], {"insured": "Jordan Sample", "email": "jordan@example.com",
                                              "phone": "+1 868 555 0102", "ptype": "Investment"})
        self.assertNotIn("Address", str(r.json()), "lookup leaked an unmapped column")
        self.assertFalse(self.client.post(url, '{"value": "POL-999999"}', content_type="application/json").json()["found"])
        self.assertEqual(self.client.post(url, '{"value": "PO"}', content_type="application/json").json()["values"], {})

    def test_submit_starts_workflow_as_first_step_with_files_and_sheet(self):
        from flows.models import DataSheet, FormSubmission
        self.publish(complete_first_step=True, sheet="Claim forms")
        # tamper attempt: read-only "Policy type" posted by hand is overwritten by the server-side lookup
        r = self.post_form({"f_form_ptype": "Platinum (forged)"})
        self.assertEqual(r.status_code, 302, r.content.decode()[:500] if r.status_code != 302 else "")
        self.assertIn(reverse("flows:form_done", args=[self.form.token]), r["Location"])
        sub = FormSubmission.objects.get(form=self.form)
        self.assertEqual(sub.status, "started", sub.error)
        self.assertEqual(sub.data["values"]["ptype"], "Investment")
        p = sub.packet
        self.assertEqual(p.source, "form")
        self.assertEqual(p.name, "Claim POL-100002 – Jordan Sample")
        vals = p.flat_values()
        self.assertEqual((vals["Policy number"], vals["Claim type"], str(vals["Claimed amount"])), ("POL-100002", "Property", "4000"))
        att = vals["Supporting documents"]
        self.assertEqual([a["name"] for a in att], ["photo.pdf"])
        names = list(p.runs.order_by("id").values_list("step_name", "status"))
        self.assertEqual(names[0], ("Claimant", "completed"))
        self.assertEqual(names[1][0], "Assessment")  # 4000 → the "Up to 10,000" lane
        sheet = DataSheet.objects.get(workspace=self.wf.workspace, name="Claim forms")
        self.assertEqual(sheet.rows.get().values["PolicyNumber"], "POL-100002")

    def test_validation_keeps_upload_and_closed_or_draft_is_unavailable(self):
        from flows.models import FormFile
        self.client.logout()
        self.assertEqual(self.client.get(reverse("flows:form_public", args=[self.form.token])).status_code, 404)  # draft
        self.client.force_login(self.owner)
        self.publish()
        r = self.post_form({"f_form_policy": "12"})  # wrong format and no lookup match
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "Use the format POL-123456.", status_code=400)
        self.assertContains(r, "photo.pdf", status_code=400)  # upload kept for the next try
        ff = FormFile.objects.get(form=self.form)
        r = self.post_form({"f_form_docs": "", "f_form_docs__keep_present": "1", "f_form_docs__keep": ff.key}, anon=False)
        self.assertEqual(r.status_code, 302)
        self.client.force_login(self.owner)
        self.client.post(reverse("flows:form_settings", args=[self.form.pk]), {"section": "general", "name": self.form.name,
                                                                             "workflow": str(self.wf.pk), "closed": "on"})
        self.client.logout()
        self.assertEqual(self.client.get(reverse("flows:form_public", args=[self.form.token])).status_code, 404)

    def test_editor_settings_and_permissions(self):
        import json
        r = self.client.get(reverse("flows:form_edit", args=[self.form.pk]))
        self.assertContains(r, "Continue to settings")
        fields = self.form.fields + [{"id": "extra", "type": "text", "label": "Extra", "role": "s1"}]
        r = self.client.post(reverse("flows:form_save", args=[self.form.pk]), json.dumps({"schema": {"fields": fields}}),
                             content_type="application/json")
        self.assertTrue(r.json()["ok"])
        self.form.refresh_from_db()
        self.assertEqual(self.form.fields[-1]["role"], "anyone")
        settings_url = reverse("flows:form_settings", args=[self.form.pk])
        page = self.client.get(settings_url)
        for text in ("In-form bots", "Look up policy", "Field mapping", "Share &amp; embed", "Automatic: Full name"):
            self.assertContains(page, text)
        lk = self.form.lookups[0]
        self.client.post(settings_url, {"section": "lookups", "lookup_count": "1", "lk0_id": lk["id"], "lk0_name": "Policy bot",
                                        "lk0_trigger": "policy", "lk0_source": "sheet", "lk0_sheet": "Policies",
                                        "lk0_key_column": "PolicyNumber", "lk0_min_length": "6",
                                        "lk0_from": ["InsuredsName", ""], "lk0_to": ["insured", ""]})
        self.form.refresh_from_db()
        self.assertEqual((self.form.lookups[0]["name"], self.form.lookups[0]["map"]), ("Policy bot", [{"from": "InsuredsName", "to": "insured"}]))
        self.client.post(settings_url, {"section": "mapping", "map:Phone": "email"})
        self.form.refresh_from_db()
        self.assertEqual(self.form.options["mapping"], {"Phone": "email"})
        b = self.client.get(reverse("flows:builder", args=[self.wf.pk]))
        self.assertContains(b, "📝 Insurance claim intake")
        self.client.force_login(User.objects.get(username="fay"))  # filler: may view, not edit
        self.assertEqual(self.client.get(reverse("flows:form_edit", args=[self.form.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("flows:form_action", args=[self.form.pk, "delete"])).status_code, 403)


PURCHASE_BLUEPRINT = {
    "name": "Purchase approval",
    "description": "Requester → manager → (finance if over 5,000) → procurement, with a revise loop.",
    "documents": [{"key": "req", "name": "Purchase request", "fields": [
        {"id": "item", "type": "text", "label": "Item", "name": "Item", "required": True, "filled_by": ["requester"]},
        {"id": "sku", "type": "text", "label": "SKU", "name": "SKU", "filled_by": ["requester"]},
        {"id": "qty", "type": "number", "label": "Quantity", "name": "Quantity", "required": True, "filled_by": ["requester"]},
        {"id": "price", "type": "currency", "label": "Unit price", "name": "Unit price", "required": True, "filled_by": ["requester"]},
        {"id": "total", "type": "formula", "label": "Total", "name": "Total", "formula": "{Quantity} * {Unit price}", "filled_by": ["requester"]},
        {"id": "mgr", "type": "radio", "label": "Manager decision", "name": "Manager decision", "required": True,
         "options": ["Approve", "Revise"], "filled_by": ["manager"]},
        {"id": "fin", "type": "radio", "label": "Finance decision", "name": "Finance decision", "required": True,
         "options": ["Approve", "Reject"], "filled_by": ["finance"]},
        {"id": "po", "type": "text", "label": "PO number", "name": "PO number", "required": True, "filled_by": ["procurement"]},
    ]}],
    "steps": [
        {"key": "requester", "name": "Requester", "recipient": {"type": "anyone"}, "settings": {"recurrence": True}},
        {"key": "manager", "name": "Manager approval", "recipient": {"type": "group", "group": "Managers", "members": ["sam@paperlane.test"]},
         "settings": {"recurrence": True},
         "jumps": [{"to": "requester", "when": {"rules": [{"field": "Manager decision", "op": "eq", "value": "Revise"}]}}]},
        {"key": "route", "type": "branch", "name": "Amount check", "lanes": [
            {"id": "big", "label": "Over 5,000", "when": {"rules": [{"field": "Total", "op": "gt", "value": "5000"}]}, "then": "merge",
             "steps": [{"key": "finance", "name": "Finance approval", "recipient": {"type": "email", "email": "cal@paperlane.test"}}]},
            {"id": "small", "label": "Up to 5,000", "target": "next"}]},
        {"key": "reject", "type": "branch", "name": "Finance outcome", "lanes": [
            {"id": "no", "label": "Rejected", "when": {"rules": [{"field": "Finance decision", "op": "eq", "value": "Reject"}]}, "target": "end"}]},
        {"key": "procurement", "name": "Procurement", "recipient": {"type": "email", "email": "fay@paperlane.test"}},
    ],
    "bots": [{"type": "rename_packet", "name": "Name it", "trigger": "step_complete", "step": "requester",
              "config": {"pattern": "PR – {field:Item}"}},
             {"type": "add_tags", "name": "Big spend", "trigger": "flow_complete", "config": {"tags": "#BigSpend"},
              "when": {"rules": [{"field": "Total", "op": "gt", "value": "5000"}]}}],
    "start_form": {"name": "Purchase request form", "publish": True, "options": {"complete_first_step": True},
                   "fields": [{"id": "item", "type": "text", "label": "Item", "name": "Item", "required": True},
                              {"id": "sku", "type": "text", "label": "SKU", "name": "SKU"},
                              {"id": "qty", "type": "number", "label": "Quantity", "name": "Quantity", "required": True},
                              {"id": "price", "type": "currency", "label": "Unit price", "name": "Unit price", "required": True}],
                   "lookups": [{"id": "cat", "name": "Catalog price", "trigger": "sku", "source": "sheet", "sheet": "Catalog",
                                "key_column": "SKU", "min_length": 3, "map": [{"from": "Item", "to": "item"}, {"from": "Price", "to": "price"}]}]},
}


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class McpServerTests(TestCase):
    """The MCP server builds, runs and inspects workflows and forms through JSON-RPC."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        from flows.management.commands.paperlane_mcp import Server
        self.srv = Server(Workspace.objects.get(slug="demo"), User.objects.get(username="owner"))
        self.n = 0

    def rpc(self, method, params=None):
        self.n += 1
        return self.srv.handle({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})

    def tool(self, _tool, **args):
        import json
        r = self.rpc("tools/call", {"name": _tool, "arguments": args})["result"]
        text = r["content"][0]["text"]
        if r["isError"]:
            return {"_error": text}
        return json.loads(text)

    def test_protocol(self):
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", init["result"]["capabilities"])
        self.assertIsNone(self.srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        names = {t["name"] for t in self.rpc("tools/list")["result"]["tools"]}
        self.assertTrue({"build_workflow", "get_workflow", "start_workflow", "complete_step", "build_form", "submit_form"} <= names)
        self.assertEqual(self.rpc("bogus/method")["error"]["code"], -32601)
        self.assertIn("Unknown tool", self.tool("nope")["_error"])
        guide = self.tool("get_build_guide")
        self.assertIn("rename_packet", guide["bot_types"])

    def test_build_run_and_export_complex_workflow(self):
        self.tool("create_spreadsheet", name="Catalog", columns=["SKU", "Item", "Price"],
                  rows=[{"SKU": "LAP-01", "Item": "Laptop", "Price": "1800"}, {"SKU": "SRV-09", "Item": "Server", "Price": "9000"}])
        built = self.tool("build_workflow", blueprint=PURCHASE_BLUEPRINT, publish=True)
        self.assertNotIn("_error", built, built)
        self.assertTrue(built["published"], built["problems"])
        self.assertEqual((built["steps"], built["bots"], len(built["start_forms"])), (6, 2, 1))
        wf_id = built["id"]
        # export round trip shows the nested lane and the jump
        bp = self.tool("get_workflow", workflow_id=wf_id)
        route = next(s for s in bp["steps"] if s["name"] == "Amount check")
        self.assertEqual(route["lanes"][0]["steps"][0]["name"], "Finance approval")
        mgr = next(s for s in bp["steps"] if s["name"] == "Manager approval")
        self.assertEqual(len(mgr["jumps"]), 1)

        # big purchase: start → manager asks to revise → requester fixes → manager approves → finance → procurement
        doc = self.tool("start_workflow", workflow_id=wf_id, recipient_email="owner@paperlane.test")
        d = doc["document_id"]
        bad = self.tool("complete_step", document_id=d, values={"Item": "Server"})
        self.assertFalse(bad["completed"])
        self.assertIn("Quantity", bad["errors"])
        r = self.tool("complete_step", document_id=d, values={"Item": "Server", "Quantity": 2, "Unit price": 4000})
        self.assertEqual(r["name"], "PR – Server")
        self.assertEqual(r["open_steps"][0]["step"], "Manager approval")
        r = self.tool("complete_step", document_id=d, values={"Manager decision": "Revise"})
        self.assertEqual(r["open_steps"][0]["step"], "Requester")  # jump back
        self.tool("complete_step", document_id=d, values={"Quantity": 3})
        r = self.tool("complete_step", document_id=d, values={"Manager decision": "Approve"})
        self.assertEqual(r["open_steps"][0]["step"], "Finance approval")  # 3 × 4000 > 5000
        self.tool("complete_step", document_id=d, values={"Finance decision": "Approve"})
        r = self.tool("complete_step", document_id=d, values={"PO number": "PO-77"})
        self.assertEqual(r["status"], "completed")
        self.assertIn("#BigSpend", r["tags"])
        info = self.tool("get_document", document_id=d)
        self.assertEqual(float(info["values"]["Total"]), 12000.0)

        # small purchase through the start form: the in-form bot fills item and price from the Catalog sheet
        form = built["start_forms"][0]
        f = self.tool("get_form", form_id=form["id"])
        look = self.tool("run_form_lookup", form_id=form["id"], lookup_id=f["lookups"][0]["id"], value="lap-01")
        self.assertEqual(look["values"], {"item": "Laptop", "price": "1800"})
        sub = self.tool("submit_form", form_id=form["id"], values={"SKU": "LAP-01", "Quantity": 2})
        self.assertTrue(sub["submitted"], sub)
        self.assertEqual(sub["filled_by_bots"], {"item": "Laptop", "price": "1800"})
        steps = [h["step"] for h in sub["document"]["history"]]
        self.assertEqual(steps, ["Requester", "Manager approval"])  # form = first step; 3,600 skips finance later
        d2 = sub["document"]["document_id"]
        self.tool("complete_step", document_id=d2, values={"Manager decision": "Approve"})
        r = self.tool("complete_step", document_id=d2, values={"PO number": "PO-78"})
        self.assertEqual([h["step"] for h in r["history"]], ["Requester", "Manager approval", "Procurement"])

    def test_errors_and_permissions(self):
        bad = dict(PURCHASE_BLUEPRINT, steps=[{"key": "a", "name": "A"}, {"key": "a", "name": "B"}])
        self.assertIn("used twice", self.tool("build_workflow", blueprint=bad)["_error"])
        bad2 = dict(PURCHASE_BLUEPRINT, bots=[{"type": "made_up"}])
        self.assertIn("Unknown bot type", self.tool("build_workflow", blueprint=bad2)["_error"])
        from flows.management.commands.paperlane_mcp import Server
        self.srv = Server(Workspace.objects.get(slug="demo"), User.objects.get(username="ana"))  # auditor
        self.assertIn("cannot create workflows", self.tool("build_workflow", blueprint=PURCHASE_BLUEPRINT)["_error"])
        self.assertIsInstance(self.tool("list_workflows"), list)  # read-only tools still work

    def test_replace(self):
        first = self.tool("build_workflow", blueprint=PURCHASE_BLUEPRINT)
        second = self.tool("build_workflow", blueprint=PURCHASE_BLUEPRINT, replace=True)
        self.assertEqual(second["replaced"], [first["id"]])
        self.assertEqual([w["id"] for w in self.tool("list_workflows", query="Purchase approval")], [second["id"]])


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AuthAndLandingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)
        cls.pw = "paperlane-demo-2026"

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def test_landing_for_visitors_dashboard_for_members(self):
        r = self.client.get("/")
        self.assertContains(r, "Forms, approvals and signatures in one workflow.")
        self.assertContains(r, "Insurance claim intake")  # template gallery
        for page in ("/", reverse("accounts:login"), reverse("accounts:signup")):  # public pages stay free of em-dashes
            body = self.client.get(page).content.decode()
            self.assertNotIn("\u2014", body.split("<body", 1)[1], page)
        self.assertContains(r, reverse("accounts:signup"))
        self.client.force_login(User.objects.get(username="owner"))
        self.assertContains(self.client.get("/"), "What will you create today?")
        self.assertContains(self.client.get(reverse("core:landing")), "Forms, approvals and signatures")

    def test_login_remember_and_safe_redirect(self):
        url = reverse("accounts:login")
        page = self.client.get(url)
        for text in ("Forgot password?", "Keep me signed in", "data-pw-toggle"):
            self.assertContains(page, text)
        r = self.client.post(url + "?next=https://evil.example/phish", {"email": "owner@paperlane.test", "password": self.pw})
        self.assertRedirects(r, reverse("core:dashboard"), fetch_redirect_response=False)  # foreign next ignored
        self.assertTrue(self.client.session.get_expire_at_browser_close())  # remember unticked → ends with the browser
        self.client.logout()
        r = self.client.post(url + "?next=/workflows/", {"email": "owner@paperlane.test", "password": self.pw, "remember": "on"})
        self.assertEqual(r["Location"], "/workflows/")
        self.assertFalse(self.client.session.get_expire_at_browser_close())

    def test_session_ends_with_browser_when_not_remembered(self):
        self.client.post(reverse("accounts:login"), {"email": "owner@paperlane.test", "password": self.pw})
        self.assertTrue(self.client.session.get_expire_at_browser_close())

    def test_throttling(self):
        url = reverse("accounts:login")
        for _ in range(8):
            r = self.client.post(url, {"email": "owner@paperlane.test", "password": "wrong"})
            self.assertContains(r, "Incorrect email or password.")
        r = self.client.post(url, {"email": "owner@paperlane.test", "password": self.pw})  # even the right one waits
        self.assertEqual(r.status_code, 429)
        self.assertContains(r, "Too many failed attempts", status_code=429)

    def test_password_reset_flow(self):
        import re
        from django.core import mail
        r = self.client.post(reverse("accounts:password_reset"), {"email": "sam@paperlane.test"})
        self.assertRedirects(r, reverse("accounts:password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)
        link = re.search(r"https?://[^\s]+/accounts/reset/[^\s]+", mail.outbox[0].body).group(0)
        path = link.split("://", 1)[1].split("/", 1)[1]
        r = self.client.get("/" + path, follow=True)
        self.assertContains(r, "Choose a new password")
        r = self.client.post(r.redirect_chain[-1][0], {"new_password1": "a-brand-new-pass-77", "new_password2": "a-brand-new-pass-77"})
        self.assertRedirects(r, reverse("accounts:password_reset_complete"))
        self.client.logout()
        r = self.client.post(reverse("accounts:login"), {"email": "sam@paperlane.test", "password": "a-brand-new-pass-77"})
        self.assertEqual(r.status_code, 302)
        # unknown address: same response, no email (no account enumeration)
        mail.outbox.clear()
        self.client.logout()
        r = self.client.post(reverse("accounts:password_reset"), {"email": "nobody@example.com"})
        self.assertRedirects(r, reverse("accounts:password_reset_done"))
        self.assertEqual(len(mail.outbox), 0)

    def test_logout_needs_post(self):
        self.client.force_login(User.objects.get(username="owner"))
        r = self.client.get(reverse("accounts:logout"))
        self.assertContains(r, "Log out of Paperlane?")
        self.assertIn("_auth_user_id", self.client.session)  # a plain visit (link, prefetch) keeps you signed in
        self.assertContains(self.client.get("/"), 'action="%s"' % reverse("accounts:logout"))
        self.assertRedirects(self.client.post(reverse("accounts:logout")), reverse("accounts:login"))
        self.assertNotIn("_auth_user_id", self.client.session)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class TwoFactorAndSessionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.user = User.objects.get(username="sam")
        self.pw = "paperlane-demo-2026"

    def _enable(self):
        from accounts import twofactor
        self.client.force_login(self.user)
        self.client.post(reverse("accounts:security_action", args=["2fa-start"]))
        secret = self.client.session["2fa_setup"]
        page = self.client.get(reverse("accounts:security"))
        self.assertContains(page, "<svg")  # QR code rendered inline
        self.assertContains(page, secret)
        bad = self.client.post(reverse("accounts:security_action", args=["2fa-confirm"]), {"code": "000000"}, follow=True)
        self.assertContains(bad, "didn&#x27;t match")
        code = twofactor.code_at(secret, twofactor.current_step())
        r = self.client.post(reverse("accounts:security_action", args=["2fa-confirm"]), {"code": code}, follow=True)
        self.assertContains(r, "Save your recovery codes")
        codes = r.context["new_codes"]
        self.assertEqual(len(codes), 10)
        self.assertNotContains(self.client.get(reverse("accounts:security")), codes[0])  # shown once
        self.client.post(reverse("accounts:logout"))
        return secret, codes

    def test_two_factor_login_with_code_and_recovery_code(self):
        from accounts import twofactor
        secret, codes = self._enable()
        login = reverse("accounts:login")
        r = self.client.post(login + "?next=/workflows/", {"email": self.user.email, "password": self.pw})
        self.assertRedirects(r, reverse("accounts:login_verify"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)  # not signed in until the code is entered
        self.assertContains(self.client.get(reverse("dashboard") if False else "/workflows/", follow=True), "Welcome back")
        r = self.client.post(login + "?next=/workflows/", {"email": self.user.email, "password": self.pw})
        bad = self.client.post(reverse("accounts:login_verify"), {"code": "123456"})
        self.assertContains(bad, "isn&#x27;t right")
        # the step used at enable time is spent; take the next one
        self.user.profile.refresh_from_db()
        step = max(self.user.profile.totp_last_step + 1, twofactor.current_step() - 1)
        with self.settings():
            from unittest import mock
            with mock.patch("accounts.twofactor.time.time", return_value=step * 30 + 1):
                r = self.client.post(reverse("accounts:login_verify"), {"code": twofactor.code_at(secret, step)})
                self.assertEqual(r["Location"], "/workflows/")
                self.assertIn("_auth_user_id", self.client.session)
                # the same code cannot be replayed
                self.client.post(reverse("accounts:logout"))
                self.client.post(login, {"email": self.user.email, "password": self.pw})
                again = self.client.post(reverse("accounts:login_verify"), {"code": twofactor.code_at(secret, step)})
                self.assertEqual(again.status_code, 200)
        # recovery code works once
        r = self.client.post(reverse("accounts:login_verify"), {"code": codes[0]})
        self.assertEqual(r.status_code, 302)
        self.user.profile.refresh_from_db()
        self.assertEqual(len(self.user.profile.recovery_codes), 9)
        self.client.post(reverse("accounts:logout"))
        self.client.post(login, {"email": self.user.email, "password": self.pw})
        self.assertEqual(self.client.post(reverse("accounts:login_verify"), {"code": codes[0]}).status_code, 200)

    def test_verify_throttle_and_disable(self):
        self._enable()
        self.client.post(reverse("accounts:login"), {"email": self.user.email, "password": self.pw})
        for _ in range(6):
            self.client.post(reverse("accounts:login_verify"), {"code": "111111"})
        r = self.client.post(reverse("accounts:login_verify"), {"code": "111111"})
        self.assertRedirects(r, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertNotIn("2fa_pending", self.client.session)
        self.client.force_login(self.user)
        self.client.post(reverse("accounts:security_action", args=["2fa-disable"]), {"password": "wrong"})
        self.user.profile.refresh_from_db()
        self.assertTrue(self.user.profile.two_factor_on)
        self.client.post(reverse("accounts:security_action", args=["2fa-disable"]), {"password": self.pw})
        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.two_factor_on)

    def test_sessions_listed_and_signed_out(self):
        other = self.client_class()
        other.post(reverse("accounts:login"), {"email": self.user.email, "password": self.pw}, HTTP_USER_AGENT="Mozilla/5.0 (iPhone) Safari/605")
        self.client.post(reverse("accounts:login"), {"email": self.user.email, "password": self.pw}, HTTP_USER_AGENT="Mozilla/5.0 (Macintosh) Chrome/140")
        page = self.client.get(reverse("accounts:security"))
        self.assertContains(page, "Safari on iPhone")
        self.assertContains(page, "This browser")
        self.client.post(reverse("accounts:security_action", args=["sign-out-others"]))
        self.assertEqual(other.get("/").status_code, 200)
        self.assertNotIn("_auth_user_id", other.session)
        self.assertIn("_auth_user_id", self.client.session)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        from django.core.cache import cache
        from accounts.models import ApiToken
        cache.clear()
        self.owner = User.objects.get(username="owner")
        self.ws = Workspace.objects.get(slug="demo")
        self.tok, self.full = ApiToken.issue(self.owner, self.ws, "full")
        _, self.read = ApiToken.issue(self.owner, self.ws, "read", scope="read")

    def api(self, method, path, raw=None, data=None, **headers):
        import json
        kw = {"HTTP_AUTHORIZATION": f"Bearer {raw}"} if raw else {}
        kw.update(headers)
        fn = getattr(self.client, method.lower())
        if data is not None:
            return fn(path, json.dumps(data), content_type="application/json", **kw)
        return fn(path, **kw)

    def test_token_page_create_show_once_and_revoke(self):
        from accounts.models import ApiToken
        self.client.force_login(self.owner)
        r = self.client.post(reverse("accounts:api_tokens"), {"name": "Claude", "scope": "read", "days": "30"}, follow=True)
        raw = r.context["new_token"]["raw"]
        self.assertContains(r, raw)
        self.assertContains(r, "claude mcp add --transport http paperlane")
        self.assertNotContains(self.client.get(reverse("accounts:api_tokens")), raw)
        tok = ApiToken.objects.get(name="Claude")
        self.assertNotIn(raw, tok.token_hash)
        self.assertEqual(self.api("GET", "/api/v1/me", raw).json()["scope"], "read")
        self.client.post(reverse("accounts:api_tokens"), {"action": "revoke", "token": tok.pk})
        self.assertEqual(self.api("GET", "/api/v1/me", raw).status_code, 401)
        # another user can't revoke your token
        self.client.force_login(User.objects.get(username="sam"))
        self.client.post(reverse("accounts:api_tokens"), {"action": "revoke", "token": self.tok.pk})
        self.assertEqual(self.api("GET", "/api/v1/me", self.full).status_code, 200)

    def test_auth_failures(self):
        from datetime import timedelta
        from django.utils import timezone
        self.assertEqual(self.api("GET", "/api/v1/workflows").status_code, 401)
        self.assertEqual(self.api("GET", "/api/v1/workflows", "sd_nope_nope").status_code, 401)
        self.assertEqual(self.api("GET", "/api/v1/workflows", self.full[:-2] + "xx").status_code, 401)
        self.tok.expires_at = timezone.now() - timedelta(minutes=1)
        self.tok.save()
        self.assertEqual(self.api("GET", "/api/v1/workflows", self.full).status_code, 401)
        self.tok.expires_at = None
        self.tok.save()
        from accounts.models import Membership
        Membership.objects.filter(user=self.owner, workspace=self.ws).update(active=False)
        self.assertEqual(self.api("GET", "/api/v1/workflows", self.full).status_code, 401)

    def test_rest_build_start_complete(self):
        from core.test_deep import PURCHASE_BLUEPRINT
        self.assertEqual(self.api("POST", "/api/v1/workflows", self.read, data=PURCHASE_BLUEPRINT).status_code, 403)
        r = self.api("POST", "/api/v1/workflows?publish=1", self.full, data=PURCHASE_BLUEPRINT)
        self.assertEqual(r.status_code, 201, r.content)
        wf = r.json()
        self.assertTrue(wf["published"])
        bp = self.api("GET", f"/api/v1/workflows/{wf['id']}", self.read).json()
        self.assertEqual(bp["name"], "Purchase approval")
        doc = self.api("POST", f"/api/v1/workflows/{wf['id']}/start", self.full, data={"recipient_email": "owner@paperlane.test"})
        self.assertEqual(doc.status_code, 201)
        d = doc.json()["document_id"]
        r = self.api("POST", f"/api/v1/documents/{d}/complete", self.full, data={"values": {"Item": "Desk", "Quantity": 1, "Unit price": 300}})
        self.assertEqual(r.json()["open_steps"][0]["step"], "Manager approval")
        self.assertEqual(self.api("GET", f"/api/v1/documents?workflow_id={wf['id']}", self.read).json()[0]["document_id"], d)
        self.assertEqual(self.api("GET", "/api/v1/", self.read).json()["api"], "Paperlane REST API v1")
        self.assertEqual(self.api("GET", "/api/v1/nonsense", self.read).status_code, 404)
        self.assertEqual(self.api("PATCH", "/api/v1/workflows", self.full, data={}).status_code, 405)
        self.assertEqual(self.api("POST", "/api/v1/tools/list_team", self.read, data={}).status_code, 200)
        bad = self.client.post("/api/v1/workflows", "{not json", content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.full}")
        self.assertEqual(bad.status_code, 400)

    def test_mcp_http(self):
        init = self.api("POST", "/mcp", self.full, data={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                         "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
        self.assertEqual(init.json()["result"]["serverInfo"]["name"], "paperlane")
        self.assertEqual(self.api("POST", "/mcp", self.full, data={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code, 202)
        tools = self.api("POST", "/mcp", self.read, data={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
        self.assertNotIn("build_workflow", {t["name"] for t in tools})
        batch = self.api("POST", "/mcp", self.full, data=[{"jsonrpc": "2.0", "id": 3, "method": "ping"},
                                                          {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "list_forms", "arguments": {}}}])
        self.assertEqual([x["id"] for x in batch.json()], [3, 4])
        self.assertEqual(self.api("GET", "/mcp", self.full).status_code, 405)
        self.assertEqual(self.api("POST", "/mcp", self.full, data={}, HTTP_ORIGIN="https://evil.example").status_code, 403)
        self.assertEqual(self.api("POST", "/mcp", None, data={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code, 401)

    def test_rate_limit(self):
        from flows.views import api
        with self.settings():
            old = api.RATE_PER_MINUTE
            api.RATE_PER_MINUTE = 3
            try:
                codes = [self.api("GET", "/api/v1/me", self.read).status_code for _ in range(4)]
            finally:
                api.RATE_PER_MINUTE = old
        self.assertEqual(codes, [200, 200, 200, 429])


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AiBuilderAndBlueprintTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        self.owner = User.objects.get(username="owner")
        self.ws = Workspace.objects.get(slug="demo")
        self.client.force_login(self.owner)

    def test_planner_examples_build_and_publish(self):
        from flows import ai_builder
        from flows.blueprint import build_workflow
        for title, text in ai_builder.EXAMPLES:
            bp, notes = ai_builder.plan_with_rules(text)
            wf, problems = build_workflow(self.ws, self.owner, bp, publish=True)
            self.assertEqual(problems, [], title)
            self.assertTrue(wf.is_published, title)

    def test_planner_understands_conditions_and_reviews(self):
        from flows.ai_builder import plan_with_rules
        bp, _ = plan_with_rules("Expense claim: staff submit merchant, amount and receipt upload. Team lead reviews it. "
                                "If amount is over 2.5k the director approves. Accounts payable records the payment reference.")
        steps = {s["name"]: s for s in bp["steps"]}
        self.assertEqual(bp["name"], "Expense claim")
        self.assertEqual(steps["Director approval"]["condition"]["rules"][0], {"field": "Amount", "op": "gt", "value": "2500"})
        self.assertEqual(steps["Team lead review"]["jumps"][0]["to"], bp["steps"][0]["key"])
        types = {f["label"]: f["type"] for f in bp["documents"][0]["fields"]}
        self.assertEqual((types["Amount"], types["Receipt upload"], types["Payment reference"]), ("currency", "attachment", "text"))

    def test_describe_page_preview_then_create(self):
        from flows.models import Workflow
        url = reverse("flows:ai_describe")
        self.assertContains(self.client.get(url), "Describe your workflow")
        before = Workflow.objects.count()
        text = "Leave request: employee fills in leave type, start date, end date and notes. Supervisor approves. Notify the employee when done."
        r = self.client.post(url, {"action": "plan", "text": text})
        self.assertContains(r, "Supervisor approval")
        self.assertContains(r, "Email the requester")
        self.assertEqual(Workflow.objects.count(), before)  # preview creates nothing
        r = self.client.post(url, {"action": "create", "text": text, "blueprint": r.context["blueprint_json"], "publish": "1"})
        wf = Workflow.objects.latest("pk")
        self.assertRedirects(r, reverse("flows:builder", args=[wf.pk]), fetch_redirect_response=False)
        self.assertTrue(wf.is_published)
        # a run: the employee's email gets the completion email
        from flows import engine
        from django.core import mail
        p = engine.start_packet(wf, user=self.owner, source="api", first_assignee={"email": "owner@paperlane.test", "name": "Owner", "user": self.owner})
        from flows.mcp import Server
        srv = Server(self.ws, self.owner)
        srv.t_complete_step(p.pk, values={"Leave type": "Annual", "Start date": "2026-11-02", "End date": "2026-11-06", "Email": "emp@example.com"})
        out = srv.t_complete_step(p.pk, values={"Supervisor approval decision": "Approve"})
        self.assertEqual(out["status"], "completed")
        self.assertTrue(any("emp@example.com" in m.to for m in mail.outbox))
        # bad JSON is reported, not a crash
        r = self.client.post(url, {"action": "create", "text": text, "blueprint": "{oops"})
        self.assertContains(r, "invalid")
        # auditors can't use it
        self.client.force_login(User.objects.get(username="ana"))
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_blueprint_export_import_round_trip_on_real_workflows(self):
        import io, json
        from flows.models import Workflow
        from flows.views.builder import validate
        call_command("build_claims_workflow", verbosity=0, stdout=io.StringIO())
        for name in ("Insurance Claim",):
            src = Workflow.objects.live().get(workspace=self.ws, name=name)
            r = self.client.get(reverse("flows:workflow_export", args=[src.pk]) + "?format=blueprint")
            self.assertIn("blueprint.json", r["Content-Disposition"])
            data = json.loads(r.content)
            from django.core.files.uploadedfile import SimpleUploadedFile
            up = SimpleUploadedFile("x.blueprint.json", json.dumps(data).encode(), content_type="application/json")
            r = self.client.post(reverse("flows:workflow_import"), {"file": up})
            copy = Workflow.objects.latest("pk")
            self.assertEqual(copy.source_ref, "blueprint", name)
            self.assertEqual(copy.steps.count(), src.steps.count(), name)
            self.assertEqual(copy.bots.count(), src.bots.count(), name)
            self.assertEqual(sum(len(d.fields) for d in copy.documents.all()), sum(len(d.fields) for d in src.documents.all()), name)
            self.assertEqual(validate(copy), validate(src), name)


class LocalModelTests(TestCase):
    """Ollama / OpenAI-compatible engines, with the HTTP calls mocked (no model server needed)."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.owner = User.objects.get(username="owner")
        self.ws = Workspace.objects.get(slug="demo")

    def fake(self, replies):
        """Patch the provider HTTP layer: /api/tags lists two models; /api/chat returns replies in order."""
        import json
        from unittest import mock
        calls = []

        def http(url, payload=None, headers=None, timeout=10):
            calls.append((url, payload))
            if url.endswith("/api/tags"):
                return {"models": [{"name": "small:1b", "details": {"parameter_size": "1.2B"}, "capabilities": ["completion"]},
                                   {"name": "big-mlx:27b", "details": {}, "capabilities": ["completion", "tools"]},
                                   {"name": "embed", "details": {}, "capabilities": ["embedding"]}]}
            if url.endswith("/api/chat"):
                r = replies.pop(0)
                return {"message": {"content": r if isinstance(r, str) else json.dumps(r)}}
            raise AssertionError(url)
        return mock.patch("flows.ai_providers._http", side_effect=http), calls

    def model_blueprint(self):
        return {"name": "Visitor access", "documents": [{"fields": [
            {"label": "Visitor name", "type": "string", "filled_by": "Host", "required": True},
            {"label": "Needs parking", "type": "boolean", "filled_by": ["Host"]},
            {"label": "Security decision", "type": "radio", "options": "Approve, Reject", "filled_by": ["Security"]}]}],
            "steps": [{"name": "Host"}, {"name": "Security", "recipient": "Security"},
                      {"name": "Facilities", "recipient": "facilities@example.com",
                       "condition": [{"field": "Needs parking", "operator": "is_checked_typo", "value": True}]}],
            "bots": [{"type": "email_on_complete", "trigger": "end", "config": {"to": "{field:Host email}"}}]}

    def test_engines_listed_and_sorted(self):
        from flows import ai_providers
        patcher, _ = self.fake([])
        with patcher:
            ids = [e["id"] for e in ai_providers.engines(refresh=True)]
        self.assertEqual(ids, ["planner", "ollama:big-mlx:27b", "ollama:small:1b"])  # embeddings skipped, biggest first

    def test_ollama_plan_with_repair(self):
        from flows import ai_builder
        bad = self.model_blueprint()
        good = self.model_blueprint()
        good["steps"][2]["condition"] = [{"field": "Needs parking", "op": "checked", "value": ""}]
        patcher, calls = self.fake(["Sure! Here it is:", good])  # first reply has no JSON → asked to fix
        with patcher:
            r = ai_builder.plan("Visitor access …", self.ws, self.owner, engine="ollama:small:1b")
        self.assertEqual(r["engine"], "ollama:small:1b")
        chat = [c for c in calls if c[0].endswith("/api/chat")]
        self.assertEqual(len(chat), 2)
        self.assertEqual(chat[0][1]["format"], "json")
        self.assertIn("problem", chat[1][1]["messages"][-1]["content"])
        bp = r["blueprint"]
        types = {f["label"]: f["type"] for f in bp["documents"][0]["fields"]}
        self.assertEqual(types["Needs parking"], "checkbox")
        self.assertIn("Email", types)  # added for the email bot
        self.assertEqual(bp["steps"][2]["recipient"], {"type": "email", "email": "facilities@example.com"})
        from flows.blueprint import build_workflow
        wf, problems = build_workflow(self.ws, self.owner, bp, publish=True)
        self.assertEqual(problems, [])

    def test_falls_back_to_planner(self):
        from flows import ai_builder
        patcher, _ = self.fake(["not json", "still not json"])
        with patcher:
            r = ai_builder.plan("Leave request: employee fills in start date. Supervisor approves.", self.ws, self.owner,
                                engine="ollama:small:1b")
        self.assertEqual(r["engine"], "planner")
        self.assertIn("couldn't produce", r["notes"][0])
        r = ai_builder.plan("Leave request: employee fills in start date. Supervisor approves.", self.ws, self.owner,
                            engine="ollama:not-installed")
        self.assertEqual(r["engine"], "planner")
        self.assertIn("isn't available", r["notes"][0])

    def test_describe_page_uses_the_workspace_model(self):
        self.client.force_login(self.owner)
        self.ws.settings = {**(self.ws.settings or {}), "ai": {"engine": "ollama:small:1b"}}
        self.ws.save()
        patcher, _ = self.fake([self.model_blueprint()])
        with patcher:
            page = self.client.get(reverse("flows:ai_describe"))
            self.assertNotContains(page, 'name="engine"')
            self.assertContains(page, "Building workflow")
            r = self.client.post(reverse("flows:ai_describe"), {"action": "plan", "text": "Visitor access", "engine": "ollama:big-mlx:27b"})
        self.assertContains(r, "drafted by small:1b")
        self.assertNotIn("ai_engine", self.client.session)


SLA_BLUEPRINT = {
    "name": "Deadline test",
    "documents": [{"key": "d", "name": "Form", "fields": [
        {"id": "item", "type": "text", "label": "Item", "required": True, "filled_by": ["req"]},
        {"id": "ok", "type": "radio", "label": "Decision", "options": ["Approve", "Reject"], "filled_by": ["mgr"]}]}],
    "steps": [
        {"key": "req", "name": "Requester", "recipient": {"type": "anyone"}},
        {"key": "mgr", "name": "Manager", "recipient": {"type": "email", "email": "sam@paperlane.test"},
         "settings": {"sla": {"amount": 4, "unit": "hours", "escalate": "reassign", "escalate_to": "cal@paperlane.test",
                              "tag": "#Overdue"}}},
        {"key": "grp", "name": "Group review", "recipient": {"type": "group", "group": "Reviewers",
                                                             "members": ["sam@paperlane.test", "ana@paperlane.test"]}},
    ],
}


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class DeadlineDelegationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        from flows.blueprint import build_workflow
        self.owner = User.objects.get(username="owner")
        self.sam = User.objects.get(username="sam")
        self.cal = User.objects.get(username="cal")
        self.ws = Workspace.objects.get(slug="demo")
        self.wf, problems = build_workflow(self.ws, self.owner, SLA_BLUEPRINT, publish=True)
        self.assertEqual(problems, [])

    def start(self):
        from flows import engine
        from flows.mcp import Server
        p = engine.start_packet(self.wf, user=self.owner, source="api",
                                first_assignee={"email": "owner@paperlane.test", "name": "Owner", "user": self.owner})
        Server(self.ws, self.owner).t_complete_step(p.pk, values={"Item": "Chair"})
        return p

    def test_business_days(self):
        import datetime as dt
        from flows.sla import add_business_days
        fri = dt.datetime(2026, 10, 16, 15, 0)
        self.assertEqual(add_business_days(fri, 1), dt.datetime(2026, 10, 19, 15, 0))  # Fri → Mon
        self.assertEqual(add_business_days(fri, 2.5), dt.datetime(2026, 10, 21, 3, 0))
        sat = dt.datetime(2026, 10, 17, 9, 0)
        self.assertEqual(add_business_days(sat, 1), dt.datetime(2026, 10, 20, 9, 0))  # weekend start counts from Monday

    def test_deadline_overdue_and_reassign_escalation(self):
        import datetime as dt
        from django.core import mail
        from django.utils import timezone
        from flows.bots import tick
        from flows.models import StepRun
        p = self.start()
        run = p.runs.get(step_name="Manager")
        self.assertAlmostEqual((run.due_at - run.created_at).total_seconds(), 4 * 3600, delta=5)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("flows:builder", args=[self.wf.pk])), "Due in 4 hours")
        r = self.client.get(reverse("flows:documents"))
        self.assertContains(r, "pk-pill pk-pill-amber")  # cards (default layout)
        self.client.cookies["dl_view"] = "compact"
        self.assertContains(self.client.get(reverse("flows:documents")), "dl-pill amber")
        self.assertContains(self.client.get(reverse("flows:workflow_detail", args=[self.wf.pk])), "dl-row")
        del self.client.cookies["dl_view"]
        self.assertNotContains(self.client.get(reverse("flows:documents") + "?quick=overdue"), p.name)
        mail.outbox.clear()
        out = tick(timezone.now() + dt.timedelta(hours=5))
        self.assertEqual(out["overdue"], 1)
        run.refresh_from_db()
        self.assertEqual(run.assignee_email, "cal@paperlane.test")  # reassigned to the escalation target
        self.assertIsNotNone(run.due_at)  # with a fresh deadline
        self.assertIn("#Overdue", list(p.tags.values_list("name", flat=True)))
        self.assertTrue(any("Escalated to you" in m.subject and "cal@paperlane.test" in m.to for m in mail.outbox))
        self.assertTrue(any("Overdue" in m.subject and "sam@paperlane.test" in m.to for m in mail.outbox))
        self.assertEqual(tick(timezone.now() + dt.timedelta(hours=5))["overdue"], 0)  # not escalated twice
        # the fresh deadline passes too → escalated again (to the same person), only once
        self.assertEqual(tick(timezone.now() + dt.timedelta(hours=10))["overdue"], 1)
        # overdue filter and insights
        StepRun.objects.filter(pk=run.pk).update(due_at=timezone.now() - dt.timedelta(minutes=1), escalated_at=timezone.now())
        self.assertContains(self.client.get(reverse("flows:documents") + "?quick=overdue"), p.name)
        ins = self.client.get(reverse("flows:workflow_insights", args=[self.wf.pk]))
        self.assertContains(ins, "Manager")
        self.assertContains(ins, "1 overdue")

    def test_notify_escalation_to_group(self):
        import datetime as dt
        from django.core import mail
        from django.utils import timezone
        from flows.bots import tick
        from flows.models import Step
        st = Step.objects.get(workflow=self.wf, name="Manager")
        st.settings = {**st.settings, "sla": {"amount": 1, "unit": "days", "escalate": "notify", "escalate_to": "Reviewers"}}
        st.save()
        from flows import services
        services.publish(self.wf, self.owner)
        p = self.start()
        mail.outbox.clear()
        tick(timezone.now() + dt.timedelta(days=2))
        run = p.runs.get(step_name="Manager")
        self.assertEqual(run.assignee_email, "sam@paperlane.test")  # notify keeps the holder
        notified = [m for m in mail.outbox if m.subject.startswith("Overdue") and "ana@paperlane.test" in m.to]
        self.assertTrue(notified)

    def test_out_of_office_delegation(self):
        import datetime as dt
        from django.utils import timezone
        from core.queries import my_runs
        prof = self.sam.profile
        self.client.force_login(self.sam)
        r = self.client.post(reverse("accounts:out_of_office"), {"ooo_until": (timezone.localdate() + dt.timedelta(days=3)).isoformat(),
                                                                 "delegate": self.cal.pk, "ooo_message": "On leave"}, follow=True)
        self.assertContains(r, "You're out of office")
        self.assertContains(self.client.get(reverse("flows:documents")), "ooo-banner")  # app-wide banner
        prof.refresh_from_db()
        self.assertTrue(prof.out_of_office())
        p = self.start()
        run = p.runs.get(step_name="Manager")
        self.assertEqual((run.assignee_email, run.delegated_from), ("cal@paperlane.test", "sam@paperlane.test"))
        from flows.models import AuditEvent
        self.assertTrue(AuditEvent.objects.filter(packet=p, action="step.delegated").exists())
        # group step: cal covers sam's share — sees it in the inbox and can act
        from flows.mcp import Server
        self.client.force_login(self.cal)
        Server(self.ws, self.cal).t_complete_step(p.pk, values={"Decision": "Approve"})
        grp = p.runs.get(step_name="Group review")
        self.assertTrue(grp.can_act(self.cal))
        self.assertIn(grp.pk, list(my_runs(self.cal, self.ws).values_list("pk", flat=True)))
        # turning it off restores normal routing
        self.client.force_login(self.sam)
        self.client.post(reverse("accounts:out_of_office"), {"action": "off"})
        prof.refresh_from_db()
        self.assertFalse(prof.out_of_office())
        self.assertFalse(grp.can_act(self.cal))
        p2 = self.start()
        self.assertEqual(p2.runs.get(step_name="Manager").assignee_email, "sam@paperlane.test")

    def test_ooo_validation(self):
        self.client.force_login(self.sam)
        r = self.client.post(reverse("accounts:out_of_office"), {"ooo_until": "2000-01-01", "delegate": self.cal.pk}, follow=True)
        self.assertContains(r, "must be today or later")
        r = self.client.post(reverse("accounts:out_of_office"), {"ooo_until": "2999-01-01", "delegate": 999999}, follow=True)
        self.assertContains(r, "Choose a teammate")


class _Receiver:
    """A tiny local HTTP server that records webhook requests (answers with a programmable status)."""

    def __init__(self):
        import http.server, threading
        self.requests, self.status = [], 200
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append((self.headers, body))
                self.send_response(outer.status)
                self.end_headers()
                self.wfile.write(b"thanks")

            def log_message(self, *a):
                pass
        self.server = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class WebhookTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        from flows.blueprint import build_workflow
        self.owner = User.objects.get(username="owner")
        self.ws = Workspace.objects.get(slug="demo")
        self.wf, _ = build_workflow(self.ws, self.owner, SLA_BLUEPRINT, publish=True)
        self.rx = _Receiver()
        self.client.force_login(self.owner)

    def tearDown(self):
        self.rx.close()

    def test_settings_events_signature_and_retry(self):
        import datetime as dt, hashlib, hmac, json
        from django.utils import timezone
        from flows import engine
        from flows.bots import tick
        from flows.models import WebhookDelivery, WebhookEndpoint
        r = self.client.post(reverse("flows:webhooks"), {"url": self.rx.url, "description": "Receiver",
                                                         "events": ["document.started", "step.completed"], "include_values": "on"})
        ep = WebhookEndpoint.objects.get()
        self.assertRedirects(r, reverse("flows:webhook_detail", args=[ep.pk]))
        # test ping
        self.client.post(reverse("flows:webhook_action", args=[ep.pk, "test"]))
        self.assertEqual(len(self.rx.requests), 1)
        # run the workflow: events are queued (delivery happens after commit / by the scheduler)
        p = engine.start_packet(self.wf, user=self.owner, source="api",
                                first_assignee={"email": "owner@paperlane.test", "name": "Owner", "user": self.owner})
        from flows.mcp import Server
        Server(self.ws, self.owner).t_complete_step(p.pk, values={"Item": "Chair"})
        events = list(WebhookDelivery.objects.exclude(event="ping").values_list("event", flat=True))
        self.assertEqual(sorted(events), ["document.started", "step.completed"])  # step.sent not subscribed
        self.assertEqual(tick()["webhooks"], 2)
        headers, body = self.rx.requests[-1]
        expected = hmac.new(ep.secret.encode(), f"{headers['X-Paperlane-Timestamp']}.".encode() + body, hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-Paperlane-Signature"], f"sha256={expected}")
        payload = json.loads(body)
        self.assertEqual(payload["event"], "step.completed")
        self.assertEqual(payload["document"]["values"]["Item"], "Chair")
        self.assertEqual(payload["id"], headers["X-Paperlane-Delivery"])
        # failures are retried with backoff, then marked failed
        self.rx.status = 500
        Server(self.ws, self.owner).t_complete_step(p.pk, values={"Decision": "Approve"})
        d = WebhookDelivery.objects.get(event="step.completed", status="pending")
        now = timezone.now()
        tick(now)
        d.refresh_from_db()
        self.assertEqual((d.attempts, d.status, d.error), (1, "pending", "HTTP 500"))
        self.assertGreater(d.next_attempt_at, now)
        for i in range(10):
            tick(now + dt.timedelta(days=1 + i))
        d.refresh_from_db()
        self.assertEqual((d.status, d.attempts), ("failed", 6))
        page = self.client.get(reverse("flows:webhook_detail", args=[ep.pk]) + "?status=failed")
        self.assertContains(page, "Failed")
        # redeliver once the receiver is fixed
        self.rx.status = 204
        self.client.post(reverse("flows:webhook_action", args=[ep.pk, "redeliver"]), {"delivery": d.pk})
        self.assertTrue(WebhookDelivery.objects.filter(payload__redelivery_of=d.uid, status="ok").exists())

    def test_permissions_validation_and_isolation(self):
        from flows.models import WebhookEndpoint
        r = self.client.post(reverse("flows:webhooks"), {"url": "ftp://nope"}, follow=True)
        self.assertContains(r, "valid http(s) URL")
        self.client.force_login(User.objects.get(username="fay"))  # filler
        self.assertEqual(self.client.get(reverse("flows:webhooks")).status_code, 403)
        ep = WebhookEndpoint.objects.create(workspace=self.ws, url=self.rx.url, workflow=self.wf)
        self.assertTrue(ep.wants("step.sent", self.wf.pk))
        self.assertFalse(ep.wants("step.sent", self.wf.pk + 1000))
        # a broken webhook never breaks the workflow
        from unittest import mock
        from flows import engine
        with mock.patch("flows.webhooks.queue_for_audit", side_effect=RuntimeError("boom")):
            p = engine.start_packet(self.wf, user=self.owner, source="api")
        self.assertEqual(p.status, "in_progress")


INTEG_BLUEPRINT = {
    "name": "Integration test",
    "documents": [{"key": "d", "name": "Claim", "fields": [
        {"id": "pol", "type": "text", "label": "Policy", "required": True, "filled_by": ["req"]},
        {"id": "holder", "type": "text", "label": "Holder"},
        {"id": "fund", "type": "number", "label": "Fund"},
        {"id": "prio", "type": "radio", "label": "Priority", "options": ["Low", "High"]},
        {"id": "summary", "type": "textarea", "label": "Summary"}]}],
    "steps": [{"key": "req", "name": "Requester", "recipient": {"type": "anyone"}},
              {"key": "rev", "name": "Review", "recipient": {"type": "email", "email": "sam@paperlane.test"}}],
}


class _MockApi:
    """A tiny local JSON API: GET /policies/<id> and POST /echo."""
    def __enter__(self):
        import http.server
        import json as _json
        import threading

        seen = self.seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = _json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                seen.append(("GET", self.path, dict(self.headers)))
                if self.path.startswith("/policies/POL-1"):
                    self._send(200, {"policy": {"holder": "Pat Doe", "fund_value": 5200}})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                seen.append(("POST", self.path, dict(self.headers), _json.loads(self.rfile.read(n) or b"{}")))
                self._send(201, {"ok": True, "id": "R-9"})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def __exit__(self, *a):
        self.server.shutdown()


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ConnectorAndAITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        from flows.blueprint import build_workflow
        self.owner = User.objects.get(username="owner")
        self.ws = Workspace.objects.get(slug="demo")
        self.wf, problems = build_workflow(self.ws, self.owner, INTEG_BLUEPRINT, publish=True)
        self.assertEqual(problems, [])
        self.req = self.wf.ordered_steps()[0].key
        self.client.force_login(self.owner)

    def connector(self, url, **kw):
        from flows.models import Connector
        return Connector.objects.create(workspace=self.ws, name="Policy API", base_url=url, auth_type="bearer", secret="s3cret",
                                        actions=[{"key": "lookup", "name": "Lookup", "method": "GET", "path": "policies/{value}",
                                                  "map": [{"field": "Holder", "path": "policy.holder"},
                                                          {"field": "Fund", "path": "policy.fund_value"}]},
                                                 {"key": "push", "name": "Push", "method": "POST", "path": "echo",
                                                  "body": '{"policy": "{field:Policy}", "note": "{field:Summary}"}', "map": [
                                                      {"field": "Summary", "path": "id"}]}], **kw)

    def publish(self):
        from flows import services
        services.publish(self.wf, self.owner)

    def start(self, policy="POL-1001"):
        from flows import engine
        from flows.mcp import Server
        p = engine.start_packet(self.wf, user=self.owner, source="api",
                                first_assignee={"email": "owner@paperlane.test", "name": "Owner", "user": self.owner})
        Server(self.ws, self.owner).t_complete_step(p.pk, values={"Policy": policy})
        p.refresh_from_db()
        return p

    def test_connector_run_auth_render_and_map(self):
        from flows import connectors
        with _MockApi() as api:
            c = self.connector(api.url)
            res = connectors.run(c, "lookup", {}, value="POL-1 01")
            self.assertTrue(res["ok"])
            self.assertEqual(res["mapped"], {"Holder": "Pat Doe", "Fund": 5200})
            method, path, headers = api.seen[0]
            self.assertEqual(path, "/policies/POL-1%2001")  # {value} is URL-encoded
            self.assertEqual(headers["Authorization"], "Bearer s3cret")
            res = connectors.run(c, "push", {"Policy": 'A"1', "Summary": "line\nbreak"})
            self.assertEqual(res["status"], 201)
            self.assertEqual(api.seen[1][3], {"policy": 'A"1', "note": "line\nbreak"})  # JSON-escaped, still valid
            bad = connectors.run(c, "lookup", {}, value="NOPE")
            self.assertFalse(bad["ok"])
            self.assertEqual(bad["error"], "HTTP 404")
            c.refresh_from_db()
            self.assertIn("404", c.last_status)
        with self.assertRaises(connectors.ConnectorError):
            connectors.run(c, "missing", {})

    def test_query_auth_secret_is_masked(self):
        from flows import connectors
        with _MockApi() as api:
            c = self.connector(api.url)
            c.auth_type, c.auth_name = "query", "key"
            res = connectors.run(c, "lookup", {}, value="POL-1")
            self.assertIn("key=s3cret", api.seen[0][1])
            self.assertNotIn("s3cret", res["url"])

    def test_connector_bot_fills_fields(self):
        from flows.models import Bot
        with _MockApi() as api:
            c = self.connector(api.url)
            c.actions[0]["path"] = "policies/{field:Policy}"
            c.save()
            Bot.objects.create(workflow=self.wf, name="Lookup", bot_type="connector_action", trigger=Bot.STEP_COMPLETE,
                               step_key=self.req, config={"connector": c.pk, "action": "lookup"})
            self.publish()
            p = self.start()
            vals = p.flat_values()
            self.assertEqual(vals["Holder"], "Pat Doe")
            self.assertEqual(str(vals["Fund"]), "5200")
            log = p.bot_logs.get(bot_name="Lookup")
            self.assertIn("filled 2 field", log.message)

    def test_connector_bot_failure_is_logged_not_fatal(self):
        from flows.models import Bot
        with _MockApi() as api:
            c = self.connector(api.url)
            Bot.objects.create(workflow=self.wf, name="Lookup", bot_type="connector_action", trigger=Bot.STEP_COMPLETE,
                               step_key=self.req, config={"connector": c.pk, "action": "lookup"})
            self.publish()
            p = self.start(policy="X-404")  # the mock API answers 404
        self.assertEqual(p.runs.filter(status__in=["pending", "opened"]).first().step_name, "Review")
        self.assertIn("404", p.bot_logs.get(bot_name="Lookup").message)

    def test_form_lookup_from_connector(self):
        from flows import intake
        from flows.models import IntakeForm
        with _MockApi() as api:
            c = self.connector(api.url)
            form = IntakeForm.objects.create(workspace=self.ws, workflow=self.wf, name="F", created_by=self.owner,
                                             schema={"fields": [{"id": "p", "type": "text", "label": "Policy"},
                                                                        {"id": "h", "type": "text", "label": "Holder"}]})
            lk = {"trigger": "p", "source": "connector", "connector": c.pk, "action": "lookup", "min_length": 3,
                  "map": [{"from": "policy.holder", "to": "h"}]}
            res = intake.run_lookup(form, lk, "POL-1001")
            self.assertTrue(res["found"])
            self.assertEqual(res["values"], {"h": "Pat Doe"})
            self.assertFalse(intake.run_lookup(form, lk, "XYZ-1")["found"])

    def test_connector_pages_and_permissions(self):
        from flows.models import Connector
        r = self.client.post("/workspace/connectors/", {"name": "CRM", "base_url": "https://crm.example.com/api",
                                                        "auth_type": "header", "secret": "k"})
        c = Connector.objects.get(name="CRM")
        self.assertRedirects(r, f"/workspace/connectors/{c.pk}/")
        self.assertEqual(self.client.get(f"/workspace/connectors/{c.pk}/").status_code, 200)
        r = self.client.post(f"/workspace/connectors/{c.pk}/", {"form": "action", "name": "Find", "key": "Find Customer",
                                                                 "method": "POST", "path": "c", "body": '{"a": "{field:X}"}',
                                                                 "map": "Holder = data.name"})
        c.refresh_from_db()
        self.assertEqual(c.action("find_customer")["map"], [{"field": "Holder", "path": "data.name"}])
        self.client.post(f"/workspace/connectors/{c.pk}/", {"form": "action", "name": "Bad", "key": "bad", "method": "POST",
                                                            "body": "{not json"})
        c.refresh_from_db()
        self.assertIsNone(c.action("bad"))
        # unticking Active turns the connector off; the saved secret is kept when the field is left blank
        self.client.post(f"/workspace/connectors/{c.pk}/", {"form": "general", "name": "CRM", "base_url": c.base_url,
                                                            "auth_type": "header", "secret": ""})
        c.refresh_from_db()
        self.assertFalse(c.active)
        self.assertEqual(c.secret, "k")
        self.assertNotContains(self.client.get(f"/workspace/connectors/{c.pk}/"), 'value="k"')
        self.client.force_login(User.objects.get(username="fay"))
        self.assertEqual(self.client.get("/workspace/connectors/").status_code, 403)
        self.assertEqual(self.client.get("/workspace/ai/").status_code, 403)

    def _fake_chat(self, reply):
        from unittest import mock
        return mock.patch("flows.ai_providers.chat", return_value=reply)

    def _ai_on(self):
        self.ws.settings = {**(self.ws.settings or {}), "ai": {"engine": "ollama:test", "ask_ai": True}}
        self.ws.save()

    def test_ai_fill_and_classify_bots(self):
        from flows.models import Bot
        self._ai_on()
        Bot.objects.create(workflow=self.wf, name="AI fill", bot_type="ai_fill_fields", trigger=Bot.STEP_COMPLETE,
                           step_key=self.req, config={"targets": "Holder, Fund, Priority"})
        self.publish()
        with self._fake_chat('{"Holder": "Pat", "Fund": "$1,200", "Priority": "high"}') as chat:
            p = self.start()
        vals = p.flat_values()
        self.assertEqual((vals["Holder"], vals["Fund"], vals["Priority"]), ("Pat", "1200", "High"))  # cleaned + matched
        self.assertIn("POL-1001", chat.call_args[0][2][0]["content"])  # the document data is in the prompt
        # an option the field doesn't have is ignored
        Bot.objects.filter(name="AI fill").delete()
        Bot.objects.create(workflow=self.wf, name="AI pick", bot_type="ai_classify", trigger=Bot.STEP_COMPLETE,
                           step_key=self.req, config={"target": "Priority"})
        self.publish()
        with self._fake_chat('{"Priority": "Urgent"}'):
            p2 = self.start()
        self.assertFalse(p2.flat_values().get("Priority"))
        self.assertIn("nothing", p2.bot_logs.get(bot_name="AI pick").message)

    def test_ai_summary_bot_writes_field_and_comment(self):
        from flows.models import Bot
        self._ai_on()
        Bot.objects.create(workflow=self.wf, name="Sum", bot_type="ai_summary", trigger=Bot.STEP_COMPLETE, step_key=self.req,
                           config={"target": "Summary", "add_comment": True})
        self.publish()
        with self._fake_chat("Policy POL-1001 needs review.") as chat:
            p = self.start()
        self.assertEqual(p.flat_values()["Summary"], "Policy POL-1001 needs review.")
        self.assertEqual(p.comments.get().author_label, "AI summary")
        self.assertFalse(chat.call_args.kwargs.get("json_mode", True))

    def test_ai_disabled_or_planner_only_logs_error(self):
        from flows.models import Bot
        self.ws.settings = {**(self.ws.settings or {}), "ai": {"disabled": True}}
        self.ws.save()
        Bot.objects.create(workflow=self.wf, name="Sum", bot_type="ai_summary", trigger=Bot.STEP_COMPLETE, step_key=self.req,
                           config={"target": "Summary"})
        self.publish()
        p = self.start()
        self.assertIn("turned off", p.bot_logs.get(bot_name="Sum").message)
        self.assertEqual(p.runs.filter(status__in=["pending", "opened"]).first().step_name, "Review")

    def test_ask_ai_endpoint_and_tab(self):
        self._ai_on()
        p = self.start()
        self.assertContains(self.client.get(f"/documents/{p.pk}/?tab=ai"), "Ask AI")
        with self._fake_chat("The policy is POL-1001."):
            r = self.client.post(f"/documents/{p.pk}/ask-ai/", {"question": "Which policy?", "save": "1"})
        self.assertEqual(r.json(), {"answer": "The policy is POL-1001."})
        self.assertIn("Which policy?", p.comments.get().text)
        self.ws.settings["ai"]["ask_ai"] = False
        self.ws.save()
        self.assertEqual(self.client.post(f"/documents/{p.pk}/ask-ai/", {"question": "x"}).status_code, 403)
        self.assertNotContains(self.client.get(f"/documents/{p.pk}/"), "Ask AI")

    def test_ai_settings_page_saves(self):
        from unittest import mock
        with mock.patch("flows.ai_providers.engines", return_value=[{"id": "ollama:m", "label": "m", "group": "Ollama"}]):
            self.assertContains(self.client.get("/workspace/ai/"), "ollama:m")
            self.client.post("/workspace/ai/", {"form": "settings", "engine": "ollama:m",
                                                "ollama_host": "http://gpu-box:11434/", "ask_ai": "on"})
        self.ws.refresh_from_db()
        self.assertEqual(self.ws.settings["ai"], {"provider": "ollama", "engine": "ollama:m", "ollama_host": "http://gpu-box:11434",
                                                  "disabled": False, "ask_ai": True, "fill_assist": False, "nl_search": False, "help_ai": False,
                                              "daily_limit": 0, "embed_engine": "", "watchdog": False})
        self.client.post("/workspace/ai/", {"form": "settings", "ollama_host": "gpu-box"})
        self.ws.refresh_from_db()
        self.assertEqual(self.ws.settings["ai"]["ollama_host"], "http://gpu-box:11434")  # rejected, unchanged

    def test_workspace_ollama_host_is_used(self):
        from flows import ai_actions, ai_providers
        self.ws.settings = {**(self.ws.settings or {}), "ai": {"engine": "ollama:m", "ollama_host": "http://gpu-box:11434"}}
        self.ws.save()
        p = self.start()
        seen = []
        with __import__("unittest").mock.patch("flows.ai_providers.chat",
                                               side_effect=lambda *a, **k: seen.append(ai_providers._ollama_url()) or "ok"):
            ai_actions.ask(p, "hi")
        self.assertEqual(seen, ["http://gpu-box:11434"])
        self.assertNotEqual(ai_providers._ollama_url(), "http://gpu-box:11434")  # reset afterwards


class ConnectorMcpTests(ConnectorAndAITests):
    def test_mcp_connector_tools_and_read_only(self):
        from flows.mcp import Server, ToolError
        with _MockApi() as api:
            c = self.connector(api.url)
            srv = Server(self.ws, self.owner)
            listed = srv.t_list_connectors()
            self.assertEqual(listed[0]["actions"][0]["maps_to"], ["Holder", "Fund"])
            res = srv.t_run_connector(c.pk, "lookup", value="POL-1")
            self.assertEqual(res["mapped"]["Holder"], "Pat Doe")
            with self.assertRaises(ToolError):
                srv.t_run_connector(999, "lookup")
        self.assertIn("connector_action", srv.t_get_build_guide()["bots"] if isinstance(srv.t_get_build_guide(), dict)
                      and "bots" in srv.t_get_build_guide() else str(srv.t_get_build_guide()))


class FillPageUxTests(TestCase):
    def test_done_page_layout_and_progress_button(self):
        import pathlib
        css = pathlib.Path("static/css/app.css").read_text()
        self.assertIn(".fill-page.center-screen { display: flex; }", css)
        tpl = pathlib.Path("templates/flows/fill.html").read_text()
        self.assertIn("data-next-field", tpl)
        self.assertIn("data-doc-wrap", tpl)
        self.assertIn("data-doc-next", tpl)
        self.assertIn("Next document", tpl)
        js = pathlib.Path("static/js/fill.js").read_text()
        self.assertIn("showFillDoc", js)
        self.assertIn("revealFillField", js)
