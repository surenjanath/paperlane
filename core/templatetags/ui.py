import json

from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()

STATUS_STYLES = {
    "not_used": ("Not used", "gray"),
    "in_progress": ("Sent", "blue"), "completed": ("Completed", "green"), "declined": ("Declined", "red"),
    "cancelled": ("Cancelled", "gray"), "draft": ("Draft", "gray"), "pending": ("Waiting", "amber"),
    "opened": ("In use", "blue"), "skipped": ("Skipped", "gray"), "expired": ("Expired", "red"),
    "revoked": ("Revoked", "gray"), "success": ("Success", "green"), "failed": ("Failed", "red"),
    "current": ("In progress", "blue"), "done": ("Done", "green"), "upcoming": ("Upcoming", "gray"),
}


@register.filter
def get(d, key):
    if d is None:
        return ""
    try:
        return d.get(key, "")
    except AttributeError:
        try:
            return d[key]
        except (KeyError, IndexError, TypeError):
            return ""


@register.filter
def initials(user_or_name):
    name = user_or_name
    if hasattr(user_or_name, "get_full_name"):
        name = user_or_name.get_full_name() or user_or_name.email or user_or_name.username
    parts = [p for p in str(name or "?").replace(".", " ").replace("@", " ").split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


@register.filter
def display_name(user):
    if not user:
        return ""
    return user.get_full_name() or user.email or user.username


@register.simple_tag
def badge(status, label=None):
    text, color = STATUS_STYLES.get(status, (status.replace("_", " ").title() if status else "", "gray"))
    return format_html('<span class="badge badge-{}">{}</span>', color, label or text)


@register.filter
def jsonify(value):
    return mark_safe(json.dumps(value).replace("</", "<\\/"))


@register.simple_tag(takes_context=True)
def qs(context, **kwargs):
    """Return the current querystring with ``kwargs`` replaced (None/'' removes)."""
    params = context["request"].GET.copy()
    params.pop("page", None)
    for k, v in kwargs.items():
        if v in (None, ""):
            params.pop(k, None)
        else:
            params[k] = v
    encoded = params.urlencode()
    return f"?{encoded}" if encoded else "?"


@register.filter
def contains(container, item):
    try:
        return item in container
    except TypeError:
        return False


@register.filter
def to_str(v):
    return str(v)


@register.filter
def pct(a, b):
    try:
        return int(a * 100 / b) if b else 0
    except (TypeError, ValueError):
        return 0


_P = {
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    "team": '<circle cx="9" cy="8" r="3"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6"/><circle cx="17" cy="9" r="2.5"/><path d="M15.5 14.2c3 .2 5.5 2.6 5.5 5.8"/>',
    "contacts": '<rect x="4" y="3" width="16" height="18" rx="2"/><circle cx="12" cy="10" r="3"/><path d="M7.5 17c.8-2 2.5-3 4.5-3s3.7 1 4.5 3"/>',
    "help": '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6v.6"/><circle cx="12" cy="17" r=".6" fill="currentColor"/>',
    "bell": '<path d="M6 16V11a6 6 0 1 1 12 0v5l1.5 2h-15z"/><path d="M10 20a2 2 0 0 0 4 0"/>',
    "building": '<rect x="4" y="3" width="16" height="18" rx="1.5"/><path d="M9 7h1M14 7h1M9 11h1M14 11h1M9 15h1M14 15h1"/>',
    "dashboard": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/>',
    "inbox": '<path d="M4 13 6.5 5h11L20 13v6H4z"/><path d="M4 13h4.5l1 2h5l1-2H20"/>',
    "documents": '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 8h6M9 12h6M9 16h4"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "workflow": '<rect x="3" y="4" width="7" height="6" rx="1.5"/><rect x="14" y="14" width="7" height="6" rx="1.5"/><path d="M6.5 10v4a3 3 0 0 0 3 3H14"/>',
    "archive": '<rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v10h14V9M10 13h4"/>',
    "trash": '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "dots": '<circle cx="5" cy="12" r="1.6" fill="currentColor"/><circle cx="12" cy="12" r="1.6" fill="currentColor"/><circle cx="19" cy="12" r="1.6" fill="currentColor"/>',
    "bolt": '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
    "link": '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
    "gear": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    "share": '<circle cx="6" cy="12" r="2.5"/><circle cx="18" cy="6" r="2.5"/><circle cx="18" cy="18" r="2.5"/><path d="m8.2 10.8 7.6-3.6M8.2 13.2l7.6 3.6"/>',
    "configure": '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 8h8M8 12h5"/><circle cx="16" cy="16" r="2"/>',
    "edit": '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="m13.5 6.5 4 4"/>',
    "download": '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
    "upload": '<path d="M12 20V9M7 14l5-5 5 5M5 4h14"/>',
    "eye": '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
    "tag": '<path d="M3 12V4h8l10 10-8 8z"/><circle cx="7.5" cy="8.5" r="1.3" fill="currentColor"/>',
    "copy": '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/>',
    "hide": '<path d="M3 3l18 18M10.6 6.1A9.8 9.8 0 0 1 12 6c6.5 0 10 6 10 6a17 17 0 0 1-3 3.6M6.3 7.3C3.7 9 2 12 2 12s3.5 7 10 7c1.6 0 3-.4 4.3-1"/>',
    "bot": '<rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 4v4M9 13h.01M15 13h.01M9 17h6"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-7 8-7s8 3 8 7"/>',
    "send": '<path d="M21 3 10 14M21 3l-7 18-4-7-7-4z"/>',
    "check": '<path d="m5 12 4 4L19 6"/>',
    "audit": '<path d="M9 5H5v16h14V5h-4"/><rect x="9" y="3" width="6" height="4" rx="1"/><path d="M8 12h8M8 16h5"/>',
    "branch": '<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="12" r="2"/><path d="M6 7v10M6 12h10"/>',
    "step": '<circle cx="12" cy="8" r="3.5"/><path d="M5 20c0-3.5 3-6 7-6s7 2.5 7 6"/>',
    "chart": '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    "expand": '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
    "calendar": '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
    "back": '<path d="m15 18-6-6 6-6"/>',
    "menu": '<path d="M4 6h16M4 12h16M4 18h16"/>',
    "view_cards": '<rect x="4" y="4" width="16" height="7" rx="2"/><rect x="4" y="13" width="16" height="7" rx="2"/>',
    "view_rows": '<path d="M4 6h16M4 10h16M4 14h16M4 18h16"/>',
    "zoom_in": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5M11 8v6M8 11h6"/>',
    "zoom_out": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5M8 11h6"/>',
    "comment": '<path d="M4 5h16v11H9l-5 4z"/>',
    "mail": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/>',
    "restore": '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
    "play": '<path d="M7 4v16l13-8z"/>',
    "logout": '<path d="M15 4h4v16h-4M10 8l-4 4 4 4M6 12h11"/>',
    "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
    "sort": '<path d="M7 4v16M3.5 16.5 7 20l3.5-3.5M13 6h8M13 11h6M13 16h4"/>',
    "filter": '<path d="M4 5h16l-6 8v5l-4 2v-7z"/>',
    "kebab": '<circle cx="12" cy="5" r="1.7" fill="currentColor"/><circle cx="12" cy="12" r="1.7" fill="currentColor"/><circle cx="12" cy="19" r="1.7" fill="currentColor"/>',
    "palette": '<path d="M12 3a9 9 0 0 0 0 18c1 0 1.5-.8 1.5-1.5 0-.9-.7-1.2-.7-2 0-.8.7-1.5 1.5-1.5H17a4 4 0 0 0 4-4c0-5-4-9-9-9z"/><circle cx="7.5" cy="11" r="1" fill="currentColor"/><circle cx="10" cy="7" r="1" fill="currentColor"/><circle cx="15" cy="7" r="1" fill="currentColor"/>',
    "code": '<path d="m8 8-4 4 4 4M16 8l4 4-4 4M14 5l-4 14"/>',
    "key": '<circle cx="8" cy="15" r="4"/><path d="m11 12 9-9M17 6l3 3M14 9l2 2"/>',
    "shield": '<path d="M12 3 4 6v6c0 4.5 3.4 8.3 8 9 4.6-.7 8-4.5 8-9V6z"/><path d="m9 12 2 2 4-4"/>',
    "server": '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
    "toggle": '<rect x="2" y="7" width="20" height="10" rx="5"/><circle cx="16" cy="12" r="3"/>',
    "sparkle": '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/>',
}


LOGO_MARK = (
    '<svg class="brand-mark" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="{s}" height="{s}" aria-hidden="true">'
    '<rect width="64" height="64" rx="15" fill="#E4572E"/>'
    '<path d="M11 50 C 19 47, 24 43, 28.5 38" fill="none" stroke="#fff" stroke-opacity=".55" stroke-width="3.2" '
    'stroke-linecap="round" stroke-dasharray="0.1 6.4"/>'
    '<path d="M15 31.5 L50 15 L41.5 49 L31.5 39.5 Z" fill="#fff"/>'
    '<path d="M50 15 L31.5 39.5 L30 47.5 Z" fill="#F8D9CE"/>'
    '<path d="M50 15 L31.5 39.5" stroke="#E4572E" stroke-opacity=".35" stroke-width="1.2"/></svg>'
)


@register.simple_tag
def logo_mark(size=32):
    """The Paperlane mark (paper plane on its lane) as inline SVG."""
    return mark_safe(LOGO_MARK.format(s=int(size)))


@register.simple_tag
def icon(name, size=None):
    s = f' width="{size}" height="{size}"' if size else ""
    return mark_safe(f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"{s} aria-hidden="true">{_P.get(name, "")}</svg>')


@register.filter
def upto(value, delimiter=","):
    """'6 hours, 3 minutes' → '6 hours'."""
    return str(value).split(delimiter)[0].replace("\xa0", " ")


@register.simple_tag
def condition_types(wf=None):
    """Condition-type groups (recipient, date, previous step, document data, counters) for rule pickers."""
    from flows.fieldtypes import CONDITION_TYPES, condition_groups

    return condition_groups(wf.documents.all()) if wf is not None and getattr(wf, "pk", None) else CONDITION_TYPES


@register.filter
def pairs(spec):
    """"a:Label A,b:Label B" → [("a", "Label A"), ("b", "Label B")] for small inline option lists."""
    return [tuple(x.split(":", 1)) for x in str(spec).split(",") if ":" in x]


@register.filter
def split_pairs(value):
    """"a|A,b|B" → [("a", "A"), ("b", "B")] for small inline option lists."""
    return [tuple(p.split("|", 1)) for p in str(value).split(",") if "|" in p]


@register.filter
def pretty_json(value):
    import json

    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


@register.filter
def wf_visible(workflow_id, request):
    """Can the current member open this workflow's page? (one query per request)"""
    m = getattr(request, "membership", None)
    if m is None:
        return False
    if m.can_view_all:
        return True
    ids = getattr(request, "_visible_wf_ids", None)
    if ids is None:
        from flows.models import Workflow

        ids = request._visible_wf_ids = set(Workflow.objects.visible_to(m).values_list("id", flat=True))
    return workflow_id in ids


@register.filter
def can_manage_doc(packet, request):
    """Mirror of permissions.get_packet(manage=True) without extra queries (sharing is checked when acting)."""
    m = getattr(request, "membership", None)
    if m is None:
        return False
    wf = packet.workflow
    return bool(m.can_manage or packet.created_by_id == m.user_id or wf.created_by_id == m.user_id
                or (m.can_create and wf.access == wf.TEAM))


@register.filter
def clean_html(value):
    """Author HTML with scripts, event handlers and javascript: links removed."""
    from core.sanitize import clean_html as _clean

    return mark_safe(_clean(value))


@register.filter
def duration_short(td):
    """timedelta → '3 d 4 h' / '5 h 10 min' / '12 min'."""
    try:
        secs = int(td.total_seconds())
    except AttributeError:
        return ""
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d} d {h} h" if h else f"{d} d"
    if h:
        return f"{h} h {m} min" if m else f"{h} h"
    return f"{m} min" if m else "< 1 min"


@register.filter
def split(value, sep=","):
    return [x for x in str(value).split(sep)]


@register.filter
def mentions(text):
    """Escape a comment and highlight @mentions."""
    import re

    from django.utils.html import escape

    out = escape(text or "")
    out = re.sub(r'@(&quot;[^&]{2,80}&quot;|[\w.+-]+@[\w-]+(?:\.[\w-]+)+|[\w.]{2,40})', r'<span class="mention">@\1</span>', out)
    return mark_safe(out.replace("&quot;", '"'))


@register.filter
def iso_dt(value):
    """An ISO timestamp string (e.g. from a JSON note) → aware datetime for |date."""
    from django.utils.dateparse import parse_datetime

    try:
        return parse_datetime(str(value))
    except (TypeError, ValueError):
        return None


@register.filter
def duration(ms):
    """Milliseconds as people read them: 850 ms, 26.0 s, 1 min 26 s."""
    try:
        ms = float(ms)
    except (TypeError, ValueError):
        return "—"
    if ms < 1000:
        return f"{ms:.0f} ms"
    s = ms / 1000
    if s < 60:
        return f"{s:.1f} s"
    return f"{int(s // 60)} min {int(s % 60)} s"


@register.filter
def my_run(packet, request):
    """The open step of ``packet`` that the person viewing can fill now (their own, their group's, or a colleague's
    they cover), or None. Uses the prefetched runs; claims by teammates are respected."""
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return None
    for r in packet.runs.all():
        if r.is_open and r.can_act(user):
            return r
    return None
