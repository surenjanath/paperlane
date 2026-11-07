"""Diagnose a document: find what went wrong (failed bots, stuck or expired steps, missing recipients, overdue work,
failed webhooks, unsent invitations), rate how bad it is, and measure how many other documents share the problem.

Rules decide the facts and the severity (reproducible, testable); the AI only explains them in plain words.
High and critical problems file a support ticket automatically — one per problem (fingerprint), updated when more
documents hit it."""
import datetime as dt
import json

from django.db.models import Q
from django.utils import timezone

RANK = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
LABEL = {"none": "No problems", "low": "Low", "medium": "Medium", "high": "High", "critical": "Critical"}


def _max(a, b):
    return a if RANK[a] >= RANK[b] else b


def _bump(sev, affected):
    """More documents with the same problem → more urgent."""
    if affected >= 10:
        return "critical"
    if affected >= 3:
        return _max(sev, "high")
    return sev


def diagnose(packet, now=None):
    from .models import BotJob, BotLog, OutboundEmail, Packet, StepRun, WebhookDelivery

    now = now or timezone.now()
    week = now - dt.timedelta(days=7)
    wf = packet.workflow
    runs = list(packet.runs.order_by("created_at"))
    open_runs = [r for r in runs if r.is_open]
    findings = []

    def add(code, severity, title, detail, fix, fingerprint, affected=1, link=""):
        findings.append({"code": code, "severity": severity, "title": title, "detail": detail, "fix": fix,
                         "fingerprint": fingerprint[:120], "affected": affected, "link": link})

    # 1. failed bots: a failure counts until the same bot later succeeds (a later "conditions not met" run doesn't fix it)
    failures = {}
    for log in BotLog.objects.filter(packet=packet).order_by("created_at"):
        key = log.bot_key or log.bot_name
        if log.status == "failed" or (log.status == "skipped" and log.message.startswith("Skipped after failure")):
            failures[key] = log
        elif log.status == "success":
            failures.pop(key, None)
    for key, log in failures.items():
        others = (BotLog.objects.filter(workflow=wf, bot_key=log.bot_key, created_at__gte=week)
                  .filter(Q(status="failed") | Q(message__startswith="Skipped after failure"))
                  .values("packet").distinct().count()) if log.bot_key else 1
        sev = _bump("medium", others)
        add("bot_failed", sev, f"Bot “{log.bot_name}” failed",
            f"{log.message[:400]} (at {timezone.localtime(log.created_at):%b %d %H:%M}, trigger {log.trigger or '—'})."
            + (f" The same bot failed on {others} documents of this workflow in the last 7 days." if others > 1 else ""),
            "Open the workflow's Bots, check this bot's settings (field names, URLs, connector, credentials), then re-run "
            "the step or fill the missing values by hand." + (" Because several documents are affected, fix the bot "
                                                               "before more documents start." if others > 1 else ""),
            f"bot:{wf.pk}:{log.bot_key or log.bot_name}", others, f"/workflows/{wf.pk}/bots/")

    if packet.status == Packet.IN_PROGRESS:
        pending_jobs = BotJob.objects.filter(packet=packet, done=False).exists()
        # 2. stuck: in progress but nobody can act
        if not open_runs and not pending_jobs:
            expired = [r for r in runs if r.status == StepRun.EXPIRED]
            if expired:
                r = expired[-1]
                add("expired", "high", f"The link for “{r.step_name}” expired",
                    f"{r.assignee_name or r.assignee_email or 'The recipient'} can no longer open the document "
                    f"(expired {timezone.localtime(r.expires_at):%b %d} ). Nothing else is waiting, so the document is stuck.",
                    "On the document's Status tab use Reassign or Resend to give the step a new link, or extend the "
                    "step's access expiration in the builder.", f"expired:{packet.pk}", 1, f"/documents/{packet.pk}/")
            else:
                stalled = Packet.objects.filter(workflow=wf, status=Packet.IN_PROGRESS, trashed_at__isnull=True).exclude(
                    runs__status__in=[StepRun.PENDING, StepRun.OPENED]).exclude(bot_jobs__done=False).count()
                add("stalled", _bump("high", stalled), "The document is stuck — no step is open",
                    "It is still in progress but no one has a step to work on and no bot is scheduled. This usually means "
                    "a branch or jump pointed to a step that was removed, every remaining step's conditions were false, "
                    "or a routing bot failed." + (f" {stalled} documents of this workflow are stuck the same way." if stalled > 1 else ""),
                    "Use Revise to send it back to the right step, or Cancel it. Then check the branch rules and step "
                    "conditions after the last completed step in the builder.", f"stalled:{wf.pk}" if stalled > 1 else f"stalled:{packet.pk}",
                    stalled, f"/documents/{packet.pk}/?tab=status")
        for r in open_runs:
            step = packet.step(r.step_key) or {}
            rtype = step.get("recipient_type", "")
            # 3. nobody assigned
            if not (r.assignee_email or r.assignee_user_id or r.assignee_group_id) and rtype not in ("anyone",):
                add("no_recipient", "high", f"Nobody is assigned to “{r.step_name}”",
                    "The step has no email, user or group, so no one was invited. This happens when the recipient comes "
                    "from a field (e.g. {field:Manager email}) that was empty.",
                    "Reassign the step on the Status tab, and make the source field required in the editor.",
                    f"norecipient:{wf.pk}:{r.step_key}", 1, f"/documents/{packet.pk}/")
            elif r.assignee_email and "@" not in r.assignee_email:
                add("bad_email", "high", f"“{r.assignee_email}” is not a valid email",
                    f"The invitation for “{r.step_name}” can't be delivered.",
                    "Reassign the step to a valid address and check the field the address comes from.",
                    f"bademail:{wf.pk}:{r.step_key}", 1, f"/documents/{packet.pk}/")
            elif r.assignee_email and rtype not in ("anyone", "current_user") and not OutboundEmail.objects.filter(
                    packet=packet, to__icontains=r.assignee_email, created_at__gte=r.created_at - dt.timedelta(minutes=1)).exists():
                add("no_email", "medium", f"No invitation email was sent for “{r.step_name}”",
                    f"There is no email to {r.assignee_email} in this document's email log since the step started.",
                    "Use Resend on the Status tab, and check My workspace › Email log and the step's notification settings.",
                    f"noemail:{packet.pk}:{r.step_key}", 1, f"/documents/{packet.pk}/?tab=emails")
            # 4. overdue
            if r.due_at and r.due_at < now:
                late = now - r.due_at
                add("overdue", "high" if late > dt.timedelta(days=3) else "medium", f"“{r.step_name}” is overdue",
                    f"It was due {timezone.localtime(r.due_at):%b %d %H:%M} — {late.days} day(s) {late.seconds // 3600} h ago — "
                    f"held by {r.assignee_name or r.assignee_email or (r.assignee_group.name if r.assignee_group_id else '—')}.",
                    "Remind the holder, reassign the step, or check Team workload for who has capacity.",
                    f"overdue:{packet.pk}:{r.step_key}", 1, f"/documents/{packet.pk}/")
        # 5. no activity
        if open_runs and packet.last_activity_at < now - dt.timedelta(days=7):
            add("stale", "low", "No activity for more than 7 days",
                f"The last change was {timezone.localtime(packet.last_activity_at):%b %d}.",
                "Send a reminder, or add reminders / a deadline to the step in the builder.", f"stale:{packet.pk}", 1,
                f"/documents/{packet.pk}/")

    # 6. webhooks
    failed_hooks = WebhookDelivery.objects.filter(payload__document__id=packet.pk, status=WebhookDelivery.FAILED)
    if failed_hooks.exists():
        h = failed_hooks.first()
        add("webhook_failed", "medium", f"Webhook delivery failed ({h.endpoint.name if hasattr(h.endpoint, 'name') else h.endpoint.url})",
            f"Event {h.event}: {h.error or h.response_code or 'no response'} after {h.attempts} attempt(s).",
            "Check the receiving system, then use Redeliver on My workspace › Webhooks.", f"webhook:{h.endpoint_id}", 1,
            "/workspace/webhooks/")

    # 7. declined (a business outcome, not an error)
    if packet.status == Packet.DECLINED:
        r = next((x for x in reversed(runs) if x.status == StepRun.DECLINED), None)
        add("declined", "low", f"Declined{f' at “{r.step_name}”' if r else ''}",
            f"Reason: {r.decline_reason}" if r and r.decline_reason else "No reason was given.",
            "This is a recipient's decision, not a system error. Use Revise to send it back, or start a new document.",
            f"declined:{packet.pk}", 1, f"/documents/{packet.pk}/")
    if (packet.flags or {}).get("duplicates"):
        add("duplicate", "low", "Possible duplicate", "Another document has the same key values.",
            "Compare them and cancel one if it is a repeat.", f"duplicate:{packet.pk}", 1, f"/documents/{packet.pk}/")

    findings.sort(key=lambda f: -RANK[f["severity"]])
    severity = "none"
    for f in findings:
        severity = _max(severity, f["severity"])
    top = findings[0] if findings else None
    affected = max((f["affected"] for f in findings), default=0)
    impact = ("Nothing is blocked." if not findings else
              f"{affected} documents of “{wf.name}” are affected." if affected > 1 else
              "This document is blocked." if top["code"] in ("stalled", "expired", "no_recipient", "bad_email") else
              "Only this document is affected.")
    return {"packet": packet.pk, "document": packet.name, "workflow": wf.name, "status": packet.get_status_display(),
            "severity": severity, "severity_label": LABEL[severity], "impact": impact, "affected": affected,
            "findings": findings, "fingerprint": top["fingerprint"] if top else "",
            "title": (top["title"] if top else "No problems found")}


