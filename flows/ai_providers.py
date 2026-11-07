"""Language-model engines. No SDKs — plain HTTP with urllib.

A workspace saves its own provider keys (see AI & models). Environment variables are a fallback when that
workspace has not saved the matching provider:

    ANTHROPIC_API_KEY            one Claude model; PAPERLANE_AI_MODEL picks it (default claude-opus-5-5)
    OLLAMA_HOST                  Ollama URL (default http://127.0.0.1:11434); its installed models are listed
    PAPERLANE_OLLAMA_MODELS       comma list to show instead of every installed model
    PAPERLANE_OPENAI_BASE_URL     OpenAI-compatible server, e.g. http://127.0.0.1:1234/v1 (LM Studio)
    PAPERLANE_OPENAI_API_KEY      its key, if it needs one
    PAPERLANE_AI_ENGINE           default engine id, e.g. "ollama:qwen3.5:9b" (otherwise Claude if configured, else the planner)
    PAPERLANE_AI_TIMEOUT          seconds to wait for a local or OpenAI-compatible model (default 300)

Engine ids: "planner", "ollama:<model>", "anthropic:<model>", "claude:<model>" (same route as anthropic),
"nvidia:<model>", "openai:<model>", "compat:<slug>:<model>".
"""
import contextlib
import contextvars
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request

from django.core.cache import cache

PLANNER = "planner"
DEFAULT_CLAUDE = "claude-opus-5-5"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODELS = "https://api.anthropic.com/v1/models?limit=100"
NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"
OPENAI_BASE = "https://api.openai.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
NAMED_KINDS = ("anthropic", "nvidia", "openai")
KIND_TITLES = {"anthropic": "Anthropic", "nvidia": "NVIDIA", "openai": "OpenAI"}
RESERVED_IDS = {"anthropic", "nvidia", "openai", "ollama", "claude", "planner", "compat"}
_VISION_ID = re.compile(r"vl|vision|llava|gpt-4o|gpt-4\.1|claude", re.I)


