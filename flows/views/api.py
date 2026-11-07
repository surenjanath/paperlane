"""Token-authenticated HTTP access: the remote MCP endpoint (Streamable HTTP, JSON responses) and the REST API.

Both run the tools in flows/mcp.py as the token's user inside the token's workspace.
    Authorization: Bearer sd_<prefix>_<secret>
"""
import json

from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from accounts.models import ApiToken
from flows.mcp import TOOLS, Server, ToolError

RATE_PER_MINUTE = 120
MAX_BODY = 2 * 1024 * 1024


def _error(status, message, **extra):
    return JsonResponse({"error": message, **extra}, status=status)


def _authenticate(request):
    """(token, server) or an error response."""
    header = request.META.get("HTTP_AUTHORIZATION", "")
    raw = header[7:].strip() if header.lower().startswith("bearer ") else ""
    tok = ApiToken.authenticate(raw)
    if tok is None:
        resp = _error(401, "Missing, invalid, expired or revoked API token. Create one in My account › API & AI access.")
        resp["WWW-Authenticate"] = 'Bearer realm="Paperlane"'
        return None, resp
    if not tok.workspace.feature("api") or tok.workspace.suspended_at:
        return None, _error(403, "API access is turned off for this workspace by the platform administrator.")
    key = f"api-rate:{tok.pk}:{int(timezone.now().timestamp() // 60)}"
    n = cache.get(key, 0) + 1
    cache.set(key, n, 120)
    if n > RATE_PER_MINUTE:
        resp = _error(429, f"Rate limit: {RATE_PER_MINUTE} requests per minute per token.")
        resp["Retry-After"] = "60"
        return None, resp
    now = timezone.now()
    if not tok.last_used_at or (now - tok.last_used_at).total_seconds() > 60:
        ApiToken.objects.filter(pk=tok.pk).update(last_used_at=now)
    return tok, Server(tok.workspace, tok.user, read_only=tok.scope == ApiToken.READ)


def _body(request):
    if len(request.body or b"") > MAX_BODY:
        raise ValueError("Request body is too large (2 MB max).")
    if not request.body:
        return {}
    try:
        return json.loads(request.body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Body is not valid JSON: {exc.msg}")


def _foreign_origin(request):
    """Browsers send Origin; only allow our own (protects local servers from DNS-rebinding pages)."""
    origin = request.META.get("HTTP_ORIGIN")
    if not origin:
        return False
    host = origin.split("://", 1)[-1].rstrip("/")
    return host != request.get_host()


# ---- MCP over HTTP ------------------------------------------------------------------------------
@csrf_exempt
def mcp_http(request):
    if _foreign_origin(request):
        return _error(403, "Cross-origin requests are not allowed.")
    if request.method != "POST":
        resp = HttpResponse(status=405)  # no server-initiated stream; clients POST every message
        resp["Allow"] = "POST"
        return resp
    tok, server = _authenticate(request)
    if tok is None:
        return server
    try:
        msg = _body(request)
    except ValueError as exc:
        return JsonResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}, status=400)
    if isinstance(msg, list):
        out = [r for r in (server.handle(m) for m in msg if isinstance(m, dict)) if r]
    elif isinstance(msg, dict):
        out = server.handle(msg)
    else:
        return JsonResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}, status=400)
    if not out:  # only notifications / responses
        return HttpResponse(status=202)
    return JsonResponse(out, safe=False, json_dumps_params={"default": str})


# ---- REST API -----------------------------------------------------------------------------------
ROUTES = [
    # method, pattern (for the index), tool, description
    ("GET", "/api/v1/me", None, "The token's user, workspace and access"),
    ("GET", "/api/v1/guide", "get_build_guide", "Blueprint reference (field types, bots, conditions…)"),
    ("GET", "/api/v1/workflows?query=", "list_workflows", "List workflows"),
    ("POST", "/api/v1/workflows?publish=1&replace=0", "build_workflow", "Create a workflow from a blueprint (JSON body)"),
    ("GET", "/api/v1/workflows/{id}", "get_workflow", "A workflow as a blueprint"),
    ("POST", "/api/v1/workflows/{id}/publish", "publish_workflow", "Publish"),
    ("POST", "/api/v1/workflows/{id}/start", "start_workflow", "Start: {prefill, recipient_email, document_name, test}"),
    ("DELETE", "/api/v1/workflows/{id}", "trash_workflow", "Move to Trash"),
    ("GET", "/api/v1/documents?workflow_id=&status=&limit=", "list_documents", "List documents"),
    ("GET", "/api/v1/documents/{id}", "get_document", "Document status, history, open steps and values"),
    ("POST", "/api/v1/documents/{id}/complete", "complete_step", "Complete the open step: {step_name, values}"),
    ("GET", "/api/v1/forms", "list_forms", "List start forms"),
    ("POST", "/api/v1/forms", "build_form", "Create a start form (JSON body)"),
    ("GET", "/api/v1/forms/{id}", "get_form", "A start form"),
    ("POST", "/api/v1/forms/{id}/publish", "publish_form", "Publish a start form"),
    ("POST", "/api/v1/forms/{id}/submit", "submit_form", "Submit values: {values}"),
    ("GET", "/api/v1/team", "list_team", "Teammates and groups"),
    ("GET", "/api/v1/spreadsheets", "list_spreadsheets", "List spreadsheets"),
    ("POST", "/api/v1/spreadsheets", "create_spreadsheet", "Create/append: {name, columns, rows}"),
    ("GET", "/api/v1/spreadsheets/{name}", "get_spreadsheet", "Rows of a spreadsheet"),
    ("POST", "/api/v1/tools/{tool}", None, "Call any MCP tool with its arguments as the JSON body"),
]