def facts_text(packet, diag):
    """What the AI sees: the diagnosis plus a short timeline (no field values beyond what the findings mention)."""
    lines = [f"Document #{packet.pk} “{packet.name}” — workflow “{packet.workflow.name}” — status {packet.get_status_display()}",
             f"Started {timezone.localtime(packet.created_at):%b %d %H:%M}, last activity {timezone.localtime(packet.last_activity_at):%b %d %H:%M}.",
             "Steps so far:"]
    for r in packet.runs.order_by("created_at")[:30]:
        who = r.assignee_name or r.assignee_email or (r.assignee_group.name if r.assignee_group_id else "—")
        lines.append(f"- {r.step_name}: {r.status} ({who})")
    recent = packet.bot_logs.order_by("-created_at")[:12]
    if recent:
        lines.append("Recent bot results:")
        lines += [f"- {b.bot_name}: {b.status} — {b.message[:160]}" for b in recent]
    lines.append("DIAGNOSIS (rules): " + json.dumps({k: diag[k] for k in ("severity", "impact", "findings")}, default=str)[:6000])
    return "\n".join(lines)


EXPLAIN_SYSTEM = """You are Paperlane's support engineer. A user asks what went wrong with a document. You get the
document's timeline and a rule-based DIAGNOSIS. Explain in plain words:
1. What went wrong (most serious first) and the likely cause.
2. How to fix it — numbered steps with exact places in bold (e.g. **Status tab › Reassign**).
3. How to stop it happening again (one line).
Only use the facts given; if nothing is wrong, say so in one or two sentences and say what the document is waiting for.
Keep it under 160 words. Plain text with **bold** and lists only.
Never mention "the diagnosis", "rules", severity codes or "skipped (conditions not met)" bots — those are internal.
The document page has these tabs only: Status, Documents, Revisions, Comments, Ask AI, Files, Audit trail, Bots log,
Emails. Actions on the Status tab: Remind, Resend, Reassign, Copy link, Revise. Don't invent other screens."""


