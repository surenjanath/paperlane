"""PDF / CSV exports of document packages."""
import csv
import io

from django.template.loader import render_to_string

from .fieldtypes import COMPOSITES, LABELS, field_value_to_text, is_input


def rendered_documents(packet, data=None, mask=False):
    """Structure used by printable templates: [{name, fields:[{label, type, text, image}]}]. ``mask`` hides sensitive values."""
    from .privacy import masked_data

    data = packet.data if data is None else data
    if mask:
        data = masked_data(packet, data)
    out = []
    for doc in packet.documents:
        vals = data.get(doc["key"], {})
        rows = []
        for f in doc.get("fields", []):
            t = f.get("type")
            if t in ("divider", "page_break", "youtube", "vimeo", "html", "image"):
                continue
            v = vals.get(f["id"])
            row = {"label": f.get("label") or LABELS.get(t, t), "type": t, "text": "", "image": "", "heading": not is_input(t)}
            if t in ("signature", "initials") and isinstance(v, str) and v.startswith("data:image"):
                row["image"] = v
            elif t in COMPOSITES and isinstance(v, dict):
                row["text"] = ", ".join(f"{lbl}: {v.get(k)}" for k, lbl in COMPOSITES[t] if v.get(k))
            else:
                row["text"] = field_value_to_text(f, v)
            if t == "description":
                row["text"] = f.get("text", "")
            rows.append(row)
        out.append({"name": doc["name"], "fields": rows})
    return out


def _pdf_from_html(html):
    try:
        from xhtml2pdf import pisa
    except ImportError:  # pragma: no cover
        return None
    buf = io.BytesIO()
    result = pisa.CreatePDF(src=html, dest=buf, encoding="utf-8")
    return None if result.err else buf.getvalue()


def packet_pdf(packet, data=None, include_certificate=None, seal=None, mask=False):
    from .engine import absolute

    if include_certificate is None:
        include_certificate = packet.workspace.setting("certificate_of_completion") and packet.status == "completed"
    html = render_to_string("flows/print_packet.html", {
        "packet": packet, "documents": rendered_documents(packet, data, mask=mask), "pdf": True, "masked": mask,
        "certificate": include_certificate, "events": packet.audit_events.order_by("created_at") if include_certificate else [],
        "seal": seal, "verify_url": absolute(seal.get_verify_url()) if seal else "",
    })
    return _pdf_from_html(html)


def certificate_pdf(packet):
    html = render_to_string("flows/print_packet.html", {
        "packet": packet, "documents": [], "pdf": True, "certificate": True,
        "events": packet.audit_events.order_by("created_at"),
    })
    return _pdf_from_html(html)


def metadata_csv(packets, can_see=None):
    """``can_see(packet)`` → whether sensitive values may be written; masked otherwise."""
    from .privacy import mask as _mask
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "Name", "Workflow", "Version", "Status", "Current step", "Created", "Completed",
                "Tags", "Field", "Value"])
    for p in packets:
        base = [p.pk, p.name, p.workflow.name, p.version, p.get_status_display(),
                (p.current_step() or {}).get("name", ""), p.created_at.isoformat(timespec="minutes"),
                p.completed_at.isoformat(timespec="minutes") if p.completed_at else "",
                ", ".join(t.name for t in p.tags.all())]
        wrote = False
        for doc in p.documents:
            vals = p.data.get(doc["key"], {})
            for f in doc.get("fields", []):
                if f["id"] in vals and is_input(f.get("type")):
                    text = field_value_to_text(f, vals[f["id"]])
                    if f.get("sensitive") and not (can_see and can_see(p)):
                        text = _mask(text)
                    w.writerow(base + [f.get("name") or f.get("label"), text])
                    wrote = True
        if not wrote:
            w.writerow(base + ["", ""])
    return buf.getvalue()


def audit_csv(events):
    buf = io.StringIO()
    w = csv.writer(buf)
    from core.audit_view import category_of, label_of

    w.writerow(["When", "Actor", "Category", "Action", "Event", "Detail", "Document", "Workflow", "IP"])
    for e in events:
        w.writerow([e.created_at.isoformat(timespec="seconds"), e.actor_label, category_of(e.action), label_of(e.action), e.action,
                    e.detail, e.packet.name if e.packet_id else "", e.workflow.name if e.workflow_id else "", e.ip or ""])
    return buf.getvalue()
