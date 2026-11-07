"""Tamper-evident sealing of completed documents.

content_hash = SHA-256 over a canonical JSON of what was agreed: every field value (signature images included),
the SHA-256 of every attached file, and the signing timeline (who completed which step, when, from where).
It is printed on the certificate with a verify code; anyone can check a code or upload the PDF at /verify/.
Re-hashing the current record later shows whether the stored data changed after sealing."""
import hashlib
import json

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _file_hash(f):
    try:
        f.file.open("rb")
        h = hashlib.sha256()
        for chunk in f.file.chunks():
            h.update(chunk)
        f.file.close()
        return h.hexdigest()
    except (OSError, ValueError):
        return "missing"


def signing_timeline(packet):
    from .models import StepRun

    out = []
    for r in packet.runs.filter(status=StepRun.COMPLETED).order_by("completed_at", "id"):
        ev = packet.audit_events.filter(action="step.completed", detail__icontains=r.step_name).order_by("created_at").first()
        out.append({"step": r.step_name, "name": r.assignee_name, "email": r.assignee_email,
                    "at": r.completed_at.isoformat() if r.completed_at else "", "ip": (ev.ip if ev else "") or ""})
    return out


def canonical(packet):
    files = sorted(({"doc": f.doc_key, "field": f.field_id, "name": f.original_name, "sha256": _file_hash(f)}
                    for f in packet.files.all()), key=lambda x: (x["doc"], x["field"], x["name"]))
    body = {"document": packet.pk, "workspace": packet.workspace_id, "workflow": packet.workflow_id, "version": packet.version,
            "name": packet.name, "data": packet.data or {}, "files": files, "timeline": signing_timeline(packet),
            "completed_at": packet.completed_at.isoformat() if packet.completed_at else ""}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":")).encode()


def content_hash(packet):
    return _sha(canonical(packet))


def _code(h):
    raw = h[:16].upper()
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def seal(packet):
    """Seal a completed document (idempotent for unchanged content). Returns the DocumentSeal."""
    from . import exports
    from .models import DocumentSeal

    h = content_hash(packet)
    current = packet.seals.filter(superseded_at__isnull=True).first()
    if current and current.content_hash == h:
        return current
    with transaction.atomic():
        number = (packet.seals.order_by("-number").values_list("number", flat=True).first() or 0) + 1
        code = _code(h)
        if DocumentSeal.objects.filter(code=code).exists():  # practically impossible; keep codes unique anyway
            code = _code(_sha((h + str(number)).encode()))
        packet.seals.filter(superseded_at__isnull=True).update(superseded_at=timezone.now())
        s = DocumentSeal.objects.create(packet=packet, number=number, content_hash=h, code=code,
                                        signers=signing_timeline(packet))
        pdf = exports.packet_pdf(packet, include_certificate=True, seal=s)
        if pdf:
            s.pdf.save(f"{packet.pk}-{number}-{code}.pdf", ContentFile(pdf), save=False)
            s.pdf_hash = _sha(pdf)
            s.save(update_fields=["pdf", "pdf_hash"])
    return s


def check(s):
    """Re-hash the stored record: {"intact": bool, "current": hash} — False means data changed after sealing."""
    from .models import Packet

    now = content_hash(Packet.objects.get(pk=s.packet_id))  # always the stored record, never a cached copy
    return {"intact": now == s.content_hash, "current": now}


def find(code_or_hash="", pdf_bytes=None):
    from .models import DocumentSeal

    if pdf_bytes is not None:
        return DocumentSeal.objects.filter(pdf_hash=_sha(pdf_bytes)).select_related("packet", "packet__workspace").first()
    q = (code_or_hash or "").strip().upper().replace(" ", "")
    if len(q) == 64:
        return DocumentSeal.objects.filter(content_hash=q.lower()).select_related("packet", "packet__workspace").first()
    if len(q.replace("-", "")) == 16:
        q = "-".join(q.replace("-", "")[i:i + 4] for i in range(0, 16, 4))
        return DocumentSeal.objects.filter(code=q).select_related("packet", "packet__workspace").first()
    return None
