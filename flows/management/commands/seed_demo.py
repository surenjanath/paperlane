"""Create a demo workspace with teammates, contacts, workflows and documents in every state.

Credentials are read from PAPERLANE_DEMO_PASSWORD (default documented in README.md).
"""
import datetime as dt
import itertools
import os
import random

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import Contact, Group, Membership, Profile, Workspace
from core.views import seed_widgets
from flows import engine, services
from flows.models import StepRun
from flows.presets import PRESETS, create_from_preset

User = get_user_model()


class Command(BaseCommand):
    help = "Seed a demo Paperlane workspace (idempotent)."

    def handle(self, *args, **opts):
        password = os.environ.get("PAPERLANE_DEMO_PASSWORD", "paperlane-demo-2026")
        owner, created = User.objects.get_or_create(username="owner", defaults={
            "email": "owner@paperlane.test", "first_name": "Olivia", "last_name": "Owner", "is_staff": True, "is_superuser": True})
        if created:
            owner.set_password(password)
            owner.save()
        Profile.objects.get_or_create(user=owner)
        ws, _ = Workspace.objects.get_or_create(slug="demo", defaults={
            "name": "Demo Insurance Ltd.", "industry": "Insurance", "department": "Operations", "size": "6-50",
            "plan_expires": (timezone.now() + timezone.timedelta(days=350)).date(), "verified_domains": "paperlane.test"})
        Membership.objects.get_or_create(user=owner, workspace=ws, defaults={"role": Membership.OWNER, "position": "Administrator"})
        people = [("sam", "Sam", "Supervisor", Membership.SUPERVISOR), ("ana", "Ana", "Auditor", Membership.AUDITOR),
                  ("cal", "Cal", "Creator", Membership.CREATOR), ("fay", "Fay", "Filler", Membership.FILLER)]
        team = []
        for uname, first, last, role in people:
            u, c = User.objects.get_or_create(username=uname, defaults={"email": f"{uname}@paperlane.test", "first_name": first, "last_name": last})
            if c:
                u.set_password(password)
                u.save()
            Profile.objects.get_or_create(user=u)
            m, _ = Membership.objects.get_or_create(user=u, workspace=ws, defaults={"role": role})
            team.append(m)
        g, _ = Group.objects.get_or_create(workspace=ws, name="Finance team")
        g.members.add(*team[:2])
        for name, email, company in [("Jordan Client", "jordan@example.com", "Client Co"), ("Riley Broker", "riley@example.com", "Broker LLC"),
                                     ("Morgan Agent", "morgan@example.com", "")]:
            Contact.objects.get_or_create(workspace=ws, email=email, defaults={"name": name, "company": company})
        seed_widgets(owner, ws)
        if ws.workflows.exists():
            self.stdout.write("Demo workspace already seeded.")
            return
        rng = random.Random(7)
        for key in PRESETS:
            wf = create_from_preset(key, ws, owner)
            steps = [s for s in wf.ordered_steps() if s.kind == "recipient"]
            steps[0].recipient_type = "current_user"
            steps[0].save()
            for s in steps[1:]:
                s.recipient_email = "sam@paperlane.test"
                s.recipient_name = "Sam Supervisor"
                s.save()
            services.publish(wf, owner)
            for i in range(rng.randint(2, 5)):
                p = engine.start_packet(wf, user=owner, source="manual")
                when = timezone.now() - timezone.timedelta(days=rng.randint(0, 6), hours=rng.randint(0, 20))
                p.created_at = when
                p.save(update_fields=["created_at"])
                if i % 3 == 0:
                    continue
                # complete as many steps as possible with generated values
                for _ in range(5):
                    run = p.runs.filter(status__in=[StepRun.PENDING, StepRun.OPENED]).first()
                    if not run:
                        break
                    vals = {}
                    for doc in p.documents:
                        ids = engine.editable_field_ids(p, run.step_key).get(doc["key"], set())
                        vals[doc["key"]] = {fid: _sample(f) for f in doc["fields"] for fid in [f["id"]] if fid in ids and _sample(f) is not None}
                    engine.complete_run(run, vals, user=run.assignee_user)
                    p.refresh_from_db()
        tpl = services.create_workflow(ws, owner, name="Beneficiary change request", kind="template")
        services.publish(tpl, owner)
        self.stdout.write(self.style.SUCCESS("Demo workspace seeded. Log in as owner@paperlane.test (see README for the password)."))


_DATE_STEP = itertools.count()


def _sample_date():
    """Sample dates a few weeks ahead of today, spaced apart, so a fresh demo never starts out overdue."""
    return (timezone.localdate() + dt.timedelta(days=12 + 9 * (next(_DATE_STEP) % 12))).isoformat()


def _sample(f):
    t = f.get("type")
    return {
        "text": "Sample text", "textarea": "Longer sample answer.", "email": "person@example.com", "date": _sample_date(),
        "number": "3", "currency": "1250", "phone": "+1 868 555 0100", "radio": (f.get("options") or ["Yes"])[0],
        "dropdown": (f.get("options") or ["Yes"])[0], "checkbox": (f.get("options") or [])[:1] or True,
        "rating": "4", "clickwrap": True,
        "signature": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=",
        "full_name": {"first_name": "Jordan", "last_name": "Client"},
        "address": {"street": "1 Main Street", "city": "Port of Spain", "country": "Trinidad and Tobago"},
        "table": [{c: "1" for c in f.get("columns", [])}],
    }.get(t)
