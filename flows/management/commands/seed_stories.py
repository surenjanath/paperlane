"""Add five realistic demo workflows with named people and documents caught at every stage.

Run after ``seed_demo``::

    python manage.py seed_stories [--workspace demo] [--replace]

Each workflow is built from a blueprint, then documents are filled step by step as the person who holds each step,
with real-looking answers and timestamps spread over the last three weeks. Some documents are finished, some are
waiting on someone, one is sent back and one is declined, so lists, insights, workload and key dates have something to
show. Emails are not sent while seeding.
"""
import base64
import datetime as dt
import io
import math
import random

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test.utils import override_settings
from django.utils import timezone

from accounts.models import Contact, Group, Membership, Profile, Workspace
from flows import engine
from flows.blueprint import build_workflow
from flows.models import AuditEvent, Packet, StepRun, Workflow

User = get_user_model()

TEAM = [  # username, first, last, role, position, groups
    ("priya", "Priya", "Patel", Membership.FILLER, "Finance officer", ["Finance"]),
    ("marcus", "Marcus", "Lee", Membership.FILLER, "HR advisor", ["HR"]),
    ("dana", "Dana", "Cruz", Membership.FILLER, "Claims adjuster", ["Claims"]),
    ("leo", "Leo", "Brooks", Membership.FILLER, "Procurement lead", ["Procurement"]),
    ("tom", "Tom", "Reyes", Membership.FILLER, "Account executive", []),
    ("grace", "Grace", "Kim", Membership.FILLER, "Operations analyst", []),
]
CLIENTS = [("Jordan Baptiste", "jordan@example.com", "Baptiste Bakery"), ("Alicia Mohammed", "alicia@example.com", ""),
           ("Kevin Ramlal", "kevin@example.com", "Ramlal Hardware"), ("Northwind Supplies", "accounts@northwind.example", "Northwind Supplies Ltd."),
           ("Bluewave IT", "hello@bluewave.example", "Bluewave IT Services"), ("Harbour Print Co.", "orders@harbourprint.example", "Harbour Print Co.")]
ME = "{field:Email}"


def em(user):
    return {"type": "email", "email": f"{user}@paperlane.test"}


def F(fid, type_, label, by, required=True, **extra):
    return {"id": fid, "type": type_, "label": label, "name": label, "required": required, "filled_by": by, **extra}


def when(field, op, value):
    return {"logic": "and", "rules": [{"field": field, "op": op, "value": value}]}


