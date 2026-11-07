"""AI document builder for the editor: design a whole form from a description or from an existing paper/PDF/Word form,
edit it by instruction, and review it. The model answers in a compact field language; everything is normalized here
into the editor's own field format (ids, names, roles, conditions by id, formulas by {Field name}) and checked, so a
small local model can't produce something the editor or the fill page can't handle."""
import io
import re
import secrets
import zipfile

from . import ai_actions
from .fieldtypes import LABELS

TYPES = sorted(LABELS)
TYPE_SYNONYMS = {"string": "text", "input": "text", "short_text": "text", "long_text": "textarea", "paragraph": "textarea",
                 "multiline": "textarea", "select": "dropdown", "choice": "radio", "single_choice": "radio",
                 "multi_choice": "checkbox", "multiple_choice": "checkbox", "checkboxes": "checkbox", "yes_no": "radio",
                 "boolean": "radio", "money": "currency", "amount": "currency", "integer": "number", "decimal": "number",
                 "tel": "phone", "telephone": "phone", "mobile": "phone", "upload": "attachment", "file": "attachment",
                 "section": "heading", "title": "heading", "header": "heading", "text_block": "description", "note": "description",
                 "calc": "formula", "calculation": "formula", "name": "full_name", "agreement": "clickwrap", "consent": "clickwrap",
                 "datetime": "date", "dob": "date", "grid": "table"}
OPS = {"eq", "neq", "contains", "not_contains", "empty", "not_empty", "gt", "lt", "gte", "lte", "checked", "not_checked"}
DISPLAY = {"heading", "description", "divider", "page_break", "image", "youtube", "vimeo", "html"}
CHOICE = {"radio", "dropdown", "checkbox", "image_choice"}

FORMAT = """Reply with ONE JSON object only:
{"title": "<document name>", "fields": [<field>, ...], "notes": ["<short note about choices you made>"]}
A <field> is: {"key": "snake_case_unique", "type": "<type>", "label": "...", "required": true|false,
  "options": ["..."] (radio/dropdown/checkbox only), "placeholder": "...", "help": "...", "width": "half"|"full",
  "role": "<who fills it: one of the ROLES, or 'anyone'>", "show_if": {"field": "<key of an earlier field>", "op": "eq|neq|contains|not_empty|gt|lt", "value": "..."},
  "sensitive": true (bank/tax/ID/medical data — masked and never sent to AI),
  "formula": "{Label A} + {Label B}" (formula type only, refer to other fields by label), "text": "..." (heading subtitle,
  description text or clickwrap agreement), "columns": ["..."] (table only), "min": n, "max": n, "max_length": n}
Types: """ + ", ".join(TYPES) + """.
Design rules:
- Group with "heading" fields (one per section, with a short subtitle in "text"). Put short related fields side by side with "width": "half".
- Use the most specific type: email, phone, date, currency, number, address, full_name, signature, initials, attachment, clickwrap.
- Mark only truly necessary fields required. Choices get realistic options. Yes/No questions are radio ["Yes", "No"].
- Use show_if for follow-up questions (e.g. bank details only when payment method is "Direct deposit").
- End forms that need agreement with a clickwrap declaration and a signature + date.
- Never invent personal data or prefilled values."""


IDENTIFIER = re.compile(r"\b(account|acct|routing|sort code|iban|swift|policy|reference|ref|id|identifier|code|zip|postal|"
                        r"phone|mobile|tax|trn|ssn|nis|passport|licen[cs]e)\b", re.I)


SENSITIVE = re.compile(r"\b(account|acct|routing|sort code|iban|swift|bic|tax|trn|ssn|social security|nis|national insurance|"
                       r"passport|driver'?s licen[cs]e|medical|diagnosis|health|salary|income|credit card|card number|cvv)\b", re.I)


def tidy_title(t):
    t = re.sub(r"[_]+", " ", str(t or "")).strip()
    return (t[:1].upper() + t[1:])[:200] if t else ""


class FormAIError(Exception):
    pass


def _uid():
    return secrets.token_hex(5)


