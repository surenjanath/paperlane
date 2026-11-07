"""Ask your documents: find documents by meaning and answer with citations.

Every document gets a search text (name, workflow, status, tags and readable field values — never sensitive fields,
signatures or files) in ``DocIndex``. With an embedding model (AI & models › Search model, or the first one found)
the text is also turned into a vector. A search ranks documents by keywords (always) blended with meaning (when
vectors exist), keeps only documents the person may see, and the workspace model answers from the best matches,
citing them as [#id]. The scheduler keeps the index fresh; a search also indexes stale documents it is about to rank.
"""
import hashlib
import json
import math
import re
from collections import Counter

from . import ai_actions, ai_providers

TOKEN = re.compile(r"[a-z0-9][a-z0-9'\-]*", re.I)
STOP = set("a an and are as at be by for from has have in is it its of on or that the this to was were will with "
           "what which who whom whose when where why how all any my me our your their them they show find list".split())
BATCH = 16


def _tokens(text):
    return [t for t in (w.lower().strip("'-") for w in TOKEN.findall(text or "")) if t and t not in STOP]


def doc_text(packet):
    from .ai_actions import DATA_CLOSE, DATA_OPEN

    facts = ai_actions.document_facts(packet, limit=6000).replace(DATA_OPEN, "").replace(DATA_CLOSE, "").strip()
    tags = ", ".join(t.name for t in packet.tags.all())
    return (f"{packet.name}\nWorkflow: {packet.workflow.name}\nStatus: {packet.get_status_display()}\n"
            f"Created: {packet.created_at:%Y-%m-%d}" + (f"\nTags: {tags}" if tags else "") + f"\n{facts}")


def embed_engine(workspace):
    """The search model chosen in AI & models, else the first embedding model available ("" when there is none)."""
    cfg = ai_actions.settings_for(workspace)
    if cfg.get("embed_engine") == "off" or not ai_actions.enabled(workspace):
        return ""
    with ai_providers.bind(cfg):
        found = [e["id"] for e in ai_providers.embedding_engines()]
    chosen = cfg.get("embed_engine") or ""
    return chosen if chosen in found else (found[0] if found and not chosen else "")


def _embed(workspace, engine, texts):
    with ai_providers.bind(ai_actions.settings_for(workspace)):
        return ai_providers.embed(engine, texts)


def refresh(workspace, packets=None, limit=200, with_vectors=True):
    """Bring the index up to date for ``packets`` (default: the workspace's documents). Returns how many changed.

    A changed text always replaces the old one (and drops its vector). With ``with_vectors`` the documents without a
    vector from the current search model also get one; without it only the keyword text is refreshed."""
    from .models import DocIndex, Packet

    qs = packets if packets is not None else Packet.objects.filter(workspace=workspace, trashed_at__isnull=True, is_test=False)
    qs = qs.select_related("workflow").prefetch_related("tags")
    have = {pk: (sig, model, has_vec) for pk, sig, model, has_vec in
            DocIndex.objects.filter(workspace=workspace).values_list("packet_id", "sig", "model", "vector__isnull")}
    engine = embed_engine(workspace) if with_vectors else ""
    texts, vec_todo, changed = {}, [], 0
    for p in qs.order_by("-last_activity_at")[:2000]:
        text = doc_text(p)
        sig = hashlib.sha1(text.encode()).hexdigest()
        old = have.get(p.pk)
        if not old or old[0] != sig:
            DocIndex.objects.update_or_create(packet=p, defaults={"workspace": workspace, "text": text, "sig": sig,
                                                                  "vector": None, "model": ""})
            changed += 1
            old = (sig, "", True)
        if engine and (old[2] or old[1] != engine):  # no vector yet, or one from another model
            vec_todo.append(p.pk)
            texts[p.pk] = text
        if changed >= limit and len(vec_todo) >= limit:
            break
    vec_todo = vec_todo[:limit]
    for i in range(0, len(vec_todo), BATCH):
        chunk = vec_todo[i:i + BATCH]
        try:
            vectors = _embed(workspace, engine, [texts[pk] for pk in chunk])
        except ai_providers.EngineError:
            break  # keyword search still works; try again on the next pass
        for pk, vec in zip(chunk, vectors):
            if vec:
                DocIndex.objects.filter(packet_id=pk).update(vector=vec, model=engine)
                changed += 1
    return changed


