"""Start forms: in-form bots (lookups) and turning a submission into a running workflow."""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from django.core.files.base import ContentFile
from django.utils import timezone

from .conditions import compute_formula

LOOKUP_SOURCES = [("sheet", "Paperlane spreadsheet"), ("documents", "Earlier documents of a workflow"),
                  ("webhook", "External API / webhook"), ("connector", "A custom connector (any API)")]


# ---- lookups (in-form bots) -----------------------------------------------------------------
def _norm(v):
    return re.sub(r"\s+", " ", str(v if v is not None else "")).strip().lower()


def _apply_map(lookup, source_values):
    """{form field id: value} for the lookup's mapping, reading ``source_values`` by key (case-insensitive)."""
    lowered = {_norm(k): v for k, v in (source_values or {}).items()}
    out = {}
    for m in lookup.get("map") or []:
        to, frm = m.get("to"), m.get("from")
        if not to or not frm:
            continue
        if frm in (source_values or {}):
            out[to] = source_values[frm]
        elif _norm(frm) in lowered:
            out[to] = lowered[_norm(frm)]
    return out


def run_lookup(form, lookup, value, all_values=None):
    """Execute one in-form bot. Returns {"found": bool, "values": {field_id: value}, "message": str}."""
    value = str(value or "").strip()
    if len(value) < int(lookup.get("min_length") or 1):
        return {"found": False, "values": {}, "message": ""}
    source = lookup.get("source")
    found = None
    try:
        if source == "sheet":
            from .models import DataSheet

            sheet = DataSheet.objects.filter(workspace=form.workspace, name=lookup.get("sheet", "")).first()
            col = lookup.get("key_column", "")
            if sheet and col:
                for row in sheet.rows.order_by("-id"):
                    if _norm(row.values.get(col)) == _norm(value):
                        found = row.values
                        break
        elif source == "documents":
            from .models import Packet

            key = lookup.get("key_field", "")
            qs = Packet.objects.filter(workspace=form.workspace, workflow_id=lookup.get("workflow_id") or 0,
                                       trashed_at__isnull=True).order_by("-created_at")[:500]
            for p in qs:
                vals = p.flat_values()
                if _norm(vals.get(key)) == _norm(value):
                    found = {k: v for k, v in vals.items() if not isinstance(v, (dict, list))}
                    break
        elif source == "webhook":
            url = (lookup.get("url") or "").replace("{value}", urllib.parse.quote(value))
            if url.startswith(("http://", "https://")):
                from superadmin.netguard import check_url

                check_url(url)
                body = None
                headers = {"Accept": "application/json", "User-Agent": "Paperlane-form-lookup"}
                if (lookup.get("method") or "GET").upper() == "POST":
                    body = json.dumps({"value": value, "field": lookup.get("trigger"), "form": form.name,
                                       "values": all_values or {}}).encode()
                    headers["Content-Type"] = "application/json"
                if lookup.get("auth_header"):
                    headers["Authorization"] = lookup["auth_header"]
                req = urllib.request.Request(url, data=body, headers=headers, method=(lookup.get("method") or "GET").upper())
                with urllib.request.urlopen(req, timeout=6) as resp:  # noqa: S310 — admin-configured URL
                    payload = json.loads(resp.read(512_000) or b"{}")
                if isinstance(payload, list):
                    payload = payload[0] if payload else {}
                if isinstance(payload, dict) and payload:
                    found = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        elif source == "connector":
            from . import connectors
            from .models import Connector

            conn = Connector.objects.filter(workspace=form.workspace, pk=lookup.get("connector") or 0, active=True).first()
            if conn and form.workspace.feature("connectors"):
                res = connectors.run(conn, lookup.get("action", ""), flat_by_name(form, all_values or {}), value=value)
                data = res["data"]
                if isinstance(data, list):
                    data = data[0] if data else None
                if res["ok"] and (isinstance(data, dict) and data or res["mapped"]):
                    found = {**_flatten(data if isinstance(data, dict) else {}), **res["mapped"]}
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return {"found": False, "values": {}, "message": f"Lookup unavailable ({type(exc).__name__})."}
    if not found:
        return {"found": False, "values": {}, "message": lookup.get("not_found_message") or "No match found."}
    return {"found": True, "values": _apply_map(lookup, found), "message": lookup.get("found_message") or "Details filled in."}


def _flatten(obj, prefix="", depth=0):
    """{"a": {"b": 1}} → {"a": {...}, "a.b": 1} so lookup maps can name nested JSON keys."""
    out = {}
    for k, v in (obj or {}).items():
        key = f"{prefix}{k}"
        out[key] = v
        if isinstance(v, dict) and depth < 3:
            out.update(_flatten(v, key + ".", depth + 1))
    return out