def _chat_json(ws, system, prompt, user=None, kind="form_ai", images=None):
    """JSON from the model, with one self-repair retry."""
    engine = ai_actions.engine_for(ws)
    try:
        if images:
            return ai_actions.chat_json(ws, vision_engine(ws) or engine, system, [{"role": "user", "content": prompt, "images": images}],
                                        kind=kind, user=user)
        return ai_actions.chat_json(ws, engine, system, prompt, kind=kind, user=user)
    except ai_actions.AIError as exc:
        raise FormAIError(str(exc))
    except ValueError as exc:
        raise FormAIError(f"The model's answer couldn't be read ({exc}). Try again, or pick a larger model.")


EXAMPLE = """EXAMPLE for "Change of address request with proof":
{"title": "Change of address", "fields": [
 {"key": "you", "type": "heading", "label": "Your details", "text": "So we can find your policy"},
 {"key": "name", "type": "full_name", "label": "Full name", "required": true},
 {"key": "policy", "type": "text", "label": "Policy number", "required": true, "width": "half"},
 {"key": "phone", "type": "phone", "label": "Phone", "width": "half"},
 {"key": "new", "type": "heading", "label": "New address"},
 {"key": "addr", "type": "address", "label": "New address", "required": true},
 {"key": "from", "type": "date", "label": "Effective from", "required": true, "width": "half"},
 {"key": "mail", "type": "radio", "label": "Also update mailing address?", "options": ["Yes", "No"], "required": true, "width": "half"},
 {"key": "proof", "type": "attachment", "label": "Proof of address (utility bill or bank statement)", "required": true},
 {"key": "decl", "type": "clickwrap", "label": "Declaration", "text": "I confirm this information is correct."},
 {"key": "sig", "type": "signature", "label": "Signature", "required": true, "width": "half"},
 {"key": "date", "type": "date", "label": "Date signed", "required": true, "width": "half"}],
 "notes": ["Grouped into two sections; proof of address is required."]}"""


def vision_engine(ws):
    """A model that can read images (Ollama, Anthropic, NVIDIA, OpenAI, or a custom endpoint), if one is available."""
    from . import ai_providers

    with ai_actions._Host(ws):
        engines = ai_providers.engines()
    vision = [e["id"] for e in engines if e.get("vision") or re.search(
        r"vl|vision|llava|minicpm-v|moondream|gpt-4o|gpt-4\.1|claude", e["id"], re.I)]
    cfg = (ws.settings or {}).get("ai", {})
    for pref in (cfg.get("vision_engine"), cfg.get("engine")):  # an explicit choice, then the default model if it can see
        if pref in vision:
            return pref
    return vision[0] if vision else None


def _role_map(roles):
    out = {"anyone": "anyone"}
    for r in roles or []:
        out[str(r["name"]).strip().lower()] = r["key"]
        out[str(r["key"]).lower()] = r["key"]
    return out