BLUEPRINTS = [
    {"name": "Expense claim", "description": "An employee claims business expenses; their manager approves and Finance pays.",
     "documents": [{"key": "claim", "name": "Expense claim", "fields": [
         F("purpose", "text", "Purpose", ["employee"]),
         F("date", "date", "Date spent", ["employee"], width="half"),
         F("category", "dropdown", "Category", ["employee"], options=["Travel", "Meals", "Client entertainment", "Software", "Office supplies"], width="half"),
         F("amount", "currency", "Amount", ["employee"], width="half"),
         F("notes", "textarea", "Notes", ["employee"], required=False),
         F("emp_sig", "signature", "Employee signature", ["employee"]),
         F("decision", "radio", "Manager decision", ["manager"], options=["Approve", "Send back"]),
         F("mgr_note", "textarea", "Manager comment", ["manager"], required=False),
         F("paid_on", "date", "Paid on", ["finance"], width="half"),
         F("pay_ref", "text", "Payment reference", ["finance"], width="half")]}],
     "steps": [{"key": "employee", "name": "Employee", "recipient": {"type": "current_user"}, "settings": {"recurrence": True}},
               {"key": "manager", "name": "Manager approval", "recipient": em("sam"),
                "jumps": [{"to": "employee", "when": when("Manager decision", "eq", "Send back")}],
                "settings": {"sla": {"amount": 2, "unit": "business_days", "escalate": "notify"}}},
               {"key": "finance", "name": "Finance payment", "recipient": {"type": "group", "group": "Finance"},
                "settings": {"sla": {"amount": 3, "unit": "business_days", "escalate": "notify"}}}]},

    {"name": "Leave request", "description": "Time off: the employee asks, the manager decides, HR records it.",
     "documents": [{"key": "leave", "name": "Leave request", "fields": [
         F("type", "dropdown", "Leave type", ["employee"], options=["Vacation", "Sick leave", "Personal day", "Parental leave"], width="half"),
         F("from", "date", "First day off", ["employee"], width="half"),
         F("to", "date", "Back at work", ["employee"], width="half"),
         F("days", "number", "Working days", ["employee"], width="half"),
         F("cover", "text", "Who covers your work", ["employee"]),
         F("decision", "radio", "Manager decision", ["manager"], options=["Approved", "Not approved"]),
         F("recorded", "checkbox", "Recorded in the HR system", ["hr"], options=["Yes"])]}],
     "steps": [{"key": "employee", "name": "Employee", "recipient": {"type": "current_user"}},
               {"key": "manager", "name": "Manager", "recipient": em("sam")},
               {"key": "hr", "name": "HR", "recipient": {"type": "group", "group": "HR"}}]},

    {"name": "Vendor onboarding", "description": "A new supplier sends its details; Procurement checks risk, high-risk vendors go to Compliance, Finance sets them up.",
     "documents": [{"key": "vendor", "name": "Vendor details", "fields": [
         F("company", "text", "Company name", ["vendor"]),
         F("email", "email", "Email", ["vendor"], width="half"),
         F("tax", "text", "Tax ID", ["vendor"], width="half"),
         F("service", "dropdown", "What you supply", ["vendor"], options=["IT services", "Printing", "Office supplies", "Cleaning", "Consulting"]),
         F("bank", "text", "Bank account", ["vendor"], sensitive=True),
         F("vendor_sig", "signature", "Authorised signature", ["vendor"]),
         F("risk", "radio", "Risk rating", ["procurement"], options=["Low", "Medium", "High"]),
         F("checks", "textarea", "Compliance checks done", ["compliance"]),
         F("vendor_id", "text", "Vendor ID", ["finance"])]}],
     "steps": [{"key": "vendor", "name": "Vendor", "recipient": {"type": "email", "email": ME}},
               {"key": "procurement", "name": "Procurement review", "recipient": {"type": "group", "group": "Procurement"}},
               {"key": "route", "type": "branch", "name": "Risk check", "lanes": [
                   {"id": "high", "label": "High risk", "when": when("Risk rating", "eq", "High"), "then": "merge",
                    "steps": [{"key": "compliance", "name": "Compliance", "recipient": em("sam")}]}]},
               {"key": "finance", "name": "Finance setup", "recipient": {"type": "group", "group": "Finance"}}]},

    {"name": "Insurance claim", "description": "A policyholder reports a loss; Claims assesses it, large claims need a senior sign-off, Finance pays.",
     "documents": [{"key": "claim", "name": "Claim", "fields": [
         F("policy", "text", "Policy number", ["claimant"], width="half"),
         F("email", "email", "Email", ["claimant"], width="half"),
         F("incident", "date", "Date of incident", ["claimant"], width="half"),
         F("kind", "dropdown", "What happened", ["claimant"], options=["Water damage", "Theft", "Fire", "Storm damage", "Accident"], width="half"),
         F("story", "textarea", "Tell us what happened", ["claimant"]),
         F("estimate", "currency", "Estimated loss", ["claimant"]),
         F("claimant_sig", "signature", "Signature", ["claimant"]),
         F("approved", "currency", "Approved amount", ["claims"], width="half"),
         F("assessment", "textarea", "Assessment", ["claims"]),
         F("senior_ok", "radio", "Senior sign-off", ["senior"], options=["Approved", "Reduce", "Reject"]),
         F("paid_on", "date", "Paid on", ["finance"])]}],
     "steps": [{"key": "claimant", "name": "Claimant", "recipient": {"type": "email", "email": ME}},
               {"key": "claims", "name": "Claims assessment", "recipient": {"type": "group", "group": "Claims"},
                "settings": {"sla": {"amount": 3, "unit": "business_days", "escalate": "notify"}}},
               {"key": "route", "type": "branch", "name": "Claim size", "lanes": [
                   {"id": "large", "label": "Over 5,000", "when": when("Approved amount", "gt", "5000"), "then": "merge",
                    "steps": [{"key": "senior", "name": "Senior sign-off", "recipient": em("sam")}]}]},
               {"key": "finance", "name": "Claim payment", "recipient": {"type": "group", "group": "Finance"}}]},

    {"name": "Purchase request", "description": "Someone needs to buy something; their manager approves and Procurement raises the order.",
     "documents": [{"key": "po", "name": "Purchase request", "fields": [
         F("item", "text", "Item", ["requester"]),
         F("supplier", "text", "Preferred supplier", ["requester"], required=False),
         F("qty", "number", "Quantity", ["requester"], width="half"),
         F("price", "currency", "Unit price", ["requester"], width="half"),
         F("total", "formula", "Total", ["requester"], required=False, formula="{qty} * {price}"),
         F("needed", "date", "Needed by", ["requester"]),
         F("decision", "radio", "Manager decision", ["manager"], options=["Approve", "Decline"]),
         F("po_number", "text", "PO number", ["procurement"])]}],
     "steps": [{"key": "requester", "name": "Requester", "recipient": {"type": "current_user"}},
               {"key": "manager", "name": "Manager approval", "recipient": em("sam")},
               {"key": "procurement", "name": "Procurement order", "recipient": {"type": "group", "group": "Procurement"}}]},
]


