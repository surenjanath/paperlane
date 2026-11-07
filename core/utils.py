from django.shortcuts import redirect, resolve_url
from django.utils.http import url_has_allowed_host_and_scheme


def safe_next(request, fallback, *args):
    """Redirect to ?next= / POST next only when it points at this site; otherwise to ``fallback``."""
    nxt = request.POST.get("next") or request.GET.get("next")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(nxt)
    return redirect(resolve_url(fallback, *args))


def safe_url(request, url, fallback):
    """``url`` if it is on this site, else ``fallback`` (for next= values and Referer headers)."""
    if url and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return url
    return fallback