def lookup_targets(form):
    return {m.get("to") for lk in form.lookups or [] for m in lk.get("map") or [] if m.get("to")}


def rerun_lookups(form, values):
    """Server-side: re-run every lookup whose trigger has a value and overwrite its target fields (read-only
    targets cannot be tampered with in the browser)."""
    for lk in form.lookups or []:
        trig = lk.get("trigger")
        if trig and values.get(trig) not in (None, ""):
            res = run_lookup(form, lk, values[trig], values)
            if res["found"]:
                values.update(res["values"])
    return values


# ---- submissions -----------------------------------------------------------------------------
def editable_ids(form):
    return {f["id"] for f in form.input_fields()}


def flat_by_name(form, values):
    """{name/label/id: value} for formulas, conditions and workflow pre-fill."""
    out = {}
    for f in form.fields:
        if f["id"] in values:
            v = values[f["id"]]
            out[f["id"]] = v
            for k in (f.get("name"), f.get("label")):
                if k:
                    out.setdefault(k, v)
    return out


def compute_formulas(form, values):
    for f in form.fields:
        if f.get("type") == "formula":
            values[f["id"]] = compute_formula(f.get("formula", ""), flat_by_name(form, values))
    return values


def _render(pattern, flat):
    return re.sub(r"\{field:([^}]+)\}", lambda m: str(flat.get(m.group(1).strip(), "")), pattern or "").strip()


def submit(form, values, files_by_field, *, user=None, ip=None, start=True):
    """Store a submission and start the linked workflow. ``files_by_field`` = {field id: [FormFile]}.
    With start=False (a payment is due first) the workflow is started later by ``start_workflow(sub)``.
    Returns the FormSubmission."""
    from .models import DataSheet, DataSheetRow, FormSubmission

    opts = form.options or {}
    flat = flat_by_name(form, values)
    record = {k: v for k, v in flat.items() if k != "" and not isinstance(v, (bytes,))}
    sub = FormSubmission.objects.create(form=form, data={"values": values, "by_name": record}, ip=ip,
                                        user=user if getattr(user, "is_authenticated", False) else None)
    for ff_list in files_by_field.values():
        for ff in ff_list:
            ff.submission = sub
            ff.save(update_fields=["submission"])

    if opts.get("sheet"):
        sheet, _ = DataSheet.objects.get_or_create(workspace=form.workspace, name=opts["sheet"][:160])
        row = {(f.get("name") or f.get("label") or f["id"]): _cell(values.get(f["id"]))
               for f in form.input_fields() if f.get("type") not in ("signature", "initials", "attachment")}
        row["Submitted at"] = timezone.localtime().strftime("%Y-%m-%d %H:%M")
        cols = list(sheet.columns or [])
        for k in row:
            if k not in cols:
                cols.append(k)
        sheet.columns = cols
        sheet.save(update_fields=["columns"])
        DataSheetRow.objects.create(sheet=sheet, values=row)

    if not start:
        sub.status = FormSubmission.AWAITING_PAYMENT
        sub.save(update_fields=["status"])
        return sub
    return start_workflow(sub, files_by_field)