def normalize(raw_fields, roles, default_role="anyone", existing=()):
    """Model fields → editor fields. Returns (fields, notes)."""
    notes, out, by_key = [], [], {}
    rmap = _role_map(roles)
    taken = {f.get("name") for f in existing if f.get("name")}
    for raw in raw_fields or []:
        if not isinstance(raw, dict):
            continue
        t = str(raw.get("type") or "text").strip().lower().replace(" ", "_").replace("-", "_")
        t = TYPE_SYNONYMS.get(t, t)
        if t not in LABELS:
            notes.append(f"“{raw.get('label')}”: unknown type {t}, used single-line text.")
            t = "text"
        label = str(raw.get("label") or raw.get("name") or "").strip()[:200]
        if not label and t not in ("divider", "page_break"):
            notes.append(f"Skipped an untitled {LABELS[t].lower()} field.")
            continue
        if t == "number" and IDENTIFIER.search(label):  # account / routing / policy numbers keep leading zeros
            t = "text"
        f = {"id": _uid(), "type": t, "label": label, "name": "", "required": bool(raw.get("required")) and t not in DISPLAY}
        if t not in DISPLAY:
            base, n = re.sub(r"\s+", " ", label).strip()[:60] or t, 1
            name = base
            while name in taken:
                n += 1
                name = f"{base} {n}"
            taken.add(name)
            f["name"] = name
        role = rmap.get(str(raw.get("role") or "").strip().lower())
        f["role"] = default_role if role in (None, "anyone") and roles else (role or default_role)
        for k in ("placeholder", "help", "text"):
            if raw.get(k):
                f[k] = str(raw[k])[:600]
        if t == "heading" and not f.get("text"):
            f["text"] = ""
        if raw.get("width") == "half" and t not in DISPLAY | {"textarea", "table", "matrix", "repeatable", "group", "signature"}:
            f["width"] = "half"
        if t in CHOICE:
            opts = [str(o)[:120] for o in (raw.get("options") or []) if str(o).strip()]
            f["options"] = opts or ["Yes", "No"]
        if t == "table":
            f["columns"] = [str(c)[:80] for c in (raw.get("columns") or ["Item", "Amount"])][:8]
            f["min_rows"] = 3
        if t == "matrix":
            f["rows"] = [str(c)[:80] for c in (raw.get("rows") or ["Row 1"])][:12]
            f["columns"] = [str(c)[:40] for c in (raw.get("columns") or ["Poor", "Fair", "Good"])][:8]
        if t in ("group", "repeatable"):
            f["subfields"] = [{"id": f"s{i + 1}", "type": "text", "label": str(c)[:80]} for i, c in enumerate(raw.get("columns") or ["Name", "Details"])][:8]
        if t == "formula":
            f["formula"] = str(raw.get("formula") or "")[:300]
        if t == "clickwrap":
            f["text"] = f.get("text") or "I confirm the information I have given is true and complete."
            f["required"] = True
        if t in ("signature", "initials"):
            f["required"] = raw.get("required", True) is not False
        if t == "rating":
            f["max"] = 5
        for k in ("min", "max", "max_length"):
            if isinstance(raw.get(k), (int, float)) and t in ("number", "currency", "text", "textarea", "rating"):
                f[k] = raw[k]
        if t == "email":
            f.setdefault("placeholder", "name@example.com")
        if raw.get("sensitive") or (t not in DISPLAY and SENSITIVE.search(label)):
            f["sensitive"] = True
        key = str(raw.get("key") or label).strip().lower()
        by_key[key] = f
        by_key.setdefault(label.lower(), f)
        f["_show_if"] = raw.get("show_if")
        out.append(f)
    # conditions refer to field ids; formulas to field names
    names = {f["label"].lower(): f["name"] for f in out if f["name"]}
    for f in out:
        si = f.pop("_show_if", None)
        if isinstance(si, dict) and si.get("field"):
            target = by_key.get(str(si["field"]).strip().lower())
            op = si.get("op") if si.get("op") in OPS else "eq"
            if target and target is not f:
                f["condition"] = {"logic": "and", "rules": [{"field": target["id"], "op": op, "value": str(si.get("value", ""))[:120]}]}
                f["required"] = f["required"] and op not in ("empty",)
            else:
                notes.append(f"“{f['label']}”: dropped a condition on an unknown field.")
        if f["type"] == "formula" and f.get("formula"):
            def fix(m):
                ref = m.group(1).strip().lower()
                return "{" + names.get(ref, by_key.get(ref, {}).get("name") or m.group(1).strip()) + "}"
            f["formula"] = re.sub(r"\{([^}]+)\}", fix, f["formula"])
    return out, notes


def outline(fields, roles):
    rn = {r["key"]: r["name"] for r in roles or []}
    lines = []
    for f in fields:
        bits = [f.get("type", "")]
        if f.get("required"):
            bits.append("required")
        if f.get("options"):
            bits.append("options: " + " / ".join(map(str, f["options"][:8])))
        if f.get("role") and f["role"] != "anyone":
            bits.append("filled by " + rn.get(f["role"], f["role"]) if isinstance(f["role"], str) else "")
        if f.get("condition"):
            bits.append("conditional")
        lines.append(f"- “{f.get('label') or f.get('name')}” ({', '.join(b for b in bits if b)})")
    return "\n".join(lines) or "(empty form)"


def _roles_text(roles):
    return ", ".join(["anyone"] + [r["name"] for r in roles or []])


