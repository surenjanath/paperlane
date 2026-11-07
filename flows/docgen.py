"""Fill Word (.docx) templates from a document's data — and turn them into PDF when LibreOffice is available.

Template language (typed in Word as normal text):
    {Field name}                       the field's value ({field:Field name} works too)
    {document_name} {workflow_name} {date}   built-in values
    {if Amount > 10000} … {endif}      keep the text only when the rule holds (=, !=, >, <, >=, <=, contains;
                                        a lone {if Field} means "has a value"); may span paragraphs and table rows
    {Items}                            a table field becomes one line per row
Word often splits typed text into several runs; each paragraph is joined before replacing, so placeholders work
however they were typed (formatting inside a single placeholder is lost, the paragraph's style is kept).
Sensitive fields are filled only when ``include_sensitive`` is set (the bot's choice).
"""
import io
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from html import escape, unescape

from django.utils import timezone

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PARTS = re.compile(r"^word/(document|header\d*|footer\d*)\.xml$")
PARA = re.compile(r"<w:p[ >].*?</w:p>|<w:p/>", re.S)
TEXT = re.compile(r"(<w:t(?: [^>]*)?>)(.*?)(</w:t>)", re.S)
TOKEN = re.compile(r"\{([^{}]{1,120})\}")
IF = re.compile(r"\{if\s+([^{}]+?)\}", re.I)
ENDIF = re.compile(r"\{endif\}", re.I)
MARK = "<!--sd-mark-->"
OPS = [(">=", "gte"), ("<=", "lte"), ("!=", "neq"), ("=", "eq"), (">", "gt"), ("<", "lt"), (" contains ", "contains")]


class TemplateError(ValueError):
    pass


def values_for(packet, include_sensitive=False):
    """{name/label/id (lower): text} for every field, plus built-ins."""
    out = {"document_name": packet.name, "workflow_name": packet.workflow.name,
           "date": timezone.localdate().strftime("%d %b %Y"), "status": packet.get_status_display()}
    for doc in packet.documents:
        vals = (packet.data or {}).get(doc["key"], {})
        for f in doc.get("fields", []):
            if f.get("sensitive") and not include_sensitive:
                continue
            v = vals.get(f["id"])
            if f.get("type") == "table" and isinstance(v, list):
                text = "\n".join(", ".join(f"{k}: {x}" for k, x in row.items()) if isinstance(row, dict) else str(row) for row in v)
            elif isinstance(v, list):
                text = ", ".join(str(x) for x in v)
            elif isinstance(v, dict):
                text = ", ".join(str(x) for x in v.values())
            elif f.get("type") in ("signature", "initials", "attachment"):
                text = "Signed" if v and f.get("type") != "attachment" else ""
            else:
                text = "" if v is None else str(v)
            for key in (f.get("name"), f.get("label"), f["id"]):
                if key:
                    out.setdefault(str(key).strip().lower(), text)
    return out


def _lookup(values, name):
    name = name.strip()
    if name.lower().startswith("field:"):
        name = name[6:]
    return values.get(name.strip().lower())


def _holds(rule, values):
    from .conditions import check_rule

    rule = unescape(rule).strip()
    for sym, op in OPS:
        if sym in rule:
            field, expected = rule.split(sym, 1)
            v = _lookup(values, field)
            return check_rule({"field": "x", "op": op, "value": expected.strip().strip('"')}, {"x": v or ""})
    return bool((_lookup(values, rule) or "").strip())


def _para_text(p):
    return "".join(m.group(2) for m in TEXT.finditer(p))


def _set_para_text(p, text):
    """Put ``text`` in the paragraph's first text run and empty the others (newlines become line breaks)."""
    runs = list(TEXT.finditer(p))
    if not runs:
        return p
    parts = text.split("\n")
    first = runs[0]
    open_tag = first.group(1) if "xml:space" in first.group(1) else first.group(1).replace("<w:t", '<w:t xml:space="preserve"', 1)
    body = f"</w:t><w:br/>{open_tag}".join(parts)
    out, last = [], 0
    for i, m in enumerate(runs):
        out.append(p[last:m.start()])
        out.append(f"{open_tag}{body}</w:t>" if i == 0 else f"{m.group(1)}</w:t>")
        last = m.end()
    out.append(p[last:])
    return "".join(out)