def refresh_all(limit=100):
    """Scheduler pass: a little work per workspace that uses AI."""
    from accounts.models import Workspace

    n = 0
    for ws in Workspace.objects.all():
        if ai_actions.enabled(ws):
            n += refresh(ws, limit=limit)
    return n


def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _snippet(text, terms):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    best = max(lines[3:] or lines, key=lambda ln: sum(t in ln.lower() for t in terms), default="")
    return best[:220]


def search(membership, query, limit=8, packets=None):
    """[{"packet", "score", "snippet", "why"}] best first, only documents ``membership`` may see."""
    from .models import DocIndex, Packet

    ws = membership.workspace
    visible = packets if packets is not None else Packet.objects.visible_to(membership).filter(trashed_at__isnull=True, is_test=False)
    refresh(ws, visible, limit=300, with_vectors=False)  # the keyword text is always current for what we rank
    rows = list(DocIndex.objects.filter(workspace=ws, packet__in=visible).select_related("packet", "packet__workflow"))
    if not rows:
        return []
    terms = _tokens(query)
    docs = [Counter(_tokens(r.text)) for r in rows]
    n, avg = len(rows), (sum(sum(d.values()) for d in docs) / len(rows)) or 1
    df = Counter(t for d in docs for t in set(d))
    lex = []
    for d in docs:  # BM25
        length, score = sum(d.values()) or 1, 0.0
        for t in terms:
            if d[t]:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                score += idf * d[t] * 2.2 / (d[t] + 1.2 * (0.25 + 0.75 * length / avg))
        lex.append(score)
    vec = [0.0] * len(rows)
    engine = embed_engine(ws)
    if engine and any(r.vector for r in rows):
        try:
            q = _embed(ws, engine, [query])[0]
            vec = [_cos(q, r.vector) if r.vector and r.model == engine else 0.0 for r in rows]
        except (ai_providers.EngineError, IndexError):
            pass
    top_lex, top_vec = max(lex) or 1, max(vec) or 1
    ranked = []
    for r, lx, vc in zip(rows, lex, vec):
        score = (0.55 * (vc / top_vec) + 0.45 * (lx / top_lex)) if any(vec) else lx / top_lex
        if score > 0.05 and (lx > 0 or vc > 0.35):
            ranked.append({"packet": r.packet, "score": round(score, 3), "snippet": _snippet(r.text, terms),
                           "why": "meaning and words" if vc > 0.35 and lx else ("meaning" if vc > 0.35 else "words")})
    ranked.sort(key=lambda x: (-x["score"], -x["packet"].pk))  # ties: newest first, the same on every database
    if ranked:  # keep the clear matches: a long tail of weak ones only adds noise
        cut = ranked[0]["score"] * 0.6
        ranked = [r for r in ranked if r["score"] >= cut]
    return ranked[:limit]


ANSWER_SYSTEM = ("You answer questions about a company's documents using only the numbered documents given. Cite every fact "
                 "with the document number in square brackets, e.g. [#123]. If the documents don't answer the question, "
                 "say so plainly. Be brief: a sentence or two, or a short list.")


def answer(membership, query, user=None):
    """{"answer", "hits", "ai"} — the AI answer is left out (ai False) when no model is available."""
    hits = search(membership, query)
    out = {"hits": hits, "answer": "", "ai": False}
    ws = membership.workspace
    if not hits or not ai_actions.enabled(ws):
        return out
    from .models import DocIndex

    texts = dict(DocIndex.objects.filter(packet__in=[h["packet"] for h in hits]).values_list("packet_id", "text"))
    context = "\n\n".join(f"[#{h['packet'].pk}]\n{texts.get(h['packet'].pk, '')[:1800]}" for h in hits[:6])
    try:
        engine = ai_actions.engine_for(ws)
        out["answer"] = ai_actions.chat(ws, engine, ANSWER_SYSTEM,
                                        f"Question: {query[:500]}\n\nDocuments:\n{ai_actions.fence(context)}",
                                        json_mode=False, kind="doc_search", user=user).strip()
        out["ai"] = True
    except ai_actions.AIError as exc:
        out["error"] = str(exc)
    return out


def cited(answer_text):
    return {int(x) for x in re.findall(r"\[#(\d+)\]", answer_text or "")}


def as_json(result):
    return json.dumps({"answer": result["answer"], "documents": [
        {"id": h["packet"].pk, "name": h["packet"].name, "score": h["score"]} for h in result["hits"]]})
