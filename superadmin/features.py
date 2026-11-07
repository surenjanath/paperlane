from django.shortcuts import render

from .models import FEATURES


def feature_off(request, key, workspace=None):
    """A 403 page when a platform feature is switched off for this workspace, else None."""
    ws = workspace or getattr(request, "workspace", None)
    if ws is None or ws.feature(key):
        return None
    label = next((l for k, l, _ in FEATURES if k == key), key)
    return render(request, "superadmin/feature_off.html", {"label": label}, status=403)