def _render_xml(xml, values, missing):
    # conditions: work paragraph by paragraph so a block can span paragraphs and table rows
    paras = list(PARA.finditer(xml))
    out, last, depth_skip, stack = [], 0, 0, []
    for m in paras:
        p = m.group(0)
        text = unescape(_para_text(p))
        out.append(xml[last:m.start()])  # structure between paragraphs (rows, cells) always stays
        last = m.end()
        opens, closes = len(IF.findall(text)), len(ENDIF.findall(text))
        inline = opens and opens == closes  # a whole {if}…{endif} inside this paragraph
        if inline:
            def cut(s):
                while True:
                    mm = IF.search(s)
                    if not mm:
                        return s
                    end = ENDIF.search(s, mm.end())
                    if not end:
                        return s
                    keep = s[mm.end():end.start()] if _holds(mm.group(1), values) else ""
                    s = s[:mm.start()] + keep + s[end.end():]
            if not depth_skip:
                out.append(_set_para_text(p, escape(_fill_raw(cut(text), values, missing), quote=False)))
            continue
        if opens:  # a block starts here and ends in a later paragraph
            holds = _holds(IF.search(text).group(1), values)
            stack.append(holds)
            if not holds:
                depth_skip += 1
            rest = IF.sub("", text).strip()
            if rest and not depth_skip:
                out.append(_set_para_text(p, escape(_fill_raw(rest, values, missing), quote=False)))
            elif not rest:
                out.append(MARK)
            continue
        if closes:
            if stack and not stack.pop():
                depth_skip = max(0, depth_skip - 1)
            rest = ENDIF.sub("", text).strip()
            if rest and not depth_skip:
                out.append(_set_para_text(p, escape(_fill_raw(rest, values, missing), quote=False)))
            elif not rest:
                out.append(MARK)
            continue
        if depth_skip:
            continue
        if "{" in text:
            out.append(_set_para_text(p, escape(_fill_raw(text, values, missing), quote=False)))
        else:
            out.append(p)
    out.append(xml[last:])
    return _drop_empty_rows("".join(out)).replace(MARK, "")


def _fill_raw(text, values, missing):
    def sub(m):
        inner = m.group(1).strip()
        v = _lookup(values, inner)
        if v is None:
            missing.add(inner)
            return ""
        return v
    return TOKEN.sub(sub, text)


def _drop_empty_rows(xml):
    """Table rows whose cells lost every paragraph to a false {if} would make Word refuse the file: drop them."""
    def fix_row(m):
        row = m.group(0)
        cells = re.findall(r"<w:tc>.*?</w:tc>|<w:tc .*?</w:tc>", row, re.S)
        if cells and all("<w:p" not in c for c in cells):
            return ""
        if MARK in row and not any(t.strip() for t in re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", row, re.S)):
            return ""  # a row that only held {if} / {endif}
        return re.sub(r"(<w:tc(?: [^>]*)?>)((?:(?!<w:p).)*?)(</w:tc>)", r"\1\2<w:p/>\3", row, flags=re.S)
    return re.sub(r"<w:tr[ >].*?</w:tr>", fix_row, xml, flags=re.S)


def render(template_bytes, values):
    """The filled .docx (bytes) and the placeholders that matched no field."""
    try:
        src = zipfile.ZipFile(io.BytesIO(template_bytes))
    except zipfile.BadZipFile:
        raise TemplateError("The template must be a Word .docx file.")
    if "word/document.xml" not in src.namelist():
        raise TemplateError("The template must be a Word .docx file.")
    out, missing = io.BytesIO(), set()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if PARTS.match(item.filename):
                data = _render_xml(data.decode("utf-8"), values, missing).encode("utf-8")
            dst.writestr(item, data)
    return out.getvalue(), sorted(missing)


def soffice():
    for path in (os.environ.get("PAPERLANE_SOFFICE"), shutil.which("soffice"), shutil.which("libreoffice"),
                 "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if path and os.path.exists(path):
            return path
    return ""


def to_pdf(docx_bytes, timeout=90):
    """PDF bytes via LibreOffice, or None when it isn't installed or fails."""
    exe = soffice()
    if not exe:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "doc.docx")
        with open(src, "wb") as fh:
            fh.write(docx_bytes)
        try:
            subprocess.run([exe, "--headless", f"-env:UserInstallation=file://{tmp}/profile", "--convert-to", "pdf",
                            "--outdir", tmp, src], check=True, capture_output=True, timeout=timeout)
        except (subprocess.SubprocessError, OSError):
            return None
        pdf = os.path.join(tmp, "doc.pdf")
        return open(pdf, "rb").read() if os.path.exists(pdf) else None
