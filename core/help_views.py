"""Help center: articles (core/help_content.py), instant search and an AI chat grounded in the articles."""
import json
import re

from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from . import help_content as hc


def _layout(request):
    return "base.html" if request.user.is_authenticated and getattr(request, "membership", None) else "core/pages/_public_base.html"


def _ai_ok(request):
    ws = getattr(request, "workspace", None)
    if not (request.user.is_authenticated and ws and getattr(request, "membership", None)):
        return False
    from flows import ai_actions, ai_providers

    return ai_actions.enabled(ws, "help_ai") and (bool(ai_actions.settings_for(ws).get("engine"))
                                                  or ai_providers.default_engine() != ai_providers.PLANNER)


def _ctx(request, **extra):
    return {"layout": _layout(request), "categories": hc.CATEGORIES, "ai_ok": _ai_ok(request),
            "index": [{"t": a["title"], "s": a["summary"], "c": a["cat_title"], "u": a["url"], "k": a["keywords"]} for a in hc.A],
            **extra}


def home(request):
    return render(request, "core/help/home.html", _ctx(request, popular=[a for a in hc.A if a["popular"]]))


def category(request, cat):
    c = hc.CAT_BY_SLUG.get(cat)
    if c is None:
        raise Http404
    return redirect(c["articles"][0]["url"]) if c["articles"] else redirect("core:help")


def article(request, cat, slug):
    a = hc.get(cat, slug)
    if a is None:
        raise Http404
    siblings = hc.CAT_BY_SLUG[cat]["articles"]
    i = siblings.index(a)
    related = [r for r in hc.search(a["title"] + " " + a["keywords"], limit=5) if r is not a][:3]
    return render(request, "core/help/article.html", _ctx(
        request, a=a, cat=hc.CAT_BY_SLUG[cat], related=related,
        prev=siblings[i - 1] if i > 0 else None, next=siblings[i + 1] if i + 1 < len(siblings) else None))


def search_json(request):
    q = request.GET.get("q", "")[:200]
    return JsonResponse({"results": [{"title": a["title"], "summary": a["summary"], "url": a["url"], "cat": a["cat_title"]}
                                     for a in hc.search(q, limit=8)]})


HELP_SYSTEM = """You are the Paperlane help assistant inside the product. Paperlane automates document workflows
(steps, recipients, branches, bots, forms, AI features). Answer the user's question using ONLY the help articles below.
- Be practical and short: give numbered steps with the exact menu path in bold (e.g. **My workspace › AI & models**).
- If the articles don't cover it, say you're not sure and suggest asking a workspace owner — never invent menus or features.
- Plain text with **bold**, `code` and "-" or "1." lists only. No headings, no links (the sources are shown separately).
The user's role in this workspace: {role}. They are on page {path}.{doc}

HELP ARTICLES:
{articles}"""


DIAG_WORDS = re.compile(r"\b(fail\w*|error\w*|wrong|stuck|broken|problem\w*|issue\w*|why|debug\w*|diagnos\w*|happen\w*|"
                        r"not (?:moving|working|sent|received|arriving)|didn'?t|doesn'?t|won'?t|can'?t|ticket|bug|late|overdue|"
                        r"expired|missing)\b", re.I)


HOWTO = re.compile(r"^\s*(how\s+(do|can|to|should)|what\s+(is|does|are)|where|why|can\s+i|is\s+there|explain|help)\b", re.I)


def _is_data_question(question):
    from flows.ai_query import DATA_WORDS

    return bool(DATA_WORDS.search(question)) and not HOWTO.search(question)


def _document_for(request, question):
    """The document the user means: an explicit id, the page they're on, or “#123” in the question."""
    if not (request.user.is_authenticated and getattr(request, "membership", None)):
        return None
    from flows.models import Packet

    ref = request.POST.get("doc") or ""
    # what the user typed wins over the page they happen to be on
    m = re.search(r"(?:#|document\s+#?|doc\s+#?)(\d{1,7})\b", question, re.I) or re.search(r"/documents/(\d+)/", request.POST.get("path", ""))
    ref = ref or (m.group(1) if m else "")
    if not str(ref).isdigit():
        return None
    return Packet.objects.visible_to(request.membership).filter(pk=int(ref)).select_related("workflow").first()


def _diagnosis_reply(request, packet, question):
    from flows import diagnose as dg

    res = dg.run(packet, request.user, question)
    q = " ".join(f["title"] for f in res["findings"][:3]) or "troubleshooting"
    sources = [{"title": a["title"], "url": a["url"], "cat": a["cat_title"]} for a in hc.search(q + " troubleshooting problem", limit=3)]
    card = {k: res[k] for k in ("packet", "document", "workflow", "status", "severity", "severity_label", "impact", "ticket")}
    card["findings"] = [{k: f[k] for k in ("severity", "title", "detail", "fix", "link")} for f in res["findings"][:5]]
    card["url"] = f"/documents/{packet.pk}/"
    card["ticket_url"] = f"/documents/{packet.pk}/ticket/"
    return JsonResponse({"ai": res["ai"], "answer": res["explanation"], "diagnosis": card, "sources": sources})