def _signature(name, seed):
    """A hand-drawn-looking signature as a PNG data URL: looping, slanted strokes for the first and last name."""
    from PIL import Image, ImageDraw

    rng = random.Random(seed)
    scale = 3  # draw large, then shrink for smooth edges
    w, h = 360 * scale, 110 * scale
    img = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    d = ImageDraw.Draw(img)
    ink = (24, 34, 72, 255)
    x0 = 22.0
    for part in name.split()[:2]:
        loops = max(3, min(len(part) - 1, 6))
        r = rng.uniform(8, 10.5)
        pts = []
        steps = loops * 18
        for i in range(steps + 1):
            t = i / steps * loops * 2 * math.pi
            big = 1.9 if i < 22 else 1.0  # a taller first letter
            x = x0 + t * rng.uniform(3.3, 3.7) + r * 0.8 * math.cos(t + math.pi / 2)
            y = 62 - big * r * (0.75 + 0.25 * math.sin(t * 0.37 + seed)) * math.sin(t + math.pi / 2)
            pts.append(((x - (62 - y) * 0.35) * scale, y * scale))  # slant to the right
        d.line(pts, fill=ink, width=int(2.4 * scale), joint="curve")
        x0 = pts[-1][0] / scale + 20
    d.line([(26 * scale, 86 * scale), (min(x0, 330) * scale, 80 * scale)], fill=ink, width=int(1.6 * scale))  # underline
    img = img.resize((w // scale, h // scale), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _day(offset):
    return (timezone.localdate() + dt.timedelta(days=offset)).isoformat()


class Command(BaseCommand):
    help = "Add five demo workflows with named people and documents at every stage (run after seed_demo)."

    def add_arguments(self, parser):
        parser.add_argument("--workspace", default="demo")
        parser.add_argument("--owner", default="owner")
        parser.add_argument("--replace", action="store_true", help="Delete these five workflows first and build them again")

    def handle(self, *args, workspace, owner, replace=False, **opts):
        ws = Workspace.objects.filter(slug=workspace).first()
        if not ws:
            raise CommandError(f"No workspace {workspace!r} — run seed_demo first.")
        owner_user = User.objects.filter(username=owner).first() or User.objects.filter(email__iexact=owner).first()
        if not owner_user:
            raise CommandError(f"No user {owner!r}.")
        names = [b["name"] for b in BLUEPRINTS]
        existing = Workflow.objects.filter(workspace=ws, name__in=names)
        if existing.exists():
            if not replace:
                self.stdout.write("Demo stories already exist (pass --replace to rebuild them).")
                return
            Packet.objects.filter(workflow__in=existing).delete()
            existing.delete()
        self.rng = random.Random(11)
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"), transaction.atomic():
            self.people = self._team(ws)
            wfs = {}
            for spec in BLUEPRINTS:
                wf, problems = build_workflow(ws, owner_user, spec, publish=True, strict=False)
                if problems:
                    self.stdout.write(f"{wf.name}: {problems}")
                wfs[spec["name"]] = wf
            count = self._stories(wfs)
        self.stdout.write(self.style.SUCCESS(f"Added {len(wfs)} workflows and {count} documents."))

    # ---------------------------------------------------------------- people
    def _team(self, ws):
        import os

        password = os.environ.get("PAPERLANE_DEMO_PASSWORD", "paperlane-demo-2026")
        people = {u.username: u for u in User.objects.filter(username__in=["owner", "sam", "ana", "cal", "fay"])}
        for uname, first, last, role, position, groups in TEAM:
            u, created = User.objects.get_or_create(username=uname, defaults={
                "email": f"{uname}@paperlane.test", "first_name": first, "last_name": last})
            if created:
                u.set_password(password)
                u.save()
            Profile.objects.get_or_create(user=u)
            m, _ = Membership.objects.get_or_create(user=u, workspace=ws, defaults={"role": role, "position": position})
            for g in groups:
                grp, _ = Group.objects.get_or_create(workspace=ws, name=g)
                grp.members.add(m)
            people[uname] = u
        sam = Membership.objects.filter(user=people["sam"], workspace=ws).first()
        for g in ("Finance", "HR", "Claims", "Procurement"):  # the supervisor can pick up any queue
            Group.objects.get_or_create(workspace=ws, name=g)[0].members.add(sam)
        for name, email, company in CLIENTS:
            Contact.objects.get_or_create(workspace=ws, email=email, defaults={"name": name, "company": company})
        return people

    # ---------------------------------------------------------------- documents
    def _stories(self, wfs):
        P = self.people
        sig = lambda who: _signature(who, sum(map(ord, who)))  # noqa: E731
        stories = []
        # Expense claim: employee → manager → finance (send-back loop on one)
        ex = wfs["Expense claim"]
        for who, purpose, cat, amt, days_ago, upto, extra in [
            ("fay", "Client lunch with Baptiste Bakery", "Client entertainment", "186.40", 19, 3, {}),
            ("tom", "Taxi to the Port of Spain branch", "Travel", "64.00", 15, 3, {}),
            ("grace", "Design software subscription (annual)", "Software", "1188.00", 11, 2, {}),
            ("fay", "Team offsite snacks", "Meals", "92.75", 7, 1, {"send_back": True}),
            ("tom", "Conference hotel, two nights", "Travel", "740.00", 4, 1, {}),
            ("grace", "Printer toner", "Office supplies", "143.20", 2, 0, {}),
        ]:
            emp = {"purpose": purpose, "date": _day(-days_ago - 1), "category": cat, "amount": amt,
                   "notes": "Receipt attached in the email thread." if cat != "Travel" else "", "emp_sig": sig(P[who].get_full_name())}
            steps = [("employee", P[who], emp),
                     ("manager", P["sam"], {"decision": "Approve", "mgr_note": "Fine, thanks."}),
                     ("finance", P["priya"], {"paid_on": _day(-days_ago + 3), "pay_ref": f"PAY-{2600 + days_ago:05d}"})]
            if extra.get("send_back"):
                steps = [("employee", P[who], {**emp, "amount": "127.75"}),
                         ("manager", P["sam"], {"decision": "Send back", "mgr_note": "Please split out the alcohol, it is not claimable."})]
                upto = 2
            stories.append((ex, P[who], f"Expense – {P[who].get_full_name()} – {purpose}", days_ago, steps[:upto], {}))
        # Leave request: employee → manager → HR (one declined)
        lv = wfs["Leave request"]
        for who, kind, start, length, days_ago, upto, decline in [
            ("tom", "Vacation", 24, 5, 12, 3, False), ("fay", "Personal day", 9, 1, 6, 2, False),
            ("grace", "Vacation", 40, 8, 3, 1, False), ("priya", "Sick leave", -2, 2, 2, 3, False),
            ("marcus", "Vacation", 15, 3, 5, 1, True)]:
            emp = {"type": kind, "from": _day(start), "to": _day(start + length + (2 if length >= 5 else 0)), "days": str(length),
                   "cover": self.rng.choice(["Grace Kim", "Tom Reyes", "Fay Filler", "Leo Brooks"])}
            steps = [("employee", P[who], emp), ("manager", P["sam"], {"decision": "Approved"}), ("hr", P["marcus"], {"recorded": ["Yes"]})]
            stories.append((lv, P[who], f"Leave – {P[who].get_full_name()} – {kind}", days_ago, steps[:upto],
                            {"decline_at": "manager", "reason": "Month-end close that week; please pick other dates."} if decline else {}))
        # Vendor onboarding: external vendor → procurement → (compliance if high risk) → finance
        vd = wfs["Vendor onboarding"]
        for company, email, service, risk, days_ago, upto in [
            ("Northwind Supplies Ltd.", "accounts@northwind.example", "Office supplies", "Low", 20, 3),
            ("Bluewave IT Services", "hello@bluewave.example", "IT services", "High", 13, 4),
            ("Harbour Print Co.", "orders@harbourprint.example", "Printing", "Medium", 6, 2),
            ("Spotless Cleaning", "team@spotless.example", "Cleaning", "High", 3, 2),
            ("Brightpath Consulting", "info@brightpath.example", "Consulting", "Low", 1, 0)]:
            steps = [("vendor", None, {"company": company, "email": email, "tax": f"TX-{self.rng.randint(100000, 999999)}", "service": service,
                                       "bank": f"0{self.rng.randint(10**10, 10**11 - 1)}", "vendor_sig": sig(company)}),
                     ("procurement", P["leo"], {"risk": risk})]
            if risk == "High":
                steps.append(("compliance", P["sam"], {"checks": "Sanctions list clear, references called, insurance certificate on file."}))
            steps.append(("finance", P["priya"], {"vendor_id": f"V-{3100 + days_ago}"}))
            stories.append((vd, P["leo"], f"Vendor – {company}", days_ago, steps[:upto], {"prefill": {"Email": email}}))
        # Insurance claim: claimant → claims → (senior if > 5,000) → finance
        cl = wfs["Insurance claim"]
        for name, email, kind, est, approved, days_ago, upto in [
            ("Jordan Baptiste", "jordan@example.com", "Water damage", "3800", "3250", 18, 3),
            ("Alicia Mohammed", "alicia@example.com", "Theft", "1450", "1450", 14, 3),
            ("Kevin Ramlal", "kevin@example.com", "Storm damage", "12400", "9800", 9, 3),
            ("Shania Joseph", "shania@example.com", "Accident", "6200", "5600", 5, 2),
            ("Daniel Ali", "daniel@example.com", "Fire", "2100", "", 2, 1),
            ("Jordan Baptiste", "jordan@example.com", "Theft", "900", "", 0, 0)]:
            claimant = {"policy": f"POL-{self.rng.randint(100000, 999999)}", "email": email, "incident": _day(-days_ago - 3), "kind": kind,
                        "story": {"Water damage": "A pipe burst under the kitchen sink overnight and soaked the floor and cabinets.",
                                  "Theft": "My laptop was taken from the car while it was parked at the mall.",
                                  "Storm damage": "The storm lifted part of the roof and rain came into two bedrooms.",
                                  "Accident": "Another car reversed into mine in a car park; the rear bumper and light are broken.",
                                  "Fire": "A small kitchen fire damaged the stove and the wall behind it."}[kind],
                        "estimate": est, "claimant_sig": sig(name)}
            steps = [("claimant", None, claimant),
                     ("claims", P["dana"], {"approved": approved or est, "assessment": "Photos and receipts checked; loss is covered under the policy."})]
            if float(approved or est) > 5000:
                steps.append(("senior", P["sam"], {"senior_ok": "Approved"}))
            steps.append(("finance", P["priya"], {"paid_on": _day(-days_ago + 4)}))
            stories.append((cl, P["dana"], f"Claim – {name} – {kind}", days_ago, steps[:upto], {"prefill": {"Email": email}}))
        # Purchase request: requester → manager → procurement
        po = wfs["Purchase request"]
        for who, item, supplier, qty, price, days_ago, upto in [
            ("cal", "Laptops for new starters", "Bluewave IT Services", "3", "1450", 16, 3),
            ("grace", "Standing desks", "Northwind Supplies Ltd.", "4", "520", 10, 3),
            ("tom", "Branded notebooks for the expo", "Harbour Print Co.", "250", "4.80", 6, 2),
            ("cal", "Meeting room screen", "", "1", "899", 2, 1),
            ("fay", "Ergonomic chair", "Northwind Supplies Ltd.", "1", "340", 1, 0)]:
            steps = [("requester", P[who], {"item": item, "supplier": supplier, "qty": qty, "price": price, "needed": _day(-days_ago + 21)}),
                     ("manager", P["sam"], {"decision": "Approve"}),
                     ("procurement", P["leo"], {"po_number": f"PO-{4400 + days_ago}"})]
            stories.append((po, P[who], f"Purchase – {item}", days_ago, steps[:upto], {}))
        for wf, starter, name, days_ago, steps, opts in stories:
            self._play(wf, starter, name, days_ago, steps, **opts)
        return len(stories)

    def _play(self, wf, starter, name, days_ago, steps, prefill=None, decline_at=None, reason=""):
        """Start a document and complete its steps in order, then spread the timestamps over real-looking days."""
        rng = self.rng
        t = timezone.now() - dt.timedelta(days=days_ago, hours=rng.randint(1, 7), minutes=rng.randint(0, 59))
        if timezone.localtime(t).hour < 8 and days_ago:  # office hours, but never in the future
            t += dt.timedelta(hours=8 - timezone.localtime(t).hour + rng.randint(0, 3))
        t = min(t, timezone.now() - dt.timedelta(hours=2))
        mark = AuditEvent.objects.order_by("-id").values_list("id", flat=True).first() or 0
        p = engine.start_packet(wf, user=starter, source="manual", name=name, prefill=prefill)
        timeline = [(mark, t)]
        Packet.objects.filter(pk=p.pk).update(created_at=t)
        for step_key, user, values in steps:
            run = p.runs.filter(status__in=[StepRun.PENDING, StepRun.OPENED]).order_by("id").first()
            if not run:
                break
            step = next(s for s in p.steps if s["key"] == run.step_key)
            if step["name"] != dict(self._names(wf)).get(step_key, step["name"]):
                break  # the document took another route than the story expected
            opened = t + dt.timedelta(minutes=rng.randint(20, 300))
            done = opened + dt.timedelta(minutes=rng.randint(6, 40))
            StepRun.objects.filter(pk=run.pk).update(created_at=t, opened_at=opened)
            mark = AuditEvent.objects.order_by("-id").values_list("id", flat=True).first() or 0
            by_doc = {}
            for doc in p.documents:
                ids = {f["id"] for f in doc["fields"]}
                by_doc[doc["key"]] = {k: v for k, v in values.items() if k in ids}
            engine.complete_run(run, by_doc, user=user)
            StepRun.objects.filter(pk=run.pk).update(completed_at=done)
            timeline.append((mark, done))
            p.refresh_from_db()
            t = done + dt.timedelta(hours=rng.randint(2, 30))
            if t > timezone.now():
                t = timezone.now() - dt.timedelta(minutes=rng.randint(5, 50))
        nxt = p.runs.filter(status__in=[StepRun.PENDING, StepRun.OPENED]).order_by("id").first()
        if nxt:
            last = timeline[-1][1]
            StepRun.objects.filter(pk=nxt.pk).update(created_at=last)
            if decline_at:
                mark = AuditEvent.objects.order_by("-id").values_list("id", flat=True).first() or 0
                engine.decline_run(nxt, reason, user=self.people["sam"])
                when_ = last + dt.timedelta(hours=rng.randint(2, 9))
                StepRun.objects.filter(pk=nxt.pk).update(opened_at=last + dt.timedelta(hours=1), completed_at=when_)
                timeline.append((mark, when_))
        # audit events take the time of the action that created them
        for i, (start_id, at) in enumerate(timeline):
            end_id = timeline[i + 1][0] if i + 1 < len(timeline) else None
            qs = AuditEvent.objects.filter(packet=p, id__gt=start_id)
            if end_id:
                qs = qs.filter(id__lte=end_id)
            qs.update(created_at=at)
        last = timeline[-1][1]
        p.refresh_from_db()
        Packet.objects.filter(pk=p.pk).update(last_activity_at=last, **({"completed_at": last} if p.completed_at else {}))

    @staticmethod
    def _names(wf):
        out = []
        for spec in BLUEPRINTS:
            if spec["name"] == wf.name:
                def walk(steps):
                    for s in steps:
                        if s.get("type") == "branch":
                            for lane in s["lanes"]:
                                walk(lane.get("steps", []))
                        else:
                            out.append((s["key"], s["name"]))
                walk(spec["steps"])
        return out