def _status_for(message):
    m = message.lower()
    if m.startswith("no ") or "unknown tool" in m:
        return 404
    if "read-only" in m or "cannot" in m or "only the step" in m:
        return 403
    return 400


def _run(server, tool, args, ok_status=200):
    try:
        return JsonResponse(server.run(tool, args), safe=False, status=ok_status, json_dumps_params={"default": str})
    except ToolError as exc:
        return _error(_status_for(str(exc)), str(exc))


def _flag(request, name, default):
    v = request.GET.get(name)
    return default if v is None else v.lower() in ("1", "true", "yes", "on")


@csrf_exempt
def rest(request, resource="", pk=None, action=""):
    if _foreign_origin(request):
        return _error(403, "Cross-origin requests are not allowed.")
    tok, server = _authenticate(request)
    if tok is None:
        return server
    method = request.method
    try:
        body = _body(request) if method in ("POST", "PUT", "PATCH") else {}
    except ValueError as exc:
        return _error(400, str(exc))
    if not isinstance(body, dict):
        return _error(400, "The JSON body must be an object.")
    q = request.GET

    def wrong():
        return _error(405, f"{method} is not supported here. GET /api/v1/ lists the endpoints.")

    if resource == "":
        return JsonResponse({"api": "Paperlane REST API v1", "auth": "Authorization: Bearer <token>",
                             "mcp": request.build_absolute_uri(reverse("mcp_http")),
                             "endpoints": [{"method": m, "path": p, "description": d} for m, p, _, d in ROUTES]})
    if resource == "me":
        return JsonResponse({"user": tok.user.email, "workspace": {"slug": tok.workspace.slug, "name": tok.workspace.name},
                             "role": server.m.role, "scope": tok.scope, "token": tok.name,
                             "expires_at": tok.expires_at.isoformat() if tok.expires_at else None,
                             "tools": server.tool_names()})
    if resource == "guide":
        return _run(server, "get_build_guide", {})
    if resource == "tools":
        if method != "POST" or not action:
            return wrong()
        return _run(server, action, body)
    if resource == "workflows":
        if pk is None:
            if method == "GET":
                return _run(server, "list_workflows", {"query": q.get("query", "")})
            if method == "POST":
                return _run(server, "build_workflow", {"blueprint": body, "publish": _flag(request, "publish", True),
                                                        "replace": _flag(request, "replace", False)}, 201)
            return wrong()
        if not action:
            if method == "GET":
                return _run(server, "get_workflow", {"workflow_id": pk})
            if method == "DELETE":
                return _run(server, "trash_workflow", {"workflow_id": pk})
            return wrong()
        if method != "POST":
            return wrong()
        if action == "publish":
            return _run(server, "publish_workflow", {"workflow_id": pk})
        if action == "start":
            return _run(server, "start_workflow", {"workflow_id": pk, **body}, 201)
    if resource == "documents":
        if pk is None and method == "GET":
            args = {k: q[k] for k in ("status",) if q.get(k)}
            for k in ("workflow_id", "limit"):
                if q.get(k, "").isdigit():
                    args[k] = int(q[k])
            return _run(server, "list_documents", args)
        if pk is not None and not action and method == "GET":
            return _run(server, "get_document", {"document_id": pk})
        if pk is not None and action == "complete" and method == "POST":
            return _run(server, "complete_step", {"document_id": pk, **body})
        return wrong()
    if resource == "forms":
        if pk is None:
            if method == "GET":
                return _run(server, "list_forms", {})
            if method == "POST":
                return _run(server, "build_form", {"form": body}, 201)
            return wrong()
        if not action and method == "GET":
            return _run(server, "get_form", {"form_id": pk})
        if action == "publish" and method == "POST":
            return _run(server, "publish_form", {"form_id": pk})
        if action == "submit" and method == "POST":
            return _run(server, "submit_form", {"form_id": pk, "values": body.get("values", body)}, 201)
        return wrong()
    if resource == "team" and method == "GET":
        return _run(server, "list_team", {})
    if resource == "spreadsheets":
        if action and method == "GET":
            return _run(server, "get_spreadsheet", {"name": action, **({"limit": int(q["limit"])} if q.get("limit", "").isdigit() else {})})
        if not action and method == "GET":
            return _run(server, "list_spreadsheets", {})
        if not action and method == "POST":
            return _run(server, "create_spreadsheet", body, 201)
        return wrong()
    return _error(404, "Unknown endpoint. GET /api/v1/ lists the endpoints.")


assert {t for _, _, t, _ in ROUTES if t} <= {n for n, _, _ in TOOLS}
