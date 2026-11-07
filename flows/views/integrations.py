"""My workspace › Connectors (custom HTTP/JSON APIs) and › AI & models (Ollama / OpenAI-compatible / Claude)."""
import json
import re
import time

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.conf import settings
from django.core.validators import URLValidator
from django.urls import reverse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .. import ai_actions, ai_providers, connectors, engine
from ..models import AIUsage, Connector, Packet

METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


def _manager(request):
    if not request.membership or not request.membership.can_manage:
        raise PermissionDenied("Only workspace owners and supervisors can manage connectors and AI settings.")


def _gate(request, key):
    from superadmin.features import feature_off

    return feature_off(request, key)


def _lines(text):
    """“key = value” lines → dict."""
    out = {}
    for line in (text or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            if k.strip():
                out[k.strip()] = v.strip()
    return out


def _dict_lines(d):
    return "\n".join(f"{k} = {v}" for k, v in (d or {}).items())


def _apply_general(request, c):
    p = request.POST
    if not c.pk:
        c.kind = Connector.KIND_SQLITE if p.get("kind") == Connector.KIND_SQLITE else Connector.KIND_HTTP
    if c.kind == Connector.KIND_SQLITE:
        connectors.database_path(p.get("database", ""))  # must exist inside an allowed data folder
        c.database = p.get("database", "").strip()[:255]
        url = ""
    else:
        url = p.get("base_url", "").strip()
        URLValidator(schemes=["http", "https"])(url)
    c.name = p.get("name", "").strip()[:120] or "Connector"
    c.description = p.get("description", "").strip()[:300]
    c.base_url = url
    c.auth_type = p.get("auth_type") if p.get("auth_type") in dict(Connector.AUTHS) else Connector.AUTH_NONE
    c.auth_name = p.get("auth_name", "").strip()[:80]
    if p.get("secret", "").strip() and p.get("secret") != "••••••••":
        c.secret = p.get("secret").strip()[:500]
    if p.get("clear_secret") == "on":
        c.secret = ""
    c.headers = _lines(p.get("headers"))
    try:
        c.timeout = max(1, min(60, int(p.get("timeout") or 15)))
    except ValueError:
        c.timeout = 15
    c.active = p.get("active") == "on" if p.get("form") == "general" else True


@login_required
def connector_list(request):
    _manager(request)
    if (off := _gate(request, "connectors")) is not None:
        return off
    if request.method == "POST":
        c = Connector(workspace=request.workspace, created_by=request.user)
        try:
            _apply_general(request, c)
        except connectors.ConnectorError as exc:
            messages.error(request, str(exc))
            return redirect("flows:connectors")
        except Exception:
            messages.error(request, "Enter a name and a valid http(s) base URL.")
            return redirect("flows:connectors")
        if Connector.objects.filter(workspace=request.workspace, name=c.name).exists():
            messages.error(request, f"A connector named “{c.name}” already exists.")
            return redirect("flows:connectors")
        c.actions = ([{"key": "lookup", "name": "Look up", "sql": "", "map": []}] if c.kind == Connector.KIND_SQLITE
                     else [{"key": "get", "name": "Get", "method": "GET", "path": "", "query": {}, "body": "", "map": []}])
        c.save()
        engine.audit(request.workspace, "connector.created", c.name, actor=request.user, request=request)
        messages.success(request, "Connector created. Define its actions, then test them.")
        return redirect("flows:connector_detail", c.pk)
    return render(request, "flows/connectors.html", {
        "section": "connectors", "items": Connector.objects.filter(workspace=request.workspace), "auths": Connector.AUTHS,
        "examples": EXAMPLES, "kinds": Connector.KINDS, "data_dirs": [p.name for p in settings.PAPERLANE_DATA_DIRS]})


EXAMPLES = [
    ("Policy system", "https://policies.example.com/api", "bearer",
     "GET policies/{value} → map policy.holder, policy.fund_value …"),
    ("n8n / Make / Zapier webhook", "https://n8n.example.com/webhook", "header", "POST a JSON body built from {field:…} values"),
    ("Any REST API", "https://api.example.com/v1", "none", "Lookup, create or update records"),
]


@login_required
def connector_detail(request, pk):
    _manager(request)
    if (off := _gate(request, "connectors")) is not None:
        return off
    c = get_object_or_404(Connector, workspace=request.workspace, pk=pk)
    if request.method == "POST":
        what = request.POST.get("form")
        if what == "general":
            try:
                _apply_general(request, c)
            except connectors.ConnectorError as exc:
                messages.error(request, str(exc))
                return redirect("flows:connector_detail", c.pk)
            except Exception:
                messages.error(request, "Enter a valid http(s) base URL.")
                return redirect("flows:connector_detail", c.pk)
            c.save()
            messages.success(request, "Connector saved.")
        elif what == "action":
            p = request.POST
            key = re.sub(r"[^a-z0-9_]+", "_", p.get("key", "").strip().lower()).strip("_")[:40]
            if not key:
                messages.error(request, "Give the action a key (letters, numbers and _).")
                return redirect("flows:connector_detail", c.pk)
            if c.kind == Connector.KIND_SQLITE:
                action = {"key": key, "name": p.get("name", "").strip()[:80] or key, "sql": p.get("sql", "").strip()[:8000],
                          "map": connectors.parse_map(p.get("map"))}
                try:
                    connectors.check_sql(action["sql"])
                except connectors.ConnectorError as exc:
                    messages.error(request, str(exc))
                    return redirect("flows:connector_detail", c.pk)
            else:
                action = {"key": key, "name": p.get("name", "").strip()[:80] or key,
                          "method": p.get("method") if p.get("method") in METHODS else "GET",
                          "path": p.get("path", "").strip()[:500], "query": _lines(p.get("query")),
                          "body": p.get("body", "").strip()[:8000], "map": connectors.parse_map(p.get("map"))}
            if action.get("body"):
                try:
                    json.loads(connectors.render(action["body"], {}, mode="json"))
                except ValueError as exc:
                    messages.error(request, f"The body template is not valid JSON: {exc}")
                    return redirect("flows:connector_detail", c.pk)
            orig = p.get("orig_key", "")
            actions = [a for a in c.actions or [] if a.get("key") not in (orig, key)]
            c.actions = actions + [action]
            c.save(update_fields=["actions"])
            messages.success(request, f"Action “{action['name']}” saved.")
        return redirect("flows:connector_detail", c.pk)
    result = request.session.pop(f"connector_test_{c.pk}", None)
    return render(request, "flows/connector_detail.html", {
        "section": "connectors", "c": c, "auths": Connector.AUTHS, "methods": METHODS, "headers_text": _dict_lines(c.headers),
        "actions": [{**a, "query_text": _dict_lines(a.get("query")), "map_text": connectors.map_text(a.get("map"))}
                    for a in c.actions or []],
        "result": result, "result_json": json.dumps(result.get("rows") or result.get("data"), indent=2, default=str)[:6000] if result else "",
    })


@login_required
@require_POST
def connector_action(request, pk, action):
    _manager(request)
    if (off := _gate(request, "connectors")) is not None:
        return off
    c = get_object_or_404(Connector, workspace=request.workspace, pk=pk)
    op = action
    if op == "delete":
        c.delete()
        messages.success(request, "Connector deleted.")
        return redirect("flows:connectors")
    if op == "remove_action":
        c.actions = [a for a in c.actions or [] if a.get("key") != request.POST.get("key")]
        c.save(update_fields=["actions"])
        messages.success(request, "Action removed.")
    elif op == "test":
        values = _lines(request.POST.get("values"))
        doc = request.POST.get("document", "").strip()
        if doc.isdigit():  # test with a real document's values
            packet = Packet.objects.visible_to(request.membership).filter(pk=int(doc)).first()
            if packet:
                values = {**packet.flat_values(), **values}
        try:
            res = connectors.run(c, request.POST.get("key", ""), values, value=request.POST.get("value") or None)
            request.session[f"connector_test_{c.pk}"] = {**res, "key": request.POST.get("key", "")}
        except connectors.ConnectorError as exc:
            messages.error(request, str(exc))
    return redirect("flows:connector_detail", c.pk)


def _account(cfg, key):
    kind = ai_providers.provider_kind(key)
    if kind in ("", "ollama"):
        return {}
    slug = key.split(":", 1)[1] if str(key).startswith("compat:") else ""
    for p in (cfg or {}).get("providers") or []:
        if slug and p.get("id") == slug:
            return p
        if not slug and p.get("kind") == kind:
            return p
    return {}


def _compose_ai_settings(cfg, post):
    """The AI settings dict for one active provider and an optional backup. Raises EngineError for a missing key."""
    kind = post.get("provider") or ""
    if kind not in ("ollama", "anthropic", "nvidia", "openai", "compat"):
        kind = ai_providers.provider_kind(ai_providers.provider_key(post.get("engine") or "") or ai_providers.active_provider(cfg))
    providers = [dict(p) for p in (cfg.get("providers") or [])]
    provider_value = kind
    if kind == "compat":
        stored = cfg.get("provider") or ""
        custom_id = stored.split(":", 1)[1] if stored.startswith("compat:") else ""
        providers = ai_providers.save_provider(
            providers, kind="compat", api_key=post.get("api_key", ""), label=post.get("label", ""),
            base_url=post.get("base_url", ""), provider_id=custom_id)
        match = next((p for p in providers if p.get("kind") == "compat" and (not custom_id or p["id"] == custom_id)), None)
        provider_value = f"compat:{(match or providers[-1])['id']}"
    elif kind != "ollama":
        providers = ai_providers.save_provider(providers, kind=kind, api_key=post.get("api_key", ""))

    backup_kind = post.get("backup_provider") or ""
    if backup_kind not in ("", "ollama", "anthropic", "nvidia", "openai", "compat"):
        backup_kind = ""
    backup_value = ""
    if backup_kind == "ollama":
        backup_value = "ollama"
    elif backup_kind and backup_kind == kind:
        backup_value = provider_value
    elif backup_kind == "compat":
        stored_b = cfg.get("backup_provider") or ""
        b_id = stored_b.split(":", 1)[1] if stored_b.startswith("compat:") else ""
        providers = ai_providers.save_provider(
            providers, kind="compat", api_key=post.get("backup_api_key", ""), label=post.get("backup_label", ""),
            base_url=post.get("backup_base_url", ""), provider_id=b_id)
        backup_value = f"compat:{b_id}" if b_id else f"compat:{[p['id'] for p in providers if p.get('kind') == 'compat'][-1]}"
    elif backup_kind:
        providers = ai_providers.save_provider(providers, kind=backup_kind, api_key=post.get("backup_api_key", ""))
        backup_value = backup_kind

    engine = ai_providers.keep_engine(post.get("engine") or "", cfg.get("engine") or "", provider_value)
    ai = {"provider": provider_value, "engine": engine, "ollama_host": (post.get("ollama_host") or "").strip().rstrip("/"),
          "disabled": post.get("disabled") == "on", "ask_ai": post.get("ask_ai") == "on",
          "fill_assist": post.get("fill_assist") == "on", "nl_search": post.get("nl_search") == "on",
          "help_ai": post.get("help_ai") == "on", "daily_limit": _limit(post.get("daily_limit")),
          "embed_engine": (post.get("embed_engine") or "").strip()[:160], "watchdog": post.get("watchdog") == "on"}
    if backup_value:
        ai["backup_provider"] = backup_value
        backup_engine = ai_providers.keep_engine(post.get("backup_engine") or "", cfg.get("backup_engine") or "", backup_value)
        if backup_engine and backup_engine != engine:
            ai["backup_engine"] = backup_engine
    if providers or "providers" in cfg:
        ai["providers"] = providers
    for k in ("benchmarks", "vision_engine"):
        if k in cfg:
            ai[k] = cfg[k]
    return ai


def _ai_choice_page(cfg, engines):
    """Models for the active provider, and for the backup, for the settings page. Keys are never included."""
    active = ai_providers.active_provider(cfg)
    backup = cfg.get("backup_provider") or ""

    def listed(key):
        return [e for e in engines if ai_providers.matches_provider(e["id"], key)]

    active_models = listed(active)
    backup_models = listed(backup) if backup else []
    probe, seen = [], set()
    for e in active_models + backup_models:
        if e["id"] not in seen:
            probe.append(e)
            seen.add(e["id"])
    groups = {}
    for e in probe:
        groups.setdefault(e["group"], []).append(e)
    active_acc, backup_acc = _account(cfg, active), _account(cfg, backup)
    return {"active_kind": ai_providers.provider_kind(active), "backup_kind": ai_providers.provider_kind(backup) if backup else "",
            "active_models": active_models, "backup_models": backup_models,
            "engine_chosen": any(e["id"] == (cfg or {}).get("engine") for e in active_models),
            "backup_chosen": any(e["id"] == (cfg or {}).get("backup_engine") for e in backup_models),
            "active_has_key": bool(active_acc.get("api_key")), "backup_has_key": bool(backup_acc.get("api_key")),
            "custom_label": active_acc.get("label") or "", "custom_url": active_acc.get("base_url") or "",
            "backup_custom_label": backup_acc.get("label") or "", "backup_custom_url": backup_acc.get("base_url") or "",
            "engine_groups": list(groups.items()), "count": len(probe)}


# ---- AI & models -------------------------------------------------------------------------------------------------
@login_required
def ai_settings(request):
    _manager(request)
    if (off := _gate(request, "ai")) is not None:
        return off
    ws = request.workspace
    cfg = ai_actions.settings_for(ws)
    if request.method == "POST":
        if request.POST.get("form") == "provider_save":
            try:
                providers = ai_providers.save_provider(
                    cfg.get("providers"), kind=request.POST.get("kind", ""), api_key=request.POST.get("api_key", ""),
                    label=request.POST.get("label", ""), base_url=request.POST.get("base_url", ""),
                    provider_id=request.POST.get("provider_id", ""))
            except ai_providers.EngineError as exc:
                messages.error(request, str(exc))
                return redirect("flows:ai_settings")
            ai = dict(cfg)
            ai["providers"] = providers
            ws.settings = {**(ws.settings or {}), "ai": ai}
            ws.save(update_fields=["settings"])
            messages.success(request, "Provider saved.")
            return redirect("flows:ai_settings")
        if request.POST.get("form") == "provider_remove":
            providers, removed = ai_providers.remove_provider(cfg.get("providers"), request.POST.get("provider_id", ""))
            ai = dict(cfg)
            ai["providers"] = providers
            if removed:
                if ai_providers.engine_uses(ai.get("engine", ""), removed):
                    ai["engine"] = ""
                if ai_providers.engine_uses(ai.get("backup_engine", ""), removed):
                    ai.pop("backup_engine", None)
                if ai_providers.engine_uses(ai.get("fast_engine", ""), removed):
                    ai.pop("fast_engine", None)
                if ai_providers.engine_uses(ai.get("vision_engine", ""), removed):
                    ai.pop("vision_engine", None)
                kind, rid = removed.get("kind"), removed.get("id")
                owned = f"compat:{rid}" if kind == "compat" else kind
                if ai.get("provider") == owned:
                    ai["provider"] = "ollama"
                    ai["engine"] = ""
                if ai.get("backup_provider") == owned:
                    ai.pop("backup_provider", None)
                    ai.pop("backup_engine", None)
            ws.settings = {**(ws.settings or {}), "ai": ai}
            ws.save(update_fields=["settings"])
            messages.success(request, "Provider removed.")
            return redirect("flows:ai_settings")
        if request.POST.get("form") == "test":
            engine_id = request.POST.get("engine") or cfg.get("engine") or ""
            started = time.monotonic()
            try:
                with ai_providers.bind(cfg):
                    reply = ai_providers.chat(engine_id, "Reply with exactly: OK", [{"role": "user", "content": "Are you there?"}],
                                              json_mode=False)
                    name = ai_providers.label(engine_id)
                messages.success(request, f"{name} answered in {time.monotonic() - started:.1f} s: “{reply.strip()[:80]}”")
            except Exception as exc:  # network, model missing …
                messages.error(request, f"{engine_id or 'No model'}: {exc}")
            return redirect("flows:ai_settings")
        if request.POST.get("form") == "benchmark":
            from .. import ai_eval

            eng = request.POST.get("engine") or cfg.get("engine") or ""
            if not eng:
                messages.error(request, "Choose a model to test.")
            elif ai_eval.start(ws, eng):
                with ai_providers.bind(cfg):
                    name = ai_providers.label(eng)
                messages.success(request, f"Benchmark started for {name} — results appear below as each task finishes.")
            return redirect(reverse("flows:ai_settings") + "#bench")
        host = request.POST.get("ollama_host", "").strip()
        if host and not host.startswith(("http://", "https://")):
            messages.error(request, "The Ollama address must start with http:// or https://")
            return redirect("flows:ai_settings")
        try:
            ai = _compose_ai_settings(cfg, request.POST)
        except ai_providers.EngineError as exc:
            messages.error(request, str(exc))
            return redirect("flows:ai_settings")
        ws.settings = {**(ws.settings or {}), "ai": ai}
        ws.save(update_fields=["settings"])
        messages.success(request, "AI settings saved.")
        return redirect("flows:ai_settings")
    with ai_providers.bind(cfg):
        engines = ai_providers.engines(refresh=request.GET.get("refresh") == "1")
        host = ai_providers._ollama_url()
        marks = sorted(((ai_providers.label(k), v) for k, v in (cfg.get("benchmarks") or {}).items()), key=lambda x: -x[1]["score"])
    page = _ai_choice_page(cfg, engines)
    shown = {k: v for k, v in cfg.items() if k != "providers"}
    return render(request, "flows/ai_settings.html", {
        "section": "ai", "cfg": shown, "ollama_host": host,
        "ollama_ok": any(e["id"].startswith("ollama:") for e in engines),
        "stats": _labelled_stats(ai_actions.usage_stats(ws)), "today_calls": ai_actions.calls_today(ws),
        "embed_engines": _embed_choices(cfg), "health": ai_actions.health(ws), "bench": __import__("flows.ai_eval", fromlist=["status"]).status(ws),
        "bench_tasks": [t for t, _ in __import__("flows.ai_eval", fromlist=["TASKS"]).TASKS],
        "marks": marks,
        "recent": _usage_rows(ws),
        **page})


_USAGE_LABELS = {
    "describe": "Describe a workflow",
    "workflow_edit": "Builder assistant",
    "workflow_suggest": "Workflow suggestion",
    "workflow_explain": "Explain a workflow",
    "agent": "AI agent",
    "email_draft": "Write an email",
    "doc_search": "Ask your documents",
    "watchdog": "AI watchdog",
    "ops_digest": "Operations digest",
    "fill": "Fill fields",
    "fill_assist": "Fill from text",
    "summary": "Document summary",
    "review": "Review",
    "translate": "Translate",
    "ask": "Ask AI",
    "ask_all": "Ask across documents",
    "search": "AI search",
    "form_build": "Build a form",
    "form_edit": "Edit a form",
    "form_review": "Review a form",
    "form_ai": "Form assistant",
    "diagnose": "Diagnosis",
    "classify": "Classify",
}


def _usage_feature(kind):
    return _USAGE_LABELS.get(kind, (kind or "Call").replace("_", " ").capitalize())


def _usage_reply(detail):
    """Pretty-print a saved reply. A reply one bracket off is filled in so the structure can be read."""
    text = (detail or "").strip()
    if not text:
        return ""
    parsed = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            from ..ai_builder import extract_json
            parsed = extract_json(text)
        except ValueError:
            return text
    return json.dumps(parsed, indent=2, ensure_ascii=False)


def _embed_choices(cfg):
    with ai_providers.bind(cfg):
        return ai_providers.embedding_engines()


def _limit(raw):
    try:
        return max(0, min(int(raw or 0), 1_000_000))
    except (TypeError, ValueError):
        return 0


def _labelled_stats(stats):
    for k in stats.get("by_kind") or []:
        k["label"] = _usage_feature(k["kind"])
    return stats


def _usage_rows(ws):
    rows = list(ws.ai_usage.select_related("packet", "user")[:25])
    for row in rows:
        row.feature = _usage_feature(row.kind)
    return rows


@login_required
def ai_usage_panel(request, pk):
    """Slide-over detail for one usage row — the same drawer contacts use."""
    _manager(request)
    if (off := _gate(request, "ai")) is not None:
        return off
    row = get_object_or_404(AIUsage.objects.select_related("packet", "user"), workspace=request.workspace, pk=pk)
    return render(request, "flows/partials/ai_usage_panel.html", {
        "u": row, "feature": _usage_feature(row.kind), "reply": _usage_reply(row.detail)})


# ---- Ask AI about a document --------------------------------------------------------------------------------------
@login_required
@require_POST
def ask_document(request, pk):
    from django.http import JsonResponse

    from ..models import Comment

    packet = get_object_or_404(Packet.objects.visible_to(request.membership), pk=pk)
    if not packet.workspace.feature("ai"):
        return JsonResponse({"error": "AI is turned off by the platform administrator."}, status=403)
    if not ai_actions.settings_for(packet.workspace).get("ask_ai", True):
        return JsonResponse({"error": "Ask AI is turned off for this workspace."}, status=403)
    question = request.POST.get("question", "").strip()[:1000] or "Summarize this document."
    try:
        history = json.loads(request.POST.get("history") or "[]")
        history = [h for h in history if isinstance(h, dict)][-6:] if isinstance(history, list) else []
    except ValueError:
        history = []
    save = request.POST.get("save") == "1"

    def finish(answer):
        if save:
            Comment.objects.create(packet=packet, author=request.user, author_label=f"AI answer for {request.user.get_full_name() or request.user.email}",
                                   text=f"Q: {question}\n\n{answer}"[:4000])
        engine.audit(packet.workspace, "packet.ask_ai", question[:200], actor=request.user, packet=packet, request=request)

    if request.POST.get("stream") == "1":
        from django.http import StreamingHttpResponse

        def lines():  # one JSON object per line: {"t": piece} …, then {"done": true} or {"error": "…"}
            parts = []
            try:
                for piece in ai_actions.ask_stream(packet, question, history=history, user=request.user):
                    parts.append(piece)
                    yield json.dumps({"t": piece}) + "\n"
            except ai_actions.AIError as exc:
                yield json.dumps({"error": str(exc)}) + "\n"
                return
            finish("".join(parts).strip())
            yield json.dumps({"done": True}) + "\n"

        resp = StreamingHttpResponse(lines(), content_type="application/x-ndjson")
        resp["Cache-Control"] = "no-cache"
        resp["X-Accel-Buffering"] = "no"  # let nginx/Caddy pass pieces through as they come
        return resp
    try:
        answer = ai_actions.ask(packet, question, history=history, user=request.user)
    except ai_actions.AIError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    finish(answer)
    return JsonResponse({"answer": answer})


@login_required
def ai_benchmark_status(request):
    _manager(request)
    from django.http import JsonResponse

    from .. import ai_eval

    return JsonResponse(ai_eval.status(request.workspace))