def generate(ws, description, roles, default_role="anyone", user=None, source_text="", images=None):
    """A whole form from a description (and optionally the text or image of an existing form)."""
    system = "You are an expert designer of business forms (insurance, HR, finance, legal). " + FORMAT
    prompt = f"ROLES (who can fill fields): {_roles_text(roles)}\n\n"
    if source_text:
        prompt += ("Recreate this existing form as a digital form. Keep every question and its wording, choose proper types, "
                   f"and group it into sections.\n\nEXISTING FORM TEXT:\n\"\"\"\n{source_text[:12000]}\n\"\"\"\n\n")
    if images:
        prompt += "Recreate the form in the attached image as a digital form, keeping every question and section.\n\n"
    prompt += f"REQUEST: {description or 'Create a well-structured form.'}"
    data = _chat_json(ws, system + "\n\n" + EXAMPLE, prompt, user=user, kind="form_build", images=images)
    fields, notes = normalize(data.get("fields"), roles, default_role)
    if not fields:
        raise FormAIError("The model didn't return any fields — try describing the document in more detail.")
    return {"title": tidy_title(data.get("title")), "fields": fields,
            "notes": [str(n)[:200] for n in (data.get("notes") or [])][:5] + notes}


EDIT_SYSTEM = """You edit an existing business form. Reply with ONE JSON object only:
{"edits": [<edit>, ...], "summary": "<one sentence>"}
Edits (refer to fields by their current LABEL exactly as listed):
- {"op": "add", "after": "<label of the field to insert after, 'TOP', or 'END'>", "fields": [<field>, ...]}
- {"op": "update", "field": "<label>", "set": {"label"|"required"|"options"|"placeholder"|"help"|"width"|"type"|"text"|"role"|"show_if": ...}}
- {"op": "remove", "field": "<label>"}
- {"op": "move", "field": "<label>", "after": "<label or null>"}
Only change what the request asks for. """ + FORMAT.split("Design rules:")[0].split("Reply with ONE JSON object only:")[1]


def _find(fields, ref):
    r = str(ref or "").strip().lower()
    return next((i for i, f in enumerate(fields) if r in (str(f.get("label", "")).lower(), str(f.get("name", "")).lower(), f.get("id"))), None)


def edit(ws, fields, instruction, roles, default_role="anyone", user=None):
    """Apply an instruction to the current fields. Returns {fields, changes, notes}."""
    prompt = f"ROLES: {_roles_text(roles)}\n\nCURRENT FORM:\n{outline(fields, roles)}\n\nREQUEST: {instruction[:1500]}"
    data = _chat_json(ws, EDIT_SYSTEM, prompt, user=user, kind="form_edit")
    return apply_edits(fields, data.get("edits") or [], roles, default_role) | {"summary": str(data.get("summary") or "")[:300]}


def apply_edits(fields, edits, roles, default_role="anyone"):
    out = [dict(f) for f in fields]
    changes, notes = [], []
    rmap = _role_map(roles)
    for e in edits[:40]:
        if not isinstance(e, dict):
            continue
        op = e.get("op")
        if op == "add":
            new, n = normalize(e.get("fields") or ([e["field"]] if isinstance(e.get("field"), dict) else []), roles, default_role, out)
            notes += n
            after = str(e.get("after") or "").strip()
            if not after or after.upper() == "END":
                idx = len(out)
            elif after.upper() in ("TOP", "START"):
                idx = 0
            else:
                j = _find(out, after)
                idx = len(out) if j is None else j + 1
            if not new:
                notes.append("An addition had no usable fields and was skipped.")
                continue
            out[idx:idx] = new
            changes.append("Added " + ", ".join(f"“{f['label']}”" for f in new))
        elif op in ("update", "remove", "move"):
            i = _find(out, e.get("field"))
            if i is None:
                notes.append(f"No field “{e.get('field')}”.")
                continue
            f = out[i]
            if op == "remove":
                out.pop(i)
                changes.append(f"Removed “{f.get('label')}”")
            elif op == "move":
                out.pop(i)
                j = _find(out, e.get("after")) if e.get("after") else -1
                out.insert(0 if j is None or j < 0 else j + 1, f)
                changes.append(f"Moved “{f.get('label')}”")
            else:
                sets = e.get("set") if isinstance(e.get("set"), dict) else {}
                for k, v in sets.items():
                    if k == "type":
                        t = TYPE_SYNONYMS.get(str(v).lower(), str(v).lower())
                        if t in LABELS:
                            f["type"] = t
                            if t in CHOICE and not f.get("options"):
                                f["options"] = ["Yes", "No"]
                    elif k == "role":
                        f["role"] = rmap.get(str(v).strip().lower(), f.get("role"))
                    elif k == "show_if" and isinstance(v, dict):
                        j = _find(out, v.get("field"))
                        if j is not None and out[j] is not f:
                            f["condition"] = {"logic": "and", "rules": [{"field": out[j]["id"], "op": v.get("op") if v.get("op") in OPS else "eq",
                                                                         "value": str(v.get("value", ""))[:120]}]}
                    elif k == "show_if" and not v:
                        f.pop("condition", None)
                    elif k == "options" and isinstance(v, list):
                        f["options"] = [str(o)[:120] for o in v if str(o).strip()]
                    elif k == "required":
                        f["required"] = bool(v)
                    elif k == "width":
                        f["width"] = "half" if v == "half" else "full"
                        if v != "half":
                            f.pop("width", None)
                    elif k in ("label", "placeholder", "help", "text", "pattern", "pattern_message"):
                        f[k] = str(v)[:600]
                changes.append(f"Updated “{f.get('label')}”: " + ", ".join(sets))
    return {"fields": out, "changes": changes, "notes": notes}