class EngineError(Exception):
    """``status`` is the HTTP status when the provider answered with an error, else 0 (unreachable / timed out)."""

    def __init__(self, message="", status=0, retry_after=0.0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


# Provider answers that usually succeed on a second try: rate limits, overload, and gateway hiccups.
TRANSIENT = {408, 409, 425, 429, 500, 502, 503, 504, 529}
RETRY_DELAYS = (1.5, 4.0)
_sleep = time.sleep  # tests patch this


_HOST = contextvars.ContextVar("paperlane_ollama_host", default="")
_PROVIDERS = contextvars.ContextVar("paperlane_ai_providers", default=None)


def use_host(host):
    """Use a workspace's own Ollama address for the following calls (returns a token for reset)."""
    return _HOST.set((host or "").strip().rstrip("/"))


def use_providers(providers):
    """Use a workspace's saved provider accounts (None keeps server env only)."""
    if providers is None:
        return _PROVIDERS.set(None)
    return _PROVIDERS.set([dict(p) for p in providers])


@contextlib.contextmanager
def bind(cfg):
    """Ollama host and saved providers for the duration of the call."""
    cfg = cfg or {}
    host_token = use_host(cfg.get("ollama_host", ""))
    prov_token = use_providers(cfg.get("providers") or [])
    try:
        yield
    finally:
        _HOST.reset(host_token)
        _PROVIDERS.reset(prov_token)


def _ollama_url():
    return _HOST.get() or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")


def _saved():
    return list(_PROVIDERS.get() or [])


def _named(kind):
    return next((p for p in _saved() if p.get("kind") == kind), None)


def _by_id(provider_id):
    return next((p for p in _saved() if p.get("id") == provider_id), None)


def _http(url, payload=None, headers=None, timeout=10):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"content-type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        try:
            wait = float(exc.headers.get("retry-after") or 0) if exc.headers else 0.0
        except ValueError:
            wait = 0.0
        raise EngineError(f"HTTP {exc.code}: {detail}", status=exc.code, retry_after=wait)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EngineError(f"can't reach {url.split('/')[2]} ({getattr(exc, 'reason', exc)})")


def _ollama_models():
    wanted = [m.strip() for m in os.environ.get("PAPERLANE_OLLAMA_MODELS", "").split(",") if m.strip()]
    try:
        data = _http(f"{_ollama_url()}/api/tags", timeout=1.5)
    except EngineError:
        return []
    out = []
    for m in data.get("models", []):
        caps = m.get("capabilities") or ["completion"]
        if "completion" not in caps or "embedding" in caps:
            continue
        if wanted and m["name"] not in wanted:
            continue
        size = (m.get("details") or {}).get("parameter_size") or ""
        if not size:
            found = re.search(r"[:\-_](\d+(?:\.\d+)?)b\b", m["name"], re.I)
            size = f"{found.group(1)}B" if found else ""
        out.append({"id": f"ollama:{m['name']}", "label": f"{m['name']}" + (f" · {size}" if size else ""), "group": "Ollama (local)",
                    "params": _params(size), "vision": "vision" in caps})
    return sorted(out, key=lambda e: (-e["params"], e["label"]))


def _params(size):
    try:
        return float(size.rstrip("BbMm")) / (1000 if size.upper().endswith("M") else 1)
    except ValueError:
        return 0.0


def _looks_vision(model_id, meta=None):
    modalities = []
    if isinstance(meta, dict):
        raw = meta.get("modalities") or meta.get("capabilities") or []
        modalities = list(raw) if isinstance(raw, (list, tuple)) else [raw]
    blob = " ".join(str(x).lower() for x in modalities)
    if "image" in blob or "vision" in blob:
        return True
    return bool(_VISION_ID.search(model_id or ""))


def _bearer(key):
    return {"authorization": f"Bearer {key}"} if key else {}


def _openai_env_models():
    """The server-wide OpenAI-compatible address, used when the workspace has not saved an OpenAI account."""
    if _named("openai"):
        return []
    base = os.environ.get("PAPERLANE_OPENAI_BASE_URL", "").rstrip("/")
    if not base:
        return []
    return _list_chat_models(base, os.environ.get("PAPERLANE_OPENAI_API_KEY", ""), "openai:", "OpenAI-compatible (local)")


def _list_chat_models(base, key, prefix, group):
    try:
        data = _http(f"{base.rstrip('/')}/models", headers=_bearer(key), timeout=1.5)
    except EngineError:
        return []
    out = []
    for m in data.get("data", []):
        mid = m.get("id")
        if not mid:
            continue
        out.append({"id": f"{prefix}{mid}", "label": mid, "group": group, "params": 0,
                    "vision": _looks_vision(mid, m)})
    return sorted(out, key=lambda e: e["label"].lower())


def _anthropic_headers(key):
    return {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}


def _anthropic_models(key):
    try:
        data = _http(ANTHROPIC_MODELS, headers=_anthropic_headers(key), timeout=1.5)
    except EngineError:
        return []
    out = []
    for m in data.get("data") or []:
        mid = m.get("id")
        if not mid or m.get("type") == "embedding":
            continue
        out.append({"id": f"anthropic:{mid}", "label": m.get("display_name") or mid, "group": "Anthropic",
                    "params": 0, "vision": True})
    return sorted(out, key=lambda e: e["label"].lower())


def _env_claude():
    """One Claude model from the server environment, when the workspace has not saved Anthropic."""
    if _named("anthropic") or not os.environ.get("ANTHROPIC_API_KEY"):
        return []
    model = os.environ.get("PAPERLANE_AI_MODEL", DEFAULT_CLAUDE)
    return [{"id": f"claude:{model}", "label": f"Claude ({model})", "group": "Anthropic", "params": 0, "vision": True}]


def _workspace_models():
    out = []
    anth = _named("anthropic")
    if anth and anth.get("api_key"):
        out += _anthropic_models(anth["api_key"])
    nv = _named("nvidia")
    if nv and nv.get("api_key"):
        out += _list_chat_models(NVIDIA_BASE, nv["api_key"], "nvidia:", "NVIDIA")
    oa = _named("openai")
    if oa and oa.get("api_key"):
        out += _list_chat_models(OPENAI_BASE, oa["api_key"], "openai:", "OpenAI")
    for p in _saved():
        if p.get("kind") != "compat":
            continue
        base = (p.get("base_url") or "").rstrip("/")
        slug = p.get("id") or ""
        if not base or not slug:
            continue
        out += _list_chat_models(base, p.get("api_key") or "", f"compat:{slug}:", p.get("label") or slug)
    return out


def _cache_key():
    blob = json.dumps({
        "host": _ollama_url(),
        "providers": [{"id": p.get("id"), "kind": p.get("kind"), "base": p.get("base_url") or "",
                       "key": hashlib.sha256((p.get("api_key") or "").encode()).hexdigest()[:16]}
                      for p in _saved()],
        "env_anth": hashlib.sha256(os.environ.get("ANTHROPIC_API_KEY", "").encode()).hexdigest()[:16],
        "env_model": os.environ.get("PAPERLANE_AI_MODEL", ""),
        "env_oai": os.environ.get("PAPERLANE_OPENAI_BASE_URL", ""),
        "env_oai_key": hashlib.sha256(os.environ.get("PAPERLANE_OPENAI_API_KEY", "").encode()).hexdigest()[:16],
    }, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def engines(refresh=False):
    """Every engine that can be used right now (remote lists are probed, cached for 30 s)."""
    ck = "ai-engines:" + _cache_key()
    if not refresh:
        cached = cache.get(ck)
        if cached is not None:
            return cached
    out = [{"id": PLANNER, "label": "Built-in planner (instant, offline)", "group": "Built in", "params": 0}]
    out += _env_claude()
    out += _ollama_models()
    out += _workspace_models()
    out += _openai_env_models()
    cache.set(ck, out, 30)
    return out


def default_engine():
    ids = [e["id"] for e in engines()]
    env = os.environ.get("PAPERLANE_AI_ENGINE")
    if env in ids:
        return env
    return next((i for i in ids if i.startswith(("claude:", "anthropic:"))), PLANNER)


def is_available(engine_id):
    return engine_id in {e["id"] for e in engines()}


def label(engine_id):
    return next((e["label"] for e in engines() if e["id"] == engine_id), engine_id)


def split_engine(engine_id):
    """kind, slug, model. Model text keeps any colons it contains."""
    kind, _, rest = (engine_id or "").partition(":")
    if kind == "claude":
        return "anthropic", "", rest
    if kind == "compat":
        slug, _, model = rest.partition(":")
        return "compat", slug, model
    return kind, "", rest


def provider_key(engine_id):
    """The provider a model id belongs to: ollama, anthropic, nvidia, openai, or compat:<slug>."""
    kind, slug, _model = split_engine(engine_id)
    if kind == "compat" and slug:
        return f"compat:{slug}"
    if kind in ("ollama", "anthropic", "nvidia", "openai"):
        return kind
    return ""


def provider_kind(key):
    key = key or ""
    return "compat" if key == "compat" or key.startswith("compat:") else key


def matches_provider(engine_id, key):
    """True when this model id is one the given provider choice lists."""
    if not engine_id or not key or engine_id == PLANNER:
        return False
    if key == "compat":
        return str(engine_id).startswith("compat:")
    return provider_key(engine_id) == key


def active_provider(cfg):
    """The saved provider choice, or the one implied by the saved model. Ollama when nothing is chosen yet."""
    cfg = cfg or {}
    stored = (cfg.get("provider") or "").strip()
    if stored in ("ollama", "anthropic", "nvidia", "openai") or stored == "compat" or stored.startswith("compat:"):
        return stored
    return provider_key(cfg.get("engine") or "") or "ollama"


def keep_engine(posted, previous, provider_value):
    """The posted model when it belongs to this provider, otherwise the previous one, otherwise none."""
    for candidate in (posted, previous):
        if matches_provider(candidate, provider_value):
            return candidate
    return ""


def _media_type(b64):
    import base64

    try:
        raw = base64.b64decode(b64[:64])[:16]
    except (ValueError, TypeError):
        return "image/jpeg"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG"):
        return "image/png"
    if raw.startswith(b"GIF8"):
        return "image/gif"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _text(message):
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _ollama_messages(system, messages):
    out = [{"role": "system", "content": system}]
    for m in messages:
        msg = {"role": m.get("role") or "user", "content": _text(m)}
        if m.get("images"):
            msg["images"] = list(m["images"])
        out.append(msg)
    return out


def _openai_messages(system, messages):
    out = [{"role": "system", "content": system}]
    for m in messages:
        images = m.get("images") or []
        role = m.get("role") or "user"
        if not images:
            out.append({"role": role, "content": _text(m)})
            continue
        parts = [{"type": "text", "text": _text(m)}]
        for img in images:
            mt = _media_type(img)
            parts.append({"type": "image_url", "image_url": {"url": f"data:{mt};base64,{img}"}})
        out.append({"role": role, "content": parts})
    return out


def _anthropic_messages(messages):
    out = []
    for m in messages:
        images = m.get("images") or []
        role = m.get("role") or "user"
        if not images:
            out.append({"role": role, "content": _text(m)})
            continue
        blocks = [{"type": "image", "source": {"type": "base64", "media_type": _media_type(img), "data": img}}
                  for img in images]
        text = _text(m)
        if text:
            blocks.append({"type": "text", "text": text})
        out.append({"role": role, "content": blocks})
    return out


def _anthropic_key():
    saved = _named("anthropic")
    if saved and saved.get("api_key"):
        return saved["api_key"]
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise EngineError("Add an Anthropic key in My workspace › AI & models")
    return key


def _openai_target():
    """Official OpenAI when the workspace saved a key; otherwise the server's OpenAI-compatible address."""
    saved = _named("openai")
    if saved and saved.get("api_key"):
        return OPENAI_BASE, saved["api_key"]
    base = os.environ.get("PAPERLANE_OPENAI_BASE_URL", "").rstrip("/")
    if not base:
        raise EngineError("Add an OpenAI key in My workspace › AI & models")
    return base, os.environ.get("PAPERLANE_OPENAI_API_KEY") or ""


def _json_rejected(exc):
    """The server refused JSON mode (OpenAI-compatible servers differ in how they say so)."""
    return getattr(exc, "status", 0) in (400, 415, 422) or "response_format" in str(exc).lower()


def _chat_openai_family(base, key, model, system, messages, json_mode, timeout, json_format):
    if not base:
        raise EngineError("This provider has no address")
    if not model:
        raise EngineError("This provider has no model")
    payload = {"model": model, "temperature": 0.2, "messages": _openai_messages(system, messages)}
    if json_mode and json_format:
        payload["response_format"] = {"type": "json_object"}
    headers = _bearer(key)
    try:
        data = _http(f"{base.rstrip('/')}/chat/completions", payload, headers=headers, timeout=timeout)
    except EngineError as exc:
        if not _json_rejected(exc):
            raise
        payload.pop("response_format", None)
        data = _http(f"{base.rstrip('/')}/chat/completions", payload, headers=headers, timeout=timeout)
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")


def chat(engine_id, system, messages, json_mode=True):
    """Send a conversation and return the reply text, retrying twice when the provider reports a passing problem.

    Timeouts and unreachable hosts are not retried here: the wait was already long, and the caller's backup model
    (AI & models › Backup) is the better next step."""
    for attempt, delay in enumerate((*RETRY_DELAYS, None)):
        try:
            return _chat_raw(engine_id, system, messages, json_mode)
        except EngineError as exc:
            if delay is None or exc.status not in TRANSIENT:
                raise
            _sleep(min(max(delay, exc.retry_after or 0), 15))


def _chat_raw(engine_id, system, messages, json_mode=True):
    """One request to the provider. messages: [{"role": "user"|"assistant", "content": str}].

    A message may also carry "images": [raw base64]. Each provider gets that in its own shape.
    """
    kind, slug, model = split_engine(engine_id)
    timeout = int(os.environ.get("PAPERLANE_AI_TIMEOUT", "300"))
    if kind == "anthropic":
        data = _http(ANTHROPIC_URL, {
            "model": model, "max_tokens": 8000, "system": system, "messages": _anthropic_messages(messages),
        }, headers=_anthropic_headers(_anthropic_key()), timeout=180)
        return "".join(p.get("text", "") for p in data.get("content", []) if p.get("type") == "text")
    if kind == "ollama":
        data = _http(f"{_ollama_url()}/api/chat", {
            "model": model, "stream": False, **({"format": "json"} if json_mode else {}), "think": False,
            "messages": _ollama_messages(system, messages),
            "options": {"temperature": 0.2, "num_ctx": 16384},
        }, timeout=timeout)
        return (data.get("message") or {}).get("content", "")
    if kind == "nvidia":
        saved = _named("nvidia")
        if not saved or not saved.get("api_key"):
            raise EngineError("Add an NVIDIA key in My workspace › AI & models")
        return _chat_openai_family(NVIDIA_BASE, saved["api_key"], model, system, messages, json_mode, timeout, True)
    if kind == "openai":
        base, key = _openai_target()
        return _chat_openai_family(base, key, model, system, messages, json_mode, timeout, True)
    if kind == "compat":
        saved = _by_id(slug)
        if not saved or saved.get("kind") != "compat":
            raise EngineError("That custom provider is not saved on this workspace")
        return _chat_openai_family(saved.get("base_url") or "", saved.get("api_key") or "", model, system, messages,
                                   json_mode, timeout, True)
    raise EngineError(f"unknown engine {engine_id}")


# ---- embeddings (search by meaning) ---------------------------------------------------------------------------
_EMBED_ID = re.compile(r"embed|bge|e5-|gte-|minilm|nomic", re.I)


def embedding_engines():
    """Models that turn text into vectors: Ollama models with the "embedding" capability, and models named like
    embedding models on OpenAI-compatible servers. Anthropic has no embedding API."""
    out = []
    try:
        data = _http(f"{_ollama_url()}/api/tags", timeout=1.5)
        for m in data.get("models", []):
            if "embedding" in (m.get("capabilities") or []) or _EMBED_ID.search(m["name"]):
                out.append({"id": f"ollama:{m['name']}", "label": f"{m['name']} · Ollama"})
    except EngineError:
        pass
    for e in _workspace_models() + _openai_env_models():
        if _EMBED_ID.search(e["id"]):
            out.append({"id": e["id"], "label": e["label"]})
    return out


def embed(engine_id, texts):
    """Vectors for ``texts`` (list of lists of floats), in order."""
    kind, slug, model = split_engine(engine_id)
    texts = [str(t or " ")[:8000] for t in texts]
    if kind == "ollama":
        data = _http(f"{_ollama_url()}/api/embed", {"model": model, "input": texts}, timeout=120)
        return data.get("embeddings") or []
    if kind == "nvidia":
        base, key = NVIDIA_BASE, (_named("nvidia") or {}).get("api_key") or ""
    elif kind == "openai":
        base, key = _openai_target()
    elif kind == "compat":
        saved = _by_id(slug) or {}
        base, key = saved.get("base_url") or "", saved.get("api_key") or ""
    else:
        raise EngineError(f"{engine_id} can't make embeddings")
    data = _http(f"{base.rstrip('/')}/embeddings", {"model": model, "input": texts}, headers=_bearer(key), timeout=120)
    return [row.get("embedding") or [] for row in sorted(data.get("data") or [], key=lambda r: r.get("index", 0))]


# ---- streaming ------------------------------------------------------------------------------------------------
def _open_stream(url, payload, headers=None, timeout=300):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"content-type": "application/json", **(headers or {})})
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise EngineError(f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}", status=exc.code)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EngineError(f"can't reach {url.split('/')[2]} ({getattr(exc, 'reason', exc)})")


