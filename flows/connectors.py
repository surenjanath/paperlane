"""Custom connectors: call a configured HTTP/JSON API (or query a read-only SQLite database) and map the answer onto fields.

Used by the “Run a custom connector” bot, by start-form lookups (source "connector") and by the API/MCP tools.
Templates: {field:Name} → a value of the document or form, {value} → the value typed in a form lookup.
"""
import base64
import json
import re
import sqlite3
import time
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from django.utils import timezone

MAX_BYTES = 1024 * 1024
TOKEN = re.compile(r"\{(field:[^}]+|value)\}")


class ConnectorError(Exception):
    pass


def _lookup(values, token):
    if token == "value":
        return values.get("@value", "")
    name = token[6:].strip()
    v = values.get(name)
    if v is None:  # case-insensitive fallback
        low = name.lower()
        v = next((x for k, x in values.items() if str(k).lower() == low), "")
    if isinstance(v, (list, dict)):
        return json.dumps(v)
    return "" if v is None else str(v)


def render(template, values, *, mode="text"):
    """Fill {field:X}/{value} tokens. mode: text | url (percent-encoded) | json (escaped inside JSON strings)."""
    def sub(m):
        v = _lookup(values, m.group(1))
        if mode == "url":
            return urllib.parse.quote(v, safe="")
        if mode == "json":
            return json.dumps(v)[1:-1]
        return v
    return TOKEN.sub(sub, template or "")


def dig(obj, path):
    """a.b.0.c paths into JSON (also accepts $.a.b)."""
    path = (path or "").strip()
    if path.startswith("$."):
        path = path[2:]
    if path in ("", "$"):
        return obj
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.lstrip("-").isdigit() and -len(obj) <= int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


def build_request(connector, action, values):
    method = (action.get("method") or "GET").upper()
    url = connector.base_url.rstrip("/") + "/" + render(action.get("path", ""), values, mode="url").lstrip("/")
    query = {k: render(str(v), values) for k, v in (action.get("query") or {}).items()}
    headers = {"Accept": "application/json", "User-Agent": "Paperlane-Connector/1.0",
               **{k: render(str(v), values) for k, v in (connector.headers or {}).items()}}
    if connector.auth_type == connector.AUTH_BEARER and connector.secret:
        headers["Authorization"] = f"Bearer {connector.secret}"
    elif connector.auth_type == connector.AUTH_HEADER and connector.secret:
        headers[connector.auth_name or "X-API-Key"] = connector.secret
    elif connector.auth_type == connector.AUTH_BASIC and connector.secret:
        headers["Authorization"] = "Basic " + base64.b64encode(connector.secret.encode()).decode()
    elif connector.auth_type == connector.AUTH_QUERY and connector.secret:
        query[connector.auth_name or "api_key"] = connector.secret
    if query:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
    body = None
    if method in ("POST", "PUT", "PATCH") and (action.get("body") or "").strip():
        raw = render(action["body"], values, mode="json")
        try:
            json.loads(raw)
        except ValueError as exc:
            raise ConnectorError(f"The request body is not valid JSON after filling in values: {exc}")
        body = raw.encode()
        headers["Content-Type"] = "application/json"
    if not url.lower().startswith(("http://", "https://")):
        raise ConnectorError("Only http(s) URLs are allowed.")
    return method, url, headers, body


def run(connector, action_key, values, *, value=None, save_status=True):
    """Call an action. Returns {"ok", "status", "data", "mapped": {field: value}, "error", "url"}."""
    action = connector.action(action_key)
    if action is None:
        raise ConnectorError(f"“{connector.name}” has no action “{action_key}”.")
    vals = dict(values or {})
    if value is not None:
        vals["@value"] = value
    if connector.kind == connector.KIND_SQLITE:
        return _run_sqlite(connector, action, vals, save_status)
    method, url, headers, body = build_request(connector, action, vals)
    safe_url = url if connector.auth_type != connector.AUTH_QUERY else url.replace(urllib.parse.quote(connector.secret, safe=""), "•••")
    out = {"ok": False, "status": None, "data": None, "mapped": {}, "error": "", "url": safe_url, "method": method}
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    from superadmin.netguard import BlockedAddress, check_url

    try:
        check_url(url)
    except BlockedAddress as exc:
        out["error"] = str(exc)
        if save_status:
            connector.last_status = f"{method} blocked"[:200]
            connector.save(update_fields=["last_status"])
        return out
    try:
        with urllib.request.urlopen(req, timeout=connector.timeout or 15) as resp:  # noqa: S310 - admin-configured endpoint
            out["status"] = resp.status
            raw = resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        out["status"], raw = exc.code, exc.read(MAX_BYTES + 1)
        out["error"] = f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        out["error"] = f"Could not reach the API: {getattr(exc, 'reason', exc)}"
        raw = b""
    if len(raw) > MAX_BYTES:
        out["error"] = "The response is larger than 1 MB."
        raw = raw[:MAX_BYTES]
    text = raw.decode(errors="replace")
    try:
        out["data"] = json.loads(text) if text.strip() else None
    except ValueError:
        out["data"] = text[:2000]
    if out["status"] and 200 <= out["status"] < 300 and not out["error"]:
        out["ok"] = True
        for m in action.get("map") or []:
            v = dig(out["data"], m.get("path", ""))
            if v is not None and m.get("field"):
                out["mapped"][m["field"]] = v if not isinstance(v, (dict, list)) else json.dumps(v)
    if save_status:
        connector.last_status = (f"{method} {out['status'] or ''} {out['error'] or 'OK'}").strip()[:200]
        connector.last_used_at = timezone.now()
        connector.save(update_fields=["last_status", "last_used_at"])
    return out


