"""Client portal: one page where a recipient sees every document of this workspace that is waiting for them or that
they finished, after proving they own the email address.

/portal/<workspace>/            enter an email → a sign-in link is emailed (same reply whether or not it has documents,
                                so the page can't be used to find out who works with the workspace)
/portal/<workspace>/<token>/    the list; the token is signed, names the workspace and email, and lasts 24 hours
/portal/<workspace>/<token>/<packet>.pdf   a finished document (sensitive fields masked)

Off by default: My workspace › Workspace settings › Client portal.
"""
from django.core import signing
from django.core.cache import cache
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from accounts.models import Workspace

from .. import engine, mailer
from ..models import Packet, StepRun

SALT = "paperlane.portal"
MAX_AGE = 24 * 3600
OPEN = (StepRun.PENDING, StepRun.OPENED)


def _workspace(slug):
    ws = get_object_or_404(Workspace, slug=slug)
    if not ws.setting("client_portal"):
        raise Http404("The client portal is not turned on for this workspace.")
    return ws


def make_token(ws, email):
    return signing.dumps({"w": ws.pk, "e": email.lower()}, salt=SALT, compress=True)


def _email_from(ws, token):
    try:
        data = signing.loads(token, salt=SALT, max_age=MAX_AGE)
    except signing.BadSignature:
        return None
    return data.get("e") if data.get("w") == ws.pk else None


def _runs(ws, email):
    return StepRun.objects.filter(packet__workspace=ws, assignee_email__iexact=email, packet__trashed_at__isnull=True,
                                  packet__is_test=False).select_related("packet", "packet__workflow")


def start(request, slug):
    ws = _workspace(slug)
    sent = False
    if request.method == "POST":
        email = (request.POST.get("email") or "").strip().lower()[:254]
        ip = request.META.get("REMOTE_ADDR", "")
        # at most 5 links per address and 20 per network per hour
        if "@" in email and cache.get_or_set(f"portal:e:{ws.pk}:{email}", 0, 3600) < 5 \
                and cache.get_or_set(f"portal:ip:{ip}", 0, 3600) < 20:
            for key in (f"portal:e:{ws.pk}:{email}", f"portal:ip:{ip}"):
                try:
                    cache.incr(key)
                except ValueError:
                    cache.set(key, 1, 3600)
            if _runs(ws, email).exists():
                link = engine.absolute(reverse("flows:portal_home", args=[ws.slug, make_token(ws, email)]))
                subject = f"Your documents with {ws.name}"
                message = (f"Use this link to see the documents {ws.name} has sent you. It works for 24 hours.\n\n{link}\n\n"
                           "If you didn't ask for it, you can ignore this email.")
                mailer.send(ws, email, "custom", {"subject": subject, "message": message}, subject=subject, body=message)
        sent = True
    return render(request, "flows/portal/start.html", {"workspace": ws, "sent": sent})


def home(request, slug, token):
    ws = _workspace(slug)
    email = _email_from(ws, token)
    if not email:
        return render(request, "flows/portal/start.html", {"workspace": ws, "expired": True}, status=403)
    runs = list(_runs(ws, email).order_by("-created_at")[:300])
    todo = [r for r in runs if r.status in OPEN and r.packet.status == Packet.IN_PROGRESS]
    done_packets, seen = [], set()
    for r in runs:
        if r.status == StepRun.COMPLETED and r.packet_id not in seen:
            seen.add(r.packet_id)
            done_packets.append(r)
    return render(request, "flows/portal/home.html", {
        "workspace": ws, "email": email, "token": token, "todo": todo, "done": done_packets[:100]})


def document_pdf(request, slug, token, pk):
    from ..exports import packet_pdf

    ws = _workspace(slug)
    email = _email_from(ws, token)
    if not email:
        raise Http404
    packet = get_object_or_404(Packet.objects.filter(workspace=ws, status=Packet.COMPLETED, runs__assignee_email__iexact=email,
                                                     runs__status=StepRun.COMPLETED).distinct(), pk=pk)
    engine.audit(ws, "portal.download", f"Downloaded by {email} from the client portal", packet=packet)
    resp = HttpResponse(packet_pdf(packet, mask=True), content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{packet.name[:80]}.pdf"'
    return resp