@require_POST
def ask(request):
    """POST question (+ history JSON, path, doc) → {answer, sources, ai, diagnosis?}.

    On a document page (or with “#123” in the question), problem questions run the diagnosis: rules find what went
    wrong, the AI explains it, and high/critical problems file a support ticket. Otherwise the answer comes from the
    help articles. Without a model: the best-matching articles (and the rule-based diagnosis)."""
    question = request.POST.get("question", "").strip()[:1000]
    if not question:
        return JsonResponse({"error": "Ask a question."}, status=400)
    try:
        history = json.loads(request.POST.get("history") or "[]")
        history = [h for h in history if isinstance(h, dict)][-6:] if isinstance(history, list) else []
    except ValueError:
        history = []
    packet = _document_for(request, question)
    if packet is not None and (request.POST.get("mode") == "diagnose" or DIAG_WORDS.search(question)):
        return _diagnosis_reply(request, packet, question)
    if packet is None and _ai_ok(request) and _is_data_question(question):
        from flows import ai_actions, ai_query

        try:
            res = ai_query.answer(request.membership, question, user=request.user)
        except (ai_actions.AIError, ValueError):
            res = None
        if res:
            return JsonResponse({"ai": True, "answer": res["answer"], "query": res["query"], "sources": []})
    page = request.POST.get("page", "")[:200]
    context_q = " ".join([page] + [h.get("q", "") for h in history[-2:]] + [question])
    found = hc.search(context_q, limit=4) or hc.search(question, limit=4)
    sources = [{"title": a["title"], "url": a["url"], "cat": a["cat_title"]} for a in found]
    if not _ai_ok(request):
        return JsonResponse({"ai": False, "sources": sources,
                             "answer": "Here are the help articles that match your question." if sources else
                             "I couldn't find an article about that. Try other words, or browse the topics."})
    from flows import ai_actions

    articles = "\n\n".join(f"### {a['title']} ({a['cat_title']})\n{a['text'][:2200]}" for a in (found or hc.A[:3]))
    doc = (f" They are looking at document #{packet.pk} “{packet.name}” (workflow “{packet.workflow.name}”, "
           f"{packet.get_status_display()}) — if they report a problem with it, tell them to ask “what went wrong?”."
           if packet is not None else "")
    system = HELP_SYSTEM.format(role=request.membership.get_role_display(), articles=articles,
                                path=request.POST.get("path", "/help/")[:120] or "/help/", doc=doc)
    messages = []
    for h in history:
        if h.get("q") and h.get("a"):
            messages += [{"role": "user", "content": str(h["q"])[:1000]}, {"role": "assistant", "content": str(h["a"])[:3000]}]
    messages.append({"role": "user", "content": question})
    if request.POST.get("stream") == "1":
        from django.http import StreamingHttpResponse

        def lines():  # {"meta": {...}} first, then {"t": piece} …, then {"done": true} or {"error", "answer"}
            yield json.dumps({"meta": {"ai": True, "sources": sources}}) + "\n"
            try:
                engine = ai_actions.engine_for(request.workspace)
                for piece in ai_actions.chat_stream(request.workspace, engine, system, messages, kind="help", user=request.user):
                    yield json.dumps({"t": piece}) + "\n"
            except ai_actions.AIError as exc:
                yield json.dumps({"error": str(exc), "answer": "The AI assistant isn't reachable right now — these articles may help."}) + "\n"
                return
            yield json.dumps({"done": True}) + "\n"

        resp = StreamingHttpResponse(lines(), content_type="application/x-ndjson")
        resp["Cache-Control"] = "no-cache"
        resp["X-Accel-Buffering"] = "no"
        return resp
    try:
        engine = ai_actions.engine_for(request.workspace)
        answer = ai_actions.chat(request.workspace, engine, system, messages, json_mode=False, kind="help", user=request.user)
    except ai_actions.AIError as exc:
        return JsonResponse({"ai": False, "sources": sources, "error": str(exc),
                             "answer": "The AI assistant isn't reachable right now — these articles may help."})
    return JsonResponse({"ai": True, "answer": answer.strip(), "sources": sources})


def legacy(request):
    return redirect("core:help")


@require_POST
def feedback(request):
    """Store a “Was this helpful?” vote (one per article per browser session)."""
    from .models import HelpFeedback

    art = request.POST.get("article", "")[:120]
    if art.count("/") != 1 or hc.get(*art.split("/")) is None:
        return JsonResponse({"ok": False}, status=400)
    seen = request.session.setdefault("help_votes", [])
    if art not in seen:
        HelpFeedback.objects.create(article=art, helpful=request.POST.get("helpful") == "1",
                                    user=request.user if request.user.is_authenticated else None)
        request.session["help_votes"] = seen + [art]
    return JsonResponse({"ok": True})