def explain(packet, diag, user=None, question=""):
    """AI explanation of the diagnosis; falls back to a rule-based text when no model is available."""
    from . import ai_actions

    try:
        engine = ai_actions.engine_for(packet.workspace)
        text = ai_actions.chat(packet.workspace, engine, EXPLAIN_SYSTEM,
                               f"{facts_text(packet, diag)}\n\nUser question: {question or 'What went wrong?'}",
                               json_mode=False, kind="diagnose", packet=packet, user=user)
        return text.strip(), True
    except ai_actions.AIError:
        if not diag["findings"]:
            return "I didn't find anything wrong with this document.", False
        parts = [f"**{f['title']}** — {f['detail']}\nFix: {f['fix']}" for f in diag["findings"][:4]]
        return "\n\n".join(parts), False


# ---- tickets ------------------------------------------------------------------------------------------------------
AUTO = ("high", "critical")


def file_ticket(packet, diag, user=None, source="manual", summary="", note=""):
    """Create a ticket for the diagnosis, or update the open one with the same fingerprint. Returns (ticket, created)."""
    from core.models import Notification

    from .models import SupportTicket

    sev = diag["severity"] if diag["severity"] != "none" else "low"
    fp = diag["fingerprint"] or f"manual:{packet.pk}"
    t = SupportTicket.objects.filter(workspace=packet.workspace, fingerprint=fp, status__in=SupportTicket.ACTIVE).first()
    stamp = timezone.now().isoformat()
    by = (user.get_full_name() or user.email) if user else "Paperlane assistant"
    if t is None:  # the same problem came back soon after it was resolved → reopen that ticket instead of a new one
        t = SupportTicket.objects.filter(workspace=packet.workspace, fingerprint=fp, status=SupportTicket.RESOLVED,
                                         resolved_at__gte=timezone.now() - dt.timedelta(days=7)).first()
        if t:
            t.status, t.resolved_at = SupportTicket.OPEN, None
            t.notes = t.notes + [{"by": "Paperlane assistant", "at": stamp, "staff": False,
                                  "text": f"Reopened — the problem happened again on document #{packet.pk}."}]
            t.save()
    if t:
        changed = []
        if RANK[sev] > RANK[t.severity]:
            t.severity = sev
            t.set_target()
            changed.append(f"severity raised to {LABEL[sev]}")
            if sev in AUTO:
                _alert_support(t)
        if diag["affected"] > t.affected_count:
            t.affected_count = diag["affected"]
            changed.append(f"{diag['affected']} documents affected")
        t.impact = diag["impact"]
        seen = t.packet_id == packet.pk or any(f"document #{packet.pk}." in n.get("text", "") for n in t.notes)
        if note or changed or not seen:
            t.notes = t.notes + [{"by": by, "at": stamp, "staff": False,
                                  "text": (note or "") + (f" ({', '.join(changed)})" if changed else "") + f" — seen again on document #{packet.pk}."}]
        t.save()
        return t, False
    t = SupportTicket.objects.create(
        workspace=packet.workspace, packet=packet, workflow=packet.workflow, title=f"{diag['title']} — {packet.workflow.name}"[:200],
        summary=summary, findings=diag["findings"], severity=sev, impact=diag["impact"], affected_count=max(1, diag["affected"]),
        fingerprint=fp, source=source, created_by=user if getattr(user, "pk", None) else None,
        notes=[{"by": by, "at": stamp, "staff": False, "text": note}] if note else [])
    t.set_target()
    t.save(update_fields=["due_at"])
    if sev in AUTO:
        _alert_support(t)
    from . import engine

    owners = [m.user for m in packet.workspace.memberships.filter(role__in=["owner", "supervisor"]).select_related("user")]
    for u in owners:
        Notification.objects.create(user=u, workspace=packet.workspace, kind="ticket",
                                    title=f"{t.ref} {LABEL[sev]}: {diag['title']}"[:200],
                                    body=diag["impact"], link=t.get_absolute_url())
    engine.audit(packet.workspace, "ticket.created", f"{t.ref} ({sev}, {source}): {t.title}", actor=user if getattr(user, "pk", None) else None,
                 packet=packet)
    return t, True