def start_workflow(sub, files_by_field=None):
    """Start the form's workflow for a stored submission (idempotent)."""
    from . import engine
    from .models import FormFile, FormSubmission

    if sub.packet_id:
        return sub
    form = sub.form
    opts = form.options or {}
    values = (sub.data or {}).get("values") or {}
    flat = flat_by_name(form, values)
    record = (sub.data or {}).get("by_name") or {}
    if files_by_field is None:
        files_by_field = {}
        for ff in FormFile.objects.filter(submission=sub):
            files_by_field.setdefault(ff.field_id, []).append(ff)
    wf = form.workflow
    if wf is None or not wf.is_published or wf.trashed_at or wf.archived_at:
        sub.status = FormSubmission.NO_WORKFLOW
        sub.save(update_fields=["status"])
        _notify(form, sub, None)
        return sub
    try:
        prefill = {k: v for k, v in record.items() if v not in (None, "") and not _is_file(v)}
        for target, src in (opts.get("mapping") or {}).items():  # explicit mapping wins
            if src == "@today":
                prefill[target] = timezone.localdate().isoformat()
            elif isinstance(src, str) and src.startswith("="):  # composed value, e.g. "={field:First} {field:Last}"
                composed = _render(src[1:], flat)
                if composed:
                    prefill[target] = composed
            elif src in values and values[src] not in (None, ""):
                prefill[target] = values[src]
        assignee = None
        em = values.get(opts.get("recipient_email_field") or "")
        if em and "@" in str(em):
            from django.contrib.auth import get_user_model

            name = values.get(opts.get("recipient_name_field") or "") or em
            if isinstance(name, dict):
                name = " ".join(str(x) for x in name.values() if x)
            assignee = {"email": str(em).strip(), "name": str(name), "user": get_user_model().objects.filter(email__iexact=str(em).strip()).first()}
        packet = engine.start_packet(wf, user=form.created_by or wf.created_by, source="form", prefill=prefill,
                                     first_assignee=assignee, name=_render(opts.get("document_name"), flat) or None)
        _attach_files(form, packet, files_by_field)
        engine.audit(packet.workspace, "packet.form", f"Started from form “{form.name}”", packet=packet)
        if opts.get("complete_first_step"):
            run = packet.runs.filter(status__in=["pending", "opened"]).order_by("id").first()
            if run:
                engine.complete_run(run, {}, user=None, label=f"{assignee['name'] if assignee else 'Form'} (via form “{form.name}”)")
        elif assignee:
            from .views.library import _send_invites

            _send_invites(packet)
        sub.packet, sub.status = packet, FormSubmission.STARTED
        sub.save(update_fields=["packet", "status"])
    except Exception as exc:  # keep the submission even if the workflow could not start
        sub.status, sub.error = FormSubmission.FAILED, f"{type(exc).__name__}: {exc}"
        sub.save(update_fields=["status", "error"])
    _notify(form, sub, sub.packet)
    return sub


def _cell(v):
    if isinstance(v, dict):
        return ", ".join(str(x) for x in v.values() if x)
    if isinstance(v, list):
        return ", ".join(str(x.get("name", x)) if isinstance(x, dict) else str(x) for x in v)
    return v


def _is_file(v):
    return isinstance(v, list) and v and isinstance(v[0], dict) and "form_file" in v[0]


def _attach_files(form, packet, files_by_field):
    """Copy uploaded form files onto workflow attachment fields with the same name/label."""
    from .models import PacketFile

    by_name = {}
    for f in form.fields:
        if f["id"] in files_by_field:
            for k in (f.get("name"), f.get("label"), f["id"]):
                if k:
                    by_name.setdefault(k.strip().lower(), files_by_field[f["id"]])
    data = packet.data or {}
    changed = False
    for doc in packet.documents:
        for wfld in doc.get("fields", []):
            if wfld.get("type") != "attachment":
                continue
            src = next((by_name[k] for k in (str(wfld.get("name", "")).lower(), str(wfld.get("label", "")).lower(), wfld["id"])
                        if k and k in by_name), None)
            if not src:
                continue
            stored = []
            for ff in src:
                ff.file.open("rb")
                pf = PacketFile.objects.create(packet=packet, doc_key=doc["key"], field_id=wfld["id"],
                                               file=ContentFile(ff.file.read(), name=ff.original_name), original_name=ff.original_name)
                ff.file.close()
                stored.append({"name": pf.original_name, "file_id": pf.pk})
            data.setdefault(doc["key"], {})[wfld["id"]] = stored
            changed = True
    if changed:
        packet.data = data
        packet.save(update_fields=["data"])


def _notify(form, sub, packet):
    from . import mailer

    emails = (form.options or {}).get("notify_emails")
    if emails:
        mailer.send(form.workspace, emails, "step_completed", {
            "user_name": "there", "actor_name": "A form submission", "step_name": form.name,
            "document_name": packet.name if packet else form.name,
            "link": "", "fields": (sub.data or {}).get("by_name", {})}, packet=packet)


def match_report(form):
    """For the settings page: each workflow field and the form field that fills it (auto by name or mapped)."""
    wf = form.workflow
    if not wf:
        return []
    names = {}
    for f in form.input_fields():
        for k in (f.get("name"), f.get("label"), f["id"]):
            if k:
                names.setdefault(k.strip().lower(), f)
    mapping = (form.options or {}).get("mapping") or {}
    by_id = {f["id"]: f for f in form.input_fields()}
    rows = []
    for doc in wf.documents.all():
        for wfld in doc.input_fields():
            key = wfld.get("name") or wfld.get("label") or wfld["id"]
            explicit = mapping.get(key)
            auto = next((names[k] for k in (str(wfld.get("name", "")).lower(), str(wfld.get("label", "")).lower(), wfld["id"])
                         if k and k in names), None)
            rows.append({"doc": doc.name, "key": key, "label": wfld.get("label") or key, "type": wfld.get("type"),
                         "explicit": explicit, "auto": auto, "source": by_id.get(explicit) if explicit else auto})
    return rows
