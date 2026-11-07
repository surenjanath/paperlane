"""Paperlane workflow engine: starts packets, routes steps, evaluates branches and completes runs."""
from django.contrib.auth import get_user_model
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from accounts.models import Group, UsageEvent

from . import mailer
from .conditions import evaluate
from .fieldtypes import field_roles
from .models import AuditEvent, Packet, PacketRevision, StepRun, Tag, Workflow

User = get_user_model()
MAX_HOPS = 200


# ---- infrastructure helpers -----------------------------------------------------
def client_ip(request):
    if request is None:
        return None
    fwd = request.META.get("HTTP_X_FORWARDED_FOR")
    return (fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR")) or None


def actor_label(user=None, fallback=""):
    if user is not None and getattr(user, "is_authenticated", False):
        return user.get_full_name() or user.email or user.username
    return fallback or "System"


def audit(workspace, action, detail="", *, actor=None, packet=None, workflow=None, request=None, label=""):
    if workflow is None and packet is not None:
        workflow = packet.workflow
    event = AuditEvent.objects.create(
        workspace=workspace, workflow=workflow, packet=packet,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        actor_label=label or actor_label(actor), action=action, detail=detail, ip=client_ip(request),
    )
    try:
        from .webhooks import queue_for_audit

        queue_for_audit(event)
    except Exception:  # a webhook problem must never stop the workflow
        import logging

        logging.getLogger("paperlane.webhooks").exception("queueing webhooks for %s failed", action)
    return event


def notify(user, workspace, title, body="", link="", kind="info"):
    from core.models import Notification

    if user is None:
        return None
    profile = getattr(user, "profile", None)
    if profile is not None and not profile.wants(kind):
        return None
    return Notification.objects.create(user=user, workspace=workspace, title=title, body=body, link=link, kind=kind)


def charge(workspace, kind, workflow=None, credits=1):
    UsageEvent.objects.create(workspace=workspace, workflow=workflow, workflow_name=workflow.name if workflow else "",
                              kind=kind, credits=credits)


def absolute(path, request=None):
    """Absolute URL. A page uses the address in the browser; emails and jobs use SITE_URL."""
    from django.conf import settings

    if request is not None:
        return request.build_absolute_uri(path)
    return settings.SITE_URL.rstrip("/") + path


def packet_link(packet):
    return absolute(reverse("flows:packet_detail", args=[packet.pk]))


# ---- starting -------------------------------------------------------------------
def default_packet_name(workflow, prefill=None):
    stamp = timezone.localtime().strftime("%d/%m/%Y %H:%M")
    return f"{workflow.name} – {stamp}"


def apply_prefill(packet, prefill):
    """``prefill`` maps field id / name / label → value; written into the proper document slots."""
    if not prefill:
        return 0
    lowered = {str(k).strip().lower(): v for k, v in prefill.items()}
    data = packet.data or {}
    count = 0
    for doc in packet.documents:
        slot = data.setdefault(doc["key"], {})
        for f in doc.get("fields", []):
            for candidate in (f.get("id"), f.get("name"), f.get("label")):
                if candidate and str(candidate).strip().lower() in lowered:
                    slot[f["id"]] = lowered[str(candidate).strip().lower()]
                    count += 1
                    break
    packet.data = data
    return count


@transaction.atomic
def start_packet(workflow, *, user=None, source="manual", name=None, prefill=None, is_test=False,
                 first_assignee=None, request=None, tags=None):
    """Create a packet (a "document" in the UI) from a workflow and route it to the first step."""
    snapshot = workflow.build_snapshot() if (is_test or not workflow.is_published) else workflow.runnable_snapshot()
    packet = Packet.objects.create(
        workspace=workflow.workspace, workflow=workflow, name=name or default_packet_name(workflow, prefill),
        version=workflow.version, definition=snapshot, source=source, is_test=is_test,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )
    if prefill:
        apply_prefill(packet, prefill)
        packet.save(update_fields=["data"])
    for t in tags or []:
        tag, _ = Tag.objects.get_or_create(workspace=workflow.workspace, name=t.strip()[:80])
        packet.tags.add(tag)
    if not is_test:
        charge(workflow.workspace, UsageEvent.SEND, workflow)
    audit(workflow.workspace, "packet.created", f"Started from {source} (version {workflow.version})",
          actor=user, packet=packet, request=request)
    _duplicate_check(packet, user)
    from .bots import run_bots

    run_bots(packet, "flow_start", context={"@status": "STARTED", "@revision": 0})
    packet.refresh_from_db()
    if is_bot_routed(packet) and first_assignee is None and open_runs(packet).exists():
        return packet  # a routing bot already sent the documents
    advance(packet, None, actor=user, first_assignee=first_assignee, request=request)
    return packet


OPEN = ("pending", "opened")


def _duplicate_check(packet, user=None):
    from . import duplicates

    if duplicates.config(packet.workflow):
        try:
            duplicates.check(packet, actor=user)
        except Exception:  # never block routing over a duplicate check
            import logging

            logging.getLogger("paperlane").exception("duplicate check failed")


def condition_context(packet, *, user=None, run=None):
    """Special condition values shared by steps, branches, documents and bots.

    * When the recipient — ``@user.email/.first_name/.last_name/.position/.role`` of the latest acting recipient
    * When the previous Step — ``@prev.status`` (completed / declined)
    * When document data — ``@revision`` (number) and ``@tag`` (list of tag names)
    * When the date — ``@date``, ``@time``, ``@datetime``, ``@day`` (weekday), ``@month`` in the workspace timezone
    * When the number of filled fields in — ``@filled:<doc key>``; document-wide field checks —
      ``@all_fillable:<doc key>`` / ``@all_required:<doc key>`` (lists of values)
    """
    from accounts.models import Membership

    if run is None:
        run = packet.runs.filter(status__in=[StepRun.COMPLETED, StepRun.DECLINED]).order_by("-completed_at", "-id").first() \
            if packet.pk else None
    acting = user if user is not None and getattr(user, "is_authenticated", False) else None
    if acting is None and run is not None:
        acting = run.assignee_user or (get_user_model().objects.filter(email__iexact=run.assignee_email).first()
                                       if run.assignee_email else None)
    email = (acting.email if acting else "") or (run.assignee_email if run else "")
    m = Membership.objects.filter(workspace=packet.workspace, user=acting).first() if acting else None
    now = timezone.localtime()
    ctx = {
        "@user.email": email,
        "@user.first_name": acting.first_name if acting else "",
        "@user.last_name": acting.last_name if acting else "",
        "@user.position": m.position if m else "",
        "@user.role": m.get_role_display() if m else "",
        "@prev.status": run.status if run else "",
        "@revision": packet.revisions.count() if packet.pk else 0,
        "@duplicate": "yes" if (packet.flags or {}).get("duplicates") else "no",  # duplicate check (workflow settings)
        "@tag": list(packet.tags.values_list("name", flat=True)) if packet.pk else [],
        "@date": now.date().isoformat(), "@time": now.strftime("%H:%M"), "@datetime": now.strftime("%Y-%m-%dT%H:%M"),
        "@day": now.strftime("%A"), "@month": now.strftime("%B"),
    }
    from .fieldtypes import is_input

    for doc in packet.documents:
        vals = (packet.data or {}).get(doc["key"], {})
        inputs = [f for f in doc.get("fields", []) if is_input(f.get("type")) and f.get("type") != "formula"]
        filled = [vals.get(f["id"]) for f in inputs]
        # “number of filled fields” also counts calculated (formula) fields that have a value
        counted = [vals.get(f["id"]) for f in doc.get("fields", []) if is_input(f.get("type")) or f.get("type") == "formula"]
        ctx[f"@filled:{doc['key']}"] = sum(1 for v in counted if v not in (None, "", [], {}, False))
        ctx[f"@all_fillable:{doc['key']}"] = filled
        ctx[f"@all_required:{doc['key']}"] = [vals.get(f["id"]) for f in inputs if f.get("required")]
    return ctx


def bot_context(packet, step_key, status, run=None, user=None):
    """Condition context for bots: acting step (+aliases), result status, documents and acting user."""
    email = (getattr(user, "email", "") if user is not None and getattr(user, "is_authenticated", False) else "") or (
        run.assignee_email if run else "")
    name = actor_label(user, run.assignee_name if run else "") if (user or run) else ""
    return {"@step": step_ids(packet, step_key), "@status": status, "@user.email": email, "@user.name": name,
            "@document": [d["key"] for d in visible_documents(packet, step_key)]}


def step_ids(packet, step_key):
    """The acting step for conditions: its key plus any role IDs it carries as aliases (imports)."""
    step = packet.step(step_key) or {}
    return [step_key] + list((step.get("settings") or {}).get("aliases") or [])


def open_runs(packet):
    return packet.runs.filter(status__in=OPEN)


def is_bot_routed(packet):
    return packet.definition.get("routing_mode") == Workflow.ROUTING_BOTS


# ---- routing ----------------------------------------------------------------------
def _resolve_assignees(packet, step, first_assignee=None):
    """Return list of dicts {email, name, user, group} for a recipient step."""
    rtype = step.get("recipient_type")
    if first_assignee is not None:
        return [first_assignee]
    if packet.is_test and packet.created_by:  # test runs route every step to the tester
        u = packet.created_by
        return [{"email": u.email, "name": actor_label(u), "user": u}]
    if rtype == "current_user":
        u = packet.created_by
        return [{"email": u.email if u else "", "name": actor_label(u) if u else "", "user": u}]
    if rtype == "contact":
        c = step.get("recipient_contact") or {}
        email = c.get("email", "")
        return [{"email": email, "name": c.get("name", ""), "user": User.objects.filter(email__iexact=email).first() if email else None}]
    if rtype == "group":
        g = step.get("recipient_group") or {}
        group = Group.objects.filter(pk=g.get("id")).first()
        return [{"email": "", "name": group.name if group else "Group", "user": None, "group": group}]
    if rtype == "anyone":
        return [{"email": "", "name": "Anyone with the link", "user": None}]
    out = []
    for raw in (step.get("recipient_email") or "").replace(";", ",").split(","):
        email = raw.strip()
        if not email:
            continue
        # allow {field:Email} style dynamic recipients
        if email.startswith("{") and email.endswith("}"):
            email = str(packet.flat_values().get(email.strip("{}").replace("field:", "").strip(), "")).strip()
            if not email:
                continue
        out.append({"email": email, "name": step.get("recipient_name") or email,
                    "user": User.objects.filter(email__iexact=email).first()})
    if not out and packet.created_by:  # nobody configured → fall back to the sender
        u = packet.created_by
        out.append({"email": u.email, "name": actor_label(u), "user": u})
    return out


def _activate_step(packet, step, *, actor=None, first_assignee=None, request=None, assignees=None,
                   subject=None, body=None):
    from .bots import step_access_expiry

    runs = []
    automated = bool((step.get("settings") or {}).get("auto_complete"))
    if automated:
        targets = [{"email": "", "name": "Paperlane bot", "user": None}]
    else:
        from .sla import apply_delegation

        targets = apply_delegation(packet, assignees or _resolve_assignees(packet, step, first_assignee))
    from .sla import due_at, group_recipients

    for a in targets:
        run = StepRun.objects.create(
            packet=packet, step_key=step["key"], step_name=step["name"],
            assignee_email=a.get("email", ""), assignee_name=a.get("name", ""),
            assignee_user=a.get("user"), assignee_group=a.get("group"),
            expires_at=step_access_expiry(step), due_at=None if automated else due_at(step),
            delegated_from=a.get("delegated_from", ""),
        )
        if run.delegated_from:
            audit(packet.workspace, "step.delegated", f"{step['name']}: {run.delegated_from} is out of office → {run.assignee_email}",
                  actor=actor, packet=packet, request=request)
        runs.append(run)
        settings_ = step.get("settings") or {}
        values = packet.flat_values()
        ctx = {
            "user_name": run.assignee_name or run.assignee_email or "there",
            "workflow_name": packet.definition.get("name", packet.workflow.name),
            "document_name": packet.name, "template_name": packet.workflow.name,
            "link": run.absolute_fill_url(), "message": "", "fields": values,
            "sender_name": actor_label(packet.created_by),
        }
        ctx["message"] = mailer.render(body if body is not None else (step.get("message_body") or ""), ctx)
        if run.assignee_email and not run.assignee_group_id and settings_.get("one_tap", True):
            taps = one_tap_links(run)
            if taps:  # the decision is all this step needs: let them decide from the email
                ctx["message"] = (ctx["message"] + "\n\nDecide straight from this email:\n"
                                  + "\n".join(f"{label}: {url}" for label, url in taps)).strip()
        group_emails = group_recipients(run.assignee_group, packet.workspace)[0] if run.assignee_group else []
        if group_emails and not packet.is_test and not automated:
            mailer.send(packet.workspace, group_emails, "invite", ctx, packet=packet,
                        subject=mailer.render(subject, ctx) if subject else (step.get("message_subject") or None))
        if run.assignee_email and (first_assignee is None or assignees) and not packet.is_test and not automated:
            attachments = None
            if step.get("attach_documents"):
                from .exports import packet_pdf

                pdf = packet_pdf(packet)
                if pdf:
                    attachments = [(f"{packet.name}.pdf", pdf, "application/pdf")]
            mailer.send(packet.workspace, run.assignee_email, "invite", ctx, packet=packet,
                        subject=mailer.render(subject, ctx) if subject else (step.get("message_subject") or None),
                        attachments=attachments)
        targets = []
        if run.assignee_user:
            targets = [run.assignee_user]
        elif run.assignee_group:
            targets = group_recipients(run.assignee_group, packet.workspace)[1]
        for u in targets:
            notify(u, packet.workspace, f"{packet.name} is waiting for you",
                   f"Step: {step['name']}", run.fill_url(), kind="assigned")
        if settings_.get("tags"):
            for t in settings_["tags"]:
                tag, _ = Tag.objects.get_or_create(workspace=packet.workspace, name=str(t).strip()[:80])
                packet.tags.add(tag)
    packet.current_step_key = step["key"]
    packet.last_activity_at = timezone.now()
    packet.save(update_fields=["current_step_key", "last_activity_at"])
    audit(packet.workspace, "step.sent",
          f"Sent to {step['name']} ({', '.join(r.assignee_email or r.assignee_name for r in runs)})",
          actor=actor, packet=packet, request=request)
    from .bots import run_bots

    run_bots(packet, "step_start", step_key=step["key"], context=bot_context(packet, step["key"], "SENT", runs[0] if runs else None))
    if automated:
        packet.refresh_from_db()
        for run in runs:
            run.refresh_from_db()
            if run.is_open:
                complete_run(run, {}, user=None, request=request, label="Paperlane bot (automated step)")
    return runs


def route(packet, step_key, *, assignees=None, subject=None, body=None, actor=None, request=None):
    """Send ``packet`` to ``step_key`` (used by routing bots and reassign bots)."""
    step = packet.step(step_key)
    if step is None or step.get("kind") != "recipient":
        return []
    if packet.status != Packet.IN_PROGRESS:
        packet.status = Packet.IN_PROGRESS
        packet.completed_at = None
        packet.save(update_fields=["status", "completed_at"])
    visited = list(packet.visited or [])
    visited.append(step_key)
    packet.visited = visited
    packet.save(update_fields=["visited"])
    return _activate_step(packet, step, actor=actor, request=request, assignees=assignees, subject=subject, body=body)


def advance(packet, after_key, *, actor=None, first_assignee=None, request=None, jump_to=None):
    """Move ``packet`` forward from step ``after_key`` (None = beginning), following branch lanes."""
    from . import diagram
    from .bots import run_bots

    steps = packet.steps
    by_key = {s["key"]: s for s in steps}
    bypass = None  # a step reached through a jump runs on the jump's conditions, not its own
    if jump_to is not None and jump_to in by_key:
        cur = jump_to
    elif after_key in by_key:
        cur = diagram.successor(steps, after_key)
        values = {**packet.flat_values(), **condition_context(packet)}
        for j in (by_key[after_key].get("settings") or {}).get("jumps") or []:
            if j.get("to") in by_key and evaluate({"logic": j.get("logic", "and"), "rules": j.get("rules") or []}, values):
                cur = bypass = j["to"]
                audit(packet.workspace, "step.jump", f"{by_key[after_key]['name']} → {by_key[cur]['name']}", packet=packet)
                break
    else:
        cur = diagram.first(steps)
    hops = 0
    while cur != diagram.END and cur in by_key and hops < MAX_HOPS:
        hops += 1
        step = by_key[cur]
        kind = step.get("kind")
        values = {**packet.flat_values(), **condition_context(packet)}
        visited = list(packet.visited or [])
        # branches are routing, so they are evaluated every time; other steps run again only with recurrence
        if kind != "branch" and cur in visited and not (step.get("settings") or {}).get("recurrence"):
            cur = diagram.successor(steps, cur)
            continue
        if cur != bypass and not evaluate(step.get("condition"), values):
            audit(packet.workspace, "step.skipped", f"Condition not met for {step['name']}", packet=packet)
            cur = diagram.successor(steps, cur)
            continue
        visited.append(cur)
        packet.visited = visited
        packet.save(update_fields=["visited"])
        if kind == "bot":
            run_bots(packet, "inline", step_key=cur)
            packet.refresh_from_db()
            cur = diagram.successor(steps, cur)
            continue
        if kind == "branch":
            nxt = None
            for i, br in enumerate(step.get("branches") or []):
                if evaluate({"logic": br.get("logic", "and"), "rules": br.get("rules", [])}, values):
                    nxt = diagram.branch_target(steps, step, i)
                    audit(packet.workspace, "branch.matched", f"{step['name']}: {br.get('label', 'branch')}", packet=packet)
                    break
            cur = nxt if nxt is not None else diagram.successor(steps, cur)  # no lane matched → continue after them
            continue
        _activate_step(packet, step, actor=actor, first_assignee=first_assignee, request=request)
        return packet
    complete_packet(packet, actor=actor, request=request)
    return packet


def one_tap_links(run):
    """[(option, absolute url)] when the step's only job is a decision (see views.approvals.plan), else []."""
    from .views.approvals import plan

    pl = plan(run)
    if not pl or pl["blocking"] or (pl["comment"] and pl["comment"].get("required")):
        return []
    opts = pl["decision"]["field"].get("options") or []
    return [(o, absolute(reverse("flows:fill_decide", args=[run.token, i]))) for i, o in enumerate(opts[:4])]


def complete_packet(packet, *, actor=None, request=None):
    from .bots import run_bots

    packet.status = Packet.COMPLETED
    packet.current_step_key = ""
    packet.completed_at = timezone.now()
    packet.last_activity_at = timezone.now()
    packet.save(update_fields=["status", "current_step_key", "completed_at", "last_activity_at"])
    audit(packet.workspace, "packet.completed", "All steps completed", actor=actor, packet=packet, request=request)
    run_bots(packet, "flow_complete")
    packet.refresh_from_db()
    from .simulator import SIMULATING

    try:  # tamper evidence: fingerprint + frozen PDF (never blocks completion; pointless in a rolled-back simulation)
        if SIMULATING.get() is None:
            from .sealing import seal

            s = seal(packet)
            audit(packet.workspace, "packet.sealed", f"Sealed · code {s.code}", packet=packet)
    except Exception as exc:  # noqa: BLE001
        audit(packet.workspace, "packet.seal_failed", str(exc)[:200], packet=packet)
    if SIMULATING.get() is None and not packet.is_test:
        from .keydates import capture

        capture(packet)  # renewal / expiry dates the workflow tracks
    if packet.created_by:
        notify(packet.created_by, packet.workspace, f"{packet.name} has been completed", "",
               reverse("flows:packet_detail", args=[packet.pk]), kind="completed")
        mailer.send(packet.workspace, packet.created_by.email, "completed", {
            "user_name": actor_label(packet.created_by), "document_name": packet.name,
            "link": packet_link(packet), "fields": packet.flat_values(),
        }, packet=packet)
    # chained workflows ("From another workflow" autostart)
    if not packet.is_test:
        from .autostart import chained_targets

        for wf in chained_targets(packet.workspace, packet.workflow_id):
            child = start_packet(wf, user=packet.created_by, source="workflow", prefill=packet.flat_values())
            audit(packet.workspace, "packet.chained", f"Started {child.name}", packet=packet)


# ---- recipient actions ------------------------------------------------------------
def editable_field_ids(packet, step_key):
    """Field ids the recipient of ``step_key`` may edit, keyed by document key."""
    step = packet.step(step_key) or {}
    access = step.get("document_access") or {}
    out = {}
    for doc in packet.documents:
        if access.get(doc["key"], "fill") != "fill":
            continue
        ids = set()
        for f in doc.get("fields", []):
            if f.get("readonly") or f.get("hidden"):
                continue
            roles = field_roles(f)
            if "anyone" in roles or step_key in roles:
                ids.add(f["id"])
        out[doc["key"]] = ids
    return out


def visible_documents(packet, step_key):
    step = packet.step(step_key) or {}
    access = step.get("document_access") or {}
    values = {**packet.flat_values(), **condition_context(packet)}
    docs = []
    for doc in packet.documents:
        if access.get(doc["key"], "fill") == "hidden":
            continue
        if not evaluate(doc.get("condition"), values):
            continue
        docs.append(doc)
    return docs


def recompute_formulas(packet):
    """Formula fields are derived values: recompute them from the current data (twice for chained formulas)."""
    from .conditions import compute_formula

    for _ in range(2):
        values = packet.flat_values()
        for doc in packet.documents:
            for f in doc.get("fields", []):
                if f.get("type") == "formula" and f.get("formula"):
                    result = compute_formula(f["formula"], values)
                    if result != "":
                        packet.data.setdefault(doc["key"], {})[f["id"]] = result


def save_revision(packet, *, run=None, user=None, note=""):
    number = (packet.revisions.order_by("-number").values_list("number", flat=True).first() or 0) + 1
    return PacketRevision.objects.create(
        packet=packet, number=number, data=packet.data, step_key=run.step_key if run else "",
        step_name=run.step_name if run else "", author=user if getattr(user, "is_authenticated", False) else None,
        author_label=actor_label(user, run.assignee_email if run else ""), note=note,
    )


@transaction.atomic
def complete_run(run, values_by_doc, *, user=None, request=None, label=None):
    from .bots import run_bots

    if run.status == StepRun.PENDING and not label:
        mark_opened(run, user=user, request=request)  # submissions that skip the fill page still pre-fill first
        run.refresh_from_db()

    packet = run.packet
    data = packet.data or {}
    for doc_key, vals in values_by_doc.items():
        data.setdefault(doc_key, {}).update(vals)
    packet.data = data
    recompute_formulas(packet)
    packet.last_activity_at = timezone.now()
    from .history import changed_by

    with changed_by(user, label or actor_label(user, run.assignee_email or run.assignee_name), f"Step: {run.step_name}"):
        packet.save(update_fields=["data", "last_activity_at"])
    _duplicate_check(packet, user)
    run.status = StepRun.COMPLETED
    run.completed_at = timezone.now()
    run.draft = {}
    if user is not None and getattr(user, "is_authenticated", False) and not run.assignee_user_id:
        run.assignee_user = user
        run.assignee_email = run.assignee_email or user.email
    run.save()
    save_revision(packet, run=run, user=user, note=f"Completed {run.step_name}")
    who = label or actor_label(user, run.assignee_email or run.assignee_name)
    audit(packet.workspace, "step.completed", f"{who} completed {run.step_name}", actor=user, packet=packet,
          request=request, label=who)
    step = packet.step(run.step_key) or {}
    # other parallel recipients of the same step finish together (first submit wins)
    packet.runs.filter(step_key=run.step_key, status__in=OPEN).exclude(pk=run.pk).update(status=StepRun.SKIPPED)
    run_bots(packet, "step_complete", step_key=run.step_key, context={**bot_context(packet, run.step_key, "FILLED", run, user), "@user.name": who})
    packet.refresh_from_db()
    try:  # the watchdog never blocks a step
        from .simulator import SIMULATING
        from .watchdog import inspect

        if SIMULATING.get() is None and not packet.is_test:
            inspect(packet)
    except Exception as exc:  # noqa: BLE001
        audit(packet.workspace, "watchdog.failed", str(exc)[:200], packet=packet)
    notify_emails = (step.get("settings") or {}).get("notify_emails")
    if notify_emails:
        mailer.send(packet.workspace, notify_emails, "step_completed", {
            "user_name": "there", "actor_name": who, "step_name": run.step_name,
            "document_name": packet.name, "link": packet_link(packet), "fields": packet.flat_values(),
        }, packet=packet)
    if packet.created_by and packet.created_by != user:
        notify(packet.created_by, packet.workspace, f"{who} completed {run.step_name}", packet.name,
               reverse("flows:packet_detail", args=[packet.pk]), kind="completed")
    if packet.status == Packet.IN_PROGRESS:
        if is_bot_routed(packet):
            if not open_runs(packet).exists():
                complete_packet(packet, actor=user, request=request)
        else:
            advance(packet, run.step_key, actor=user, request=request)
    return packet


def decline_run(run, reason, *, user=None, request=None):
    packet = run.packet
    run.status = StepRun.DECLINED
    run.decline_reason = reason
    run.completed_at = timezone.now()
    run.save()
    who = actor_label(user, run.assignee_email or run.assignee_name)
    audit(packet.workspace, "step.declined", f"{who} declined: {reason}", actor=user, packet=packet, request=request, label=who)
    if is_bot_routed(packet):
        from .bots import run_bots

        run_bots(packet, "step_complete", step_key=run.step_key, context={**bot_context(packet, run.step_key, "DECLINED", run, user), "@user.name": who})
        packet.refresh_from_db()
        if open_runs(packet).exists() or packet.bot_jobs.filter(done=False).exists():
            packet.last_activity_at = timezone.now()
            packet.save(update_fields=["last_activity_at"])
            return
    packet.status = Packet.DECLINED
    packet.last_activity_at = timezone.now()
    packet.save(update_fields=["status", "last_activity_at"])
    if packet.created_by:
        notify(packet.created_by, packet.workspace, f"{who} declined {packet.name}", reason,
               reverse("flows:packet_detail", args=[packet.pk]), kind="declined")
        mailer.send(packet.workspace, packet.created_by.email, "declined", {
            "user_name": actor_label(packet.created_by), "actor_name": who, "reason": reason or "—",
            "document_name": packet.name, "link": packet_link(packet),
        }, packet=packet)


def revise(packet, step_key, *, user=None, request=None):
    """Re-open a packet at ``step_key`` (the "Revise" action)."""
    step = packet.step(step_key)
    if not step or step["kind"] != "recipient":
        return None
    packet.runs.filter(status__in=[StepRun.PENDING, StepRun.OPENED]).update(status=StepRun.REVOKED)
    packet.status = Packet.IN_PROGRESS
    packet.completed_at = None
    packet.visited = [k for k in (packet.visited or []) if k != step_key]
    packet.save(update_fields=["status", "completed_at", "visited"])
    audit(packet.workspace, "packet.revised", f"Revision requested at {step['name']}", actor=user, packet=packet, request=request)
    advance(packet, None, actor=user, request=request, jump_to=step_key)
    return packet


def cancel_packet(packet, *, user=None, request=None):
    packet.runs.filter(status__in=[StepRun.PENDING, StepRun.OPENED]).update(status=StepRun.REVOKED)
    packet.status = Packet.CANCELLED
    packet.save(update_fields=["status"])
    audit(packet.workspace, "packet.cancelled", "", actor=user, packet=packet, request=request)


def mark_opened(run, *, user=None, request=None):
    from .bots import run_bots

    if run.status == StepRun.PENDING:
        run.status = StepRun.OPENED
        run.opened_at = timezone.now()
        run.save(update_fields=["status", "opened_at"])
        who = actor_label(user, run.assignee_email or run.assignee_name)
        audit(run.packet.workspace, "step.opened", f"{who} opened {run.step_name}", actor=user, packet=run.packet,
              request=request, label=who)
        ctx = bot_context(run.packet, run.step_key, "OPENED", run, user)
        run_bots(run.packet, "step_start", step_key=run.step_key, context=ctx)  # pre-fill again with the opener known
        run_bots(run.packet, "step_opened", step_key=run.step_key, context=ctx)