def _alert_support(t):
    """Email the platform's support address about a high/critical ticket (once per severity level)."""
    from superadmin.models import get_settings

    from . import mailer
    from .engine import absolute

    to = get_settings().support_email
    if not to or t.notes and any(n.get("alerted") == t.severity for n in t.notes):
        return
    mailer.send(t.workspace, to, "custom", {
        "subject": f"[{t.get_severity_display()}] {t.ref} · {t.workspace.name}: {t.title}"[:200],
        "message": f"{t.impact}\n\n{t.summary[:1500]}\n\nRespond by {timezone.localtime(t.due_at):%b %d %H:%M}.\n"
                   f"Open: {absolute('/platform/tickets/')}"}, packet=t.packet)
    t.notes = t.notes + [{"by": "Paperlane", "at": timezone.now().isoformat(), "staff": True, "alerted": t.severity,
                          "text": f"Support was alerted by email ({t.get_severity_display()})."}]
    t.save(update_fields=["notes"])


def ticket_json(t, created=None):
    return {"ref": t.ref, "url": t.get_absolute_url(), "severity": t.severity, "severity_label": t.get_severity_display(),
            "status": t.get_status_display(), "created": created, "auto": t.source == "auto"}


def run(packet, user=None, question="", auto_ticket=True):
    """Diagnose + explain + (for high/critical) file a ticket. Returns the JSON the assistant renders."""
    from .models import SupportTicket

    diag = diagnose(packet)
    text, ai = explain(packet, diag, user, question)
    ticket = None
    if auto_ticket and diag["severity"] in AUTO:
        t, created = file_ticket(packet, diag, user, source="auto", summary=text)
        ticket = ticket_json(t, created)
    elif diag["fingerprint"]:
        t = SupportTicket.objects.filter(workspace=packet.workspace, fingerprint=diag["fingerprint"],
                                         status__in=SupportTicket.ACTIVE).first()
        ticket = ticket_json(t, False) if t else None
    return {**diag, "explanation": text, "ai": ai, "ticket": ticket}