REVIEW_SYSTEM = """You review a business form like an experienced form designer: missing important questions, wrong field
types, fields that should (not) be required, missing validation, unclear labels, missing declaration/signature, ordering
and grouping, follow-up questions that should be conditional. Reply with ONE JSON object only:
{"suggestions": [{"title": "short", "why": "one sentence", "edits": [<edit>, ...]}]} with 3-6 suggestions, most valuable
first, each with the edits that implement it. """ + EDIT_SYSTEM.split("Reply with ONE JSON object only:")[1].split("Only change")[0]


def review(ws, fields, roles, user=None):
    prompt = f"ROLES: {_roles_text(roles)}\n\nFORM:\n{outline(fields, roles)}"
    data = _chat_json(ws, REVIEW_SYSTEM, prompt, user=user, kind="form_review")
    out = []
    for s in (data.get("suggestions") or [])[:6]:
        if isinstance(s, dict) and s.get("edits"):
            out.append({"title": str(s.get("title") or "")[:120], "why": str(s.get("why") or "")[:300], "edits": s["edits"][:10]})
    return out


# ---- reading existing forms ------------------------------------------------------------------------------------------
def text_from_upload(upload):
    """Text of a PDF / Word (.docx) / text file, or None for images (they go to a vision model)."""
    name = (upload.name or "").lower()
    raw = upload.read(15 * 1024 * 1024 + 1)
    if len(raw) > 15 * 1024 * 1024:
        raise FormAIError("That file is larger than 15 MB.")
    if name.endswith(".pdf") or raw[:5] == b"%PDF-":
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(raw))
            text = "\n".join((p.extract_text() or "") for p in reader.pages[:20])
            fields = reader.get_fields() or {}
        except Exception as exc:  # noqa: BLE001 - malformed PDFs
            raise FormAIError(f"Couldn't read that PDF ({type(exc).__name__}).")
        if fields:  # a fillable PDF: its own field names are a great hint
            text += "\n\nFILLABLE PDF FIELDS: " + ", ".join(list(fields)[:150])
        if not text.strip():
            raise FormAIError("That PDF has no text layer (it's a scan) — upload a photo/PNG of the page instead.")
        return text
    if name.endswith(".docx"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                xml = z.read("word/document.xml").decode("utf-8", "replace")
        except (zipfile.BadZipFile, KeyError):
            raise FormAIError("Couldn't read that Word file.")
        xml = re.sub(r"</w:p>", "\n", xml)
        return re.sub(r"<[^>]+>", "", xml)
    if name.endswith((".png", ".jpg", ".jpeg", ".webp")):
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise FormAIError("Upload a PDF, Word (.docx), text file or an image (PNG/JPG).")


def image_b64(upload):
    import base64

    upload.seek(0)
    raw = upload.read(10 * 1024 * 1024 + 1)
    if len(raw) > 10 * 1024 * 1024:
        raise FormAIError("Images must be under 10 MB.")
    return base64.b64encode(raw).decode()
