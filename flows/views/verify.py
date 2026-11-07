"""Public document verification: check a seal code, a fingerprint or an uploaded PDF."""
from django.core.cache import cache
from django.shortcuts import render
from django.views.decorators.csrf import csrf_protect

from .. import sealing
from ..engine import client_ip

LIMIT = 30  # lookups per IP per 10 minutes


def _limited(request):
    k = f"verify:{client_ip(request)}"
    n = cache.get(k, 0) + 1
    cache.set(k, n, 600)
    return n > LIMIT


@csrf_protect
def verify(request, code=""):
    ctx = {"code": code or request.GET.get("code", "").strip()}
    if request.method == "POST" or ctx["code"]:
        if _limited(request):
            ctx["error"] = "Too many checks from your network. Try again in a few minutes."
            return render(request, "flows/verify.html", ctx, status=429)
        upload = request.FILES.get("pdf") if request.method == "POST" else None
        if upload is not None:
            if upload.size > 30 * 1024 * 1024:
                ctx["error"] = "That file is too large to be a Paperlane document."
                return render(request, "flows/verify.html", ctx)
            s = sealing.find(pdf_bytes=upload.read())
            ctx["mode"] = "pdf"
        else:
            if request.method == "POST":
                ctx["code"] = request.POST.get("code", "").strip()
            s = sealing.find(ctx["code"])
            ctx["mode"] = "code"
        ctx["checked"] = True
        if s:
            ctx["seal"] = s
            ctx["integrity"] = sealing.check(s)
            ctx["latest"] = s.packet.seals.filter(superseded_at__isnull=True).first()
    return render(request, "flows/verify.html", ctx)
