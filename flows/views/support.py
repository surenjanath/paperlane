"""Support: diagnose a document (JSON), file a ticket from the assistant, and the workspace's ticket list/detail."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .. import diagnose as dg
from .. import engine
from ..models import Packet, SupportTicket


def _tickets(request):
    qs = SupportTicket.objects.filter(workspace=request.workspace).select_related("packet", "workflow", "created_by")
    m = request.membership
    if not (m.can_manage or m.role == "auditor"):
        qs = qs.filter(created_by=request.user) | qs.filter(packet__in=Packet.objects.visible_to(m))
    return qs.distinct()


@login_required
@require_POST
def diagnose_document(request, pk):
    packet = get_object_or_404(Packet.objects.visible_to(request.membership), pk=pk)
    return JsonResponse(dg.run(packet, request.user, request.POST.get("question", "")[:500]))


@login_required
@require_POST
def ticket_from_document(request, pk):
    """The assistant's "Create a support ticket" button (any severity)."""
    packet = get_object_or_404(Packet.objects.visible_to(request.membership), pk=pk)
    diag = dg.diagnose(packet)
    note = request.POST.get("note", "").strip()[:2000]
    t, created = dg.file_ticket(packet, diag, request.user, source="assistant", summary=request.POST.get("summary", "")[:6000],
                                note=note or "Reported from the assistant.")
    return JsonResponse({"ticket": dg.ticket_json(t, created)})


@login_required
def ticket_list(request):
    qs = _tickets(request)
    status, severity = request.GET.get("status", "active"), request.GET.get("severity", "")
    if status == "active":
        qs = qs.filter(status__in=SupportTicket.ACTIVE)
    elif status:
        qs = qs.filter(status=status)
    if severity:
        qs = qs.filter(severity=severity)
    base = _tickets(request).filter(status__in=SupportTicket.ACTIVE)
    counts = {s: base.filter(severity=s).count() for s, _ in SupportTicket.SEVERITIES}
    return render(request, "flows/tickets.html", {
        "page": Paginator(qs, 25).get_page(request.GET.get("page")), "status": status, "severity": severity,
        "counts": counts, "statuses": SupportTicket.STATUSES, "severities": SupportTicket.SEVERITIES, "nav": "tickets"})


@login_required
def ticket_detail(request, pk):
    t = get_object_or_404(_tickets(request), pk=pk)
    m = request.membership
    if request.method == "POST":
        action = request.POST.get("do")
        if action == "note":
            text = request.POST.get("text", "").strip()[:4000]
            if text:
                t.notes = t.notes + [{"by": request.user.get_full_name() or request.user.email, "at": timezone.now().isoformat(),
                                      "staff": False, "text": text}]
                t.save(update_fields=["notes", "updated_at"])
        elif action == "status":
            new = request.POST.get("status")
            if new not in dict(SupportTicket.STATUSES):
                raise PermissionDenied("Unknown status")
            if not (m.can_manage or t.created_by_id == request.user.id):
                raise PermissionDenied("Only managers or the reporter can change the status.")
            t.status = new
            t.resolved_at = timezone.now() if new in (SupportTicket.RESOLVED, SupportTicket.CLOSED) else None
            t.notes = t.notes + [{"by": request.user.get_full_name() or request.user.email, "at": timezone.now().isoformat(),
                                  "staff": False, "text": f"Status changed to {t.get_status_display()}."}]
            t.save()
            engine.audit(t.workspace, "ticket.status", f"{t.ref}: {t.get_status_display()}", actor=request.user, packet=t.packet,
                         request=request)
        elif action == "rediagnose" and t.packet_id:
            diag = dg.diagnose(t.packet)
            t.findings, t.impact = diag["findings"], diag["impact"]
            t.notes = t.notes + [{"by": "Paperlane assistant", "at": timezone.now().isoformat(), "staff": False,
                                  "text": ("Checked again: " + diag["severity_label"] + " — " + diag["title"]) if diag["findings"]
                                  else "Checked again: no problems found any more."}]
            t.save()
            messages.success(request, "The document was checked again.")
        return redirect(t.get_absolute_url())
    return render(request, "flows/ticket_detail.html", {"t": t, "nav": "tickets", "statuses": SupportTicket.STATUSES,
                                                        "can_status": m.can_manage or t.created_by_id == request.user.id})
