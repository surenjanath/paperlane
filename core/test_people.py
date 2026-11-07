"""Team, groups and contacts pages: panels, group usage warnings, role/position edits, contact activity."""
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from accounts.models import Contact, Group, Membership, Workspace

User = get_user_model()


class PeopleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        self.owner = User.objects.get(username="owner")
        self.ws = Workspace.objects.get(slug="demo")
        self.client.force_login(self.owner)

    def test_team_page_and_member_panel(self):
        r = self.client.get("/team/")
        self.assertContains(r, "active teammate")
        self.assertContains(r, 'class="role-select')
        m = Membership.objects.get(workspace=self.ws, user__username="sam")
        panel = self.client.get(f"/team/{m.pk}/")
        self.assertContains(panel, "Open work")
        self.assertContains(panel, "sam@paperlane.test")
        self.client.post(f"/team/{m.pk}/position/", {"position": "Claims lead"})
        m.refresh_from_db()
        self.assertEqual(m.position, "Claims lead")
        self.assertContains(self.client.get("/team/?q=claims"), "sam@paperlane.test")
        self.client.post(f"/team/{m.pk}/role/", {"role": "auditor"})
        m.refresh_from_db()
        self.assertEqual(m.role, "auditor")
        # fillers see the page but no management controls, and can't edit positions
        self.client.force_login(User.objects.get(username="fay"))
        r = self.client.get("/team/")
        self.assertNotContains(r, 'class="role-select')
        self.assertNotContains(r, "Invite teammates")
        self.assertEqual(self.client.post(f"/team/{m.pk}/position/", {"position": "x"}).status_code, 403)

    def test_groups_usage_warning_panel_and_duplicate_names(self):
        from flows.models import Step, Workflow
        g = Group.objects.create(workspace=self.ws, name="Legal review")
        wf = Workflow.objects.filter(workspace=self.ws).first()
        Step.objects.create(workflow=wf, order=999, kind="recipient", name="Legal check", recipient_type="group", recipient_group=g)
        r = self.client.get("/team/groups/?show=attention")
        self.assertContains(r, "Legal review")
        self.assertContains(r, "no active members")
        panel = self.client.get(f"/team/groups/{g.pk}/")
        self.assertContains(panel, "Legal check")
        self.assertContains(panel, "Find a teammate")
        cal = Membership.objects.get(workspace=self.ws, user__username="cal")
        self.client.post("/team/groups/", {"id": g.pk, "name": "Legal review", "members": [cal.pk]})
        self.assertEqual(list(g.members.all()), [cal])
        self.assertNotIn(g.pk, [x.pk for x in self.client.get("/team/groups/?show=attention").context["groups"]])
        self.client.post("/team/groups/", {"name": "legal REVIEW"})
        self.assertEqual(Group.objects.filter(workspace=self.ws, name__iexact="legal review").count(), 1)
        self.assertContains(self.client.get("/team/groups/?q=cal"), "Legal review")  # search by member
        self.client.force_login(User.objects.get(username="fay"))
        self.assertEqual(self.client.post("/team/groups/", {"delete": g.pk}).status_code, 403)
        self.assertEqual(self.client.get(f"/team/groups/{g.pk}/").status_code, 403)

    def test_contacts_activity_filters_and_panel(self):
        from flows import engine
        from flows.models import Workflow
        c = Contact.objects.create(workspace=self.ws, name="Pat Client", email="Pat@Client.test", company="Acme")
        Contact.objects.create(workspace=self.ws, name="Other", email="o@x.test", company="Beta")
        wf = Workflow.objects.filter(workspace=self.ws, kind="workflow", status="published").first()
        engine.start_packet(wf, user=self.owner, source="api", first_assignee={"email": "pat@client.test", "name": "Pat"})
        r = self.client.get("/contacts/?company=Acme")
        self.assertContains(r, "Pat Client")
        self.assertNotContains(r, "o@x.test")
        self.assertEqual(r.context["contacts"][0].docs, 1)  # matched case-insensitively
        panel = self.client.get(f"/contacts/{c.pk}/")
        self.assertContains(panel, "Recent documents")
        self.assertContains(panel, wf.name)
        self.assertEqual(panel.context["doc_count"], 1)