def _sse(resp):
    """The JSON payloads of a server-sent-events stream."""
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except ValueError:
            continue


def stream(engine_id, system, messages):
    """Yield the reply text in pieces as the model writes it (plain text, no JSON mode).

    Raises EngineError before the first piece when the provider can't be reached, so the caller can fall back."""
    kind, slug, model = split_engine(engine_id)
    timeout = int(os.environ.get("PAPERLANE_AI_TIMEOUT", "300"))
    if kind == "ollama":
        resp = _open_stream(f"{_ollama_url()}/api/chat", {
            "model": model, "stream": True, "think": False, "messages": _ollama_messages(system, messages),
            "options": {"temperature": 0.2, "num_ctx": 16384}}, timeout=timeout)
        with resp:
            for raw in resp:
                try:
                    part = json.loads(raw)
                except ValueError:
                    continue
                text = (part.get("message") or {}).get("content") or ""
                if text:
                    yield text
                if part.get("done"):
                    return
        return
    if kind == "anthropic":
        resp = _open_stream(ANTHROPIC_URL, {"model": model, "max_tokens": 4000, "system": system, "stream": True,
                                            "messages": _anthropic_messages(messages)},
                            headers=_anthropic_headers(_anthropic_key()), timeout=180)
        with resp:
            for ev in _sse(resp):
                if ev.get("type") == "content_block_delta":
                    text = (ev.get("delta") or {}).get("text") or ""
                    if text:
                        yield text
                elif ev.get("type") == "error":
                    raise EngineError(str((ev.get("error") or {}).get("message") or "stream error"))
        return
    if kind == "nvidia":
        saved = _named("nvidia") or {}
        base, key = NVIDIA_BASE, saved.get("api_key") or ""
    elif kind == "openai":
        base, key = _openai_target()
    elif kind == "compat":
        saved = _by_id(slug) or {}
        base, key = saved.get("base_url") or "", saved.get("api_key") or ""
    else:
        raise EngineError(f"unknown engine {engine_id}")
    if not base or not model:
        raise EngineError("This provider has no address or model")
    resp = _open_stream(f"{base.rstrip('/')}/chat/completions", {
        "model": model, "temperature": 0.2, "stream": True, "messages": _openai_messages(system, messages)},
        headers=_bearer(key), timeout=timeout)
    with resp:
        for ev in _sse(resp):
            text = (((ev.get("choices") or [{}])[0].get("delta") or {}).get("content")) or ""
            if text:
                yield text