def parse_map(text):
    """“Field name = json.path” lines → [{"field", "path"}]."""
    out = []
    for line in (text or "").splitlines():
        if "=" in line:
            field, path = line.split("=", 1)
            if field.strip() and path.strip():
                out.append({"field": field.strip(), "path": path.strip()})
    return out


def map_text(items):
    return "\n".join(f"{m['field']} = {m['path']}" for m in items or [])


# ---- read-only SQLite --------------------------------------------------------------------------------------
SQL_ROWS = 200
SQL_SECONDS = 3
SQL_SUFFIXES = (".sqlite", ".sqlite3", ".db")
_READ_ONLY_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, getattr(sqlite3, "SQLITE_RECURSIVE", 33)}
_BLOCKED_FUNCTIONS = {"load_extension", "readfile", "writefile", "fts3_tokenizer", "edit"}


def database_path(name):
    """Resolve a connector's database inside settings.PAPERLANE_DATA_DIRS, or raise ConnectorError."""
    from django.conf import settings

    rel = (name or "").strip()
    if not rel or not rel.lower().endswith(SQL_SUFFIXES):
        raise ConnectorError("Name a .sqlite, .sqlite3 or .db file.")
    if Path(rel).is_absolute():
        raise ConnectorError("Give the file's path inside the data folder, not a full path.")
    for root in settings.PAPERLANE_DATA_DIRS:
        root = Path(root).resolve()
        path = (root / rel).resolve()
        if path.is_relative_to(root) and path.is_file():
            return path
    raise ConnectorError("That database file isn't in an allowed data folder (PAPERLANE_DATA_DIRS).")


def check_sql(sql):
    """One SELECT (or WITH … SELECT) statement; anything else is refused before it reaches the database."""
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        raise ConnectorError("Write a SELECT query.")
    if ";" in TOKEN.sub("", text):
        raise ConnectorError("Use a single SELECT statement.")
    if not re.match(r"(?is)^(select|with)\b", text):
        raise ConnectorError("Only SELECT queries are allowed.")
    return text


def _sql_params(template, values):
    """{field:X}/{value} tokens become ? parameters (never pasted into the SQL text)."""
    params = []

    def sub(m):
        params.append(_lookup(values, m.group(1)))
        return "?"
    return TOKEN.sub(sub, template), params


def _authorizer(action, arg1, arg2, dbname, source):
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in _BLOCKED_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in _READ_ONLY_ACTIONS else sqlite3.SQLITE_DENY


def query_sqlite(connector, sql_template, values):
    """Run a read-only query. Returns (columns, rows as dicts)."""
    sql, params = _sql_params(check_sql(sql_template), values)
    path = database_path(connector.database)
    con = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2)
    try:
        con.execute("PRAGMA query_only = ON")
        con.set_authorizer(_authorizer)
        deadline = time.monotonic() + min(connector.timeout or SQL_SECONDS, SQL_SECONDS)
        con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 2000)
        con.row_factory = sqlite3.Row
        try:
            cur = con.execute(sql, params)
            rows = cur.fetchmany(SQL_ROWS)
        except sqlite3.DatabaseError as exc:
            msg = "The query took too long." if "interrupted" in str(exc) else f"Query failed: {exc}"
            raise ConnectorError(msg)
        cols = [d[0] for d in cur.description or []]
        # SQLite REAL columns give 6350.0 for whole amounts; show them as 6350 in documents
        tidy = lambda v: int(v) if isinstance(v, float) and v.is_integer() else v  # noqa: E731
        return cols, [{c: tidy(r[c]) for c in cols} for r in rows]
    finally:
        con.close()


def _run_sqlite(connector, action, vals, save_status):
    out = {"ok": False, "status": None, "data": None, "rows": [], "mapped": {}, "error": "", "url": f"sqlite: {connector.database}",
           "method": "SELECT"}
    try:
        cols, rows = query_sqlite(connector, action.get("sql", ""), vals)
    except ConnectorError as exc:
        out["error"] = str(exc)
    else:
        out.update(ok=True, status=len(rows), rows=rows, columns=cols, data=rows[0] if rows else None)
        for m in action.get("map") or []:
            v = dig(out["data"], m.get("path", "")) if out["data"] is not None else None
            if v is not None and m.get("field"):
                out["mapped"][m["field"]] = v
    if save_status:
        connector.last_status = (f"SELECT {len(out['rows'])} row{'s' if len(out['rows']) != 1 else ''}" if out["ok"] else out["error"])[:200]
        connector.last_used_at = timezone.now()
        connector.save(update_fields=["last_status", "last_used_at"])
    return out
