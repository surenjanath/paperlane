"""Sensitive fields (bank accounts, tax IDs, medical details…): never sent to an AI model, and masked for people who
don't need them — anyone other than the document's creator, the people who filled its steps, and managers."""
from django.db.models import Q


def is_sensitive(field):
    return bool(field.get("sensitive"))


def sensitive_ids(documents):
    return {f["id"] for d in documents for f in d.get("fields", []) if is_sensitive(f)}


def sensitive_names(documents):
    out = set()
    for d in documents:
        for f in d.get("fields", []):
            if is_sensitive(f):
                out |= {k for k in (f.get("name"), f.get("label"), f["id"]) if k}
    return out


def mask(value):
    if value in (None, "", [], {}):
        return value
    s = value if isinstance(value, str) else str(value)
    tail = s[-4:] if len(s) > 6 else ""
    return "•••• " + tail if tail else "••••"


def can_see(user, membership, packet):
    if not getattr(user, "is_authenticated", False):
        return False
    if membership and membership.workspace_id == packet.workspace_id and membership.can_manage:
        return True
    if packet.created_by_id == user.id:
        return True
    return packet.runs.filter(Q(assignee_user=user) | Q(assignee_email__iexact=user.email) | Q(claimed_by=user)).exists()


def masked_data(packet, data=None):
    """A copy of packet.data with sensitive values masked."""
    data = packet.data if data is None else data
    ids = sensitive_ids(packet.documents)
    if not ids:
        return data
    return {dk: {fid: (mask(v) if fid in ids else v) for fid, v in vals.items()} for dk, vals in (data or {}).items()}