def _slug(label):
    slug = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")[:40]
    return slug or "custom"


def _unique_slug(base, providers):
    taken = {p.get("id") for p in providers}
    slug, n = base, 2
    while slug in taken or slug in RESERVED_IDS:
        slug = f"{base}-{n}"
        n += 1
    return slug


def _base_url(url):
    url = (url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise EngineError("The address must start with http:// or https://")
    return url


def save_provider(providers, *, kind, api_key="", label="", base_url="", provider_id=""):
    """Insert or replace one account. A blank key keeps the key already saved for that account."""
    providers = [dict(p) for p in (providers or [])]
    kind = (kind or "").strip()
    if kind not in (*NAMED_KINDS, "compat"):
        raise EngineError("Choose Anthropic, NVIDIA, OpenAI, or a custom endpoint.")
    api_key = (api_key or "").strip()
    provider_id = (provider_id or "").strip()
    if kind == "compat":
        existing = next((p for p in providers if provider_id and p.get("id") == provider_id and p.get("kind") == "compat"), None)
        if provider_id and existing is None:
            raise EngineError("That provider is no longer saved.")
        label = (label or "").strip() or (existing or {}).get("label") or ""
        if not label:
            raise EngineError("Name the custom provider.")
        base_url = _base_url(base_url or (existing or {}).get("base_url") or "")
        if not api_key and existing:
            api_key = existing.get("api_key") or ""
        if existing:
            existing.update(label=label, base_url=base_url, api_key=api_key)
            return providers
        providers.append({"id": _unique_slug(_slug(label), providers), "kind": "compat", "label": label,
                          "base_url": base_url, "api_key": api_key})
        return providers
    existing = next((p for p in providers if p.get("kind") == kind), None)
    if not api_key:
        if existing and existing.get("api_key"):
            api_key = existing["api_key"]
        else:
            raise EngineError("Enter an API key.")
    rec = {"id": kind, "kind": kind, "api_key": api_key}
    if existing:
        existing.clear()
        existing.update(rec)
    else:
        providers.append(rec)
    return providers


def remove_provider(providers, provider_id):
    provider_id = (provider_id or "").strip()
    removed = next((p for p in (providers or []) if p.get("id") == provider_id), None)
    kept = [p for p in (providers or []) if p.get("id") != provider_id]
    return kept, removed


def engine_uses(engine_id, provider):
    """True when this engine id belongs to that saved account."""
    if not engine_id or not provider:
        return False
    kind = provider.get("kind")
    if kind == "compat":
        return engine_id.startswith(f"compat:{provider.get('id')}:")
    if kind == "anthropic":
        return engine_id.startswith("anthropic:")
    return bool(kind) and engine_id.startswith(f"{kind}:")


def provider_rows(providers, engines):
    """Settings-page rows. The API key is never included."""
    ids = [e["id"] for e in engines if e.get("id")]
    rows = []
    for p in providers or []:
        kind = p.get("kind")
        if kind == "compat":
            prefix, title = f"compat:{p.get('id')}:", p.get("label") or p.get("id")
        elif kind in KIND_TITLES:
            prefix, title = f"{kind}:", KIND_TITLES[kind]
        else:
            continue
        rows.append({"id": p.get("id"), "kind": kind, "title": title, "base_url": p.get("base_url") or "",
                     "ok": any(i.startswith(prefix) for i in ids), "has_key": bool(p.get("api_key"))})
    return rows
