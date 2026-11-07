"""Workspace AI providers: Anthropic, NVIDIA, OpenAI, and custom OpenAI-compatible accounts."""
import base64
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase

from accounts.models import Workspace
from flows import ai_forms, ai_providers


JPEG = base64.b64encode(b"\xff\xd8\xff" + b"\x00" * 32).decode()


def _http_router(routes):
    def http(url, payload=None, headers=None, timeout=10):
        http.calls.append((url, payload, headers or {}))
        for needle, body in routes:
            if needle in url:
                if isinstance(body, Exception):
                    raise body
                return body
        raise AssertionError(url)
    http.calls = []
    return http


class ProviderCallTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_ollama_keeps_colons_in_the_model_name(self):
        http = _http_router([("/api/chat", {"message": {"content": "ok"}})])
        with mock.patch("flows.ai_providers._http", http):
            self.assertEqual(ai_providers.chat("ollama:qwen3.5:9b", "s", [{"role": "user", "content": "hi"}]), "ok")
        self.assertTrue(http.calls[0][0].endswith("/api/chat"))
        self.assertEqual(http.calls[0][1]["model"], "qwen3.5:9b")

    def test_claude_id_uses_the_workspace_anthropic_key(self):
        http = _http_router([("/v1/messages", {"content": [{"type": "text", "text": "ok"}]})])
        cfg = {"providers": [{"id": "anthropic", "kind": "anthropic", "api_key": "sk-ws"}]}
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-env"}):
            with ai_providers.bind(cfg), mock.patch("flows.ai_providers._http", http):
                ai_providers.chat("claude:claude-opus-5-5", "sys", [{"role": "user", "content": "hi"}])
        url, payload, headers = http.calls[0]
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(headers["x-api-key"], "sk-ws")
        self.assertEqual(payload["model"], "claude-opus-5-5")
        self.assertNotIn("sk-env", str(http.calls))

    def test_env_claude_when_the_workspace_has_no_anthropic_account(self):
        http = _http_router([
            ("/v1/messages", {"content": [{"type": "text", "text": "ok"}]}),
            ("/api/tags", ai_providers.EngineError("down")),
        ])
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-env", "PAPERLANE_AI_MODEL": "claude-from-env"}):
            with ai_providers.bind({"providers": []}), mock.patch("flows.ai_providers._http", http):
                ids = [e["id"] for e in ai_providers.engines(refresh=True)]
                ai_providers.chat("claude:claude-from-env", "s", [{"role": "user", "content": "hi"}])
        self.assertIn("claude:claude-from-env", ids)
        sent = [c for c in http.calls if c[0].endswith("/messages")]
        self.assertEqual(sent[0][2]["x-api-key"], "sk-env")

    def test_nvidia_openai_and_custom_routes(self):
        http = _http_router([
            ("/chat/completions", {"choices": [{"message": {"content": "ok"}}]}),
            ("/models", {"data": [{"id": "listed"}]}),
            ("/api/tags", {"models": []}),
        ])
        cfg = {"providers": [
            {"id": "nvidia", "kind": "nvidia", "api_key": "nv-key"},
            {"id": "openai", "kind": "openai", "api_key": "oa-key"},
            {"id": "groq", "kind": "compat", "label": "Groq", "base_url": "https://api.groq.com/openai/v1", "api_key": "g-key"},
        ]}
        with ai_providers.bind(cfg), mock.patch("flows.ai_providers._http", http):
            ids = [e["id"] for e in ai_providers.engines(refresh=True)]
            ai_providers.chat("nvidia:meta/llama-3.1-8b-instruct", "s", [{"role": "user", "content": "hi"}], json_mode=True)
            ai_providers.chat("openai:gpt-4o", "s", [{"role": "user", "content": "hi"}], json_mode=True)
            ai_providers.chat("compat:groq:llama-3.3-70b", "s", [{"role": "user", "content": "hi"}], json_mode=True)
        self.assertIn("nvidia:listed", ids)
        self.assertIn("openai:listed", ids)
        self.assertIn("compat:groq:listed", ids)
        chats = [c for c in http.calls if c[0].endswith("/chat/completions")]
        self.assertEqual(chats[0][0], "https://integrate.api.nvidia.com/v1/chat/completions")
        self.assertEqual(chats[0][2]["authorization"], "Bearer nv-key")
        self.assertEqual(chats[0][1]["model"], "meta/llama-3.1-8b-instruct")
        self.assertEqual(chats[0][1]["response_format"], {"type": "json_object"})  # JSON mode; dropped if refused
        self.assertEqual(chats[1][0], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(chats[1][2]["authorization"], "Bearer oa-key")
        self.assertEqual(chats[1][1]["response_format"], {"type": "json_object"})
        self.assertEqual(chats[2][0], "https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(chats[2][2]["authorization"], "Bearer g-key")
        self.assertEqual(chats[2][1]["model"], "llama-3.3-70b")
        self.assertEqual(chats[2][1]["response_format"], {"type": "json_object"})

    def test_openai_retries_without_json_mode_on_400(self):
        calls = []

        def http(url, payload=None, headers=None, timeout=10):
            calls.append(payload)
            if payload and "response_format" in payload:
                raise ai_providers.EngineError("HTTP 400: response_format is not supported", status=400)
            return {"choices": [{"message": {"content": "ok"}}]}

        cfg = {"providers": [{"id": "openai", "kind": "openai", "api_key": "oa-key"}]}
        with ai_providers.bind(cfg), mock.patch("flows.ai_providers._http", http):
            self.assertEqual(ai_providers.chat("openai:gpt-4o", "s", [{"role": "user", "content": "hi"}]), "ok")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("response_format", calls[1])

    def test_transient_errors_are_retried_then_raised(self):
        calls = []

        def http(url, payload=None, headers=None, timeout=10):
            calls.append(url)
            if len(calls) < 3:
                raise ai_providers.EngineError("HTTP 503: busy", status=503)
            return {"choices": [{"message": {"content": "ok"}}]}

        cfg = {"providers": [{"id": "openai", "kind": "openai", "api_key": "k"}]}
        with ai_providers.bind(cfg), mock.patch("flows.ai_providers._http", http), \
                mock.patch("flows.ai_providers._sleep") as slept:
            self.assertEqual(ai_providers.chat("openai:gpt-4o", "s", [{"role": "user", "content": "hi"}], json_mode=False), "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(slept.call_count, 2)

        def always(url, payload=None, headers=None, timeout=10):
            raise ai_providers.EngineError("HTTP 429: slow down", status=429)

        def unreachable(url, payload=None, headers=None, timeout=10):
            calls.append(url)
            raise ai_providers.EngineError("can't reach x (timed out)")

        with ai_providers.bind(cfg), mock.patch("flows.ai_providers._http", always), mock.patch("flows.ai_providers._sleep"):
            with self.assertRaises(ai_providers.EngineError):
                ai_providers.chat("openai:gpt-4o", "s", [{"role": "user", "content": "hi"}], json_mode=False)
        calls.clear()
        with ai_providers.bind(cfg), mock.patch("flows.ai_providers._http", unreachable), mock.patch("flows.ai_providers._sleep"):
            with self.assertRaises(ai_providers.EngineError):
                ai_providers.chat("openai:gpt-4o", "s", [{"role": "user", "content": "hi"}], json_mode=False)
        self.assertEqual(len(calls), 1)  # a timeout goes straight to the backup model instead

    def test_json_repair_fixes_bracket_slips(self):
        from flows.ai_builder import extract_json

        # real replies that failed: a closer missing at the end, and closers in the wrong order
        self.assertEqual(extract_json('{"edits": [{"op": "x", "bot": {"when": {"rules": [{"f": 1}]}}}]')["edits"][0]["op"], "x")
        self.assertEqual(extract_json('{"edits":[{"bot":{"when":{"rules":[{"f":1}]}}]}')["edits"][0]["bot"]["when"]["rules"],
                         [{"f": 1}])
        self.assertEqual(extract_json('{"a": 1}}}'), {"a": 1})
        self.assertEqual(extract_json('<think>{"no": 1}</think>```json\n{"a": [1, 2,], "b": "} in text",}\n```'),
                         {"a": [1, 2], "b": "} in text"})
        self.assertEqual(extract_json('Here you go: {"a": "cut off'), {"a": "cut off"})
        with self.assertRaises(ValueError):
            extract_json("no json at all")

    def test_images_are_rewritten_for_anthropic_and_nvidia(self):
        http = _http_router([
            ("api.anthropic.com", {"content": [{"type": "text", "text": "seen"}]}),
            ("integrate.api.nvidia.com", {"choices": [{"message": {"content": "seen"}}]}),
        ])
        cfg = {"providers": [
            {"id": "anthropic", "kind": "anthropic", "api_key": "sk-ws"},
            {"id": "nvidia", "kind": "nvidia", "api_key": "nv-key"},
        ]}
        messages = [{"role": "user", "content": "What is this?", "images": [JPEG]}]
        with ai_providers.bind(cfg), mock.patch("flows.ai_providers._http", http):
            ai_providers.chat("anthropic:claude-sonnet-4-5", "s", messages, json_mode=False)
            ai_providers.chat("nvidia:meta/llama-vision", "s", messages, json_mode=False)
        anth = http.calls[0][1]["messages"][0]["content"]
        self.assertEqual(anth[0]["type"], "image")
        self.assertEqual(anth[0]["source"]["media_type"], "image/jpeg")
        self.assertEqual(anth[0]["source"]["data"], JPEG)
        self.assertEqual(anth[1], {"type": "text", "text": "What is this?"})
        parts = http.calls[1][1]["messages"][1]["content"]
        self.assertEqual(parts[1]["type"], "image_url")
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_vision_engine_can_be_anthropic(self):
        ws = Workspace(settings={"ai": {"providers": [{"id": "anthropic", "kind": "anthropic", "api_key": "sk-ws"}]}})
        http = _http_router([
            ("api.anthropic.com", {"data": [{"id": "claude-sonnet-4-5", "type": "model"}]}),
            ("/api/tags", ai_providers.EngineError("down")),
        ])
        with mock.patch("flows.ai_providers._http", http):
            self.assertEqual(ai_forms.vision_engine(ws), "anthropic:claude-sonnet-4-5")


class ProviderSettingsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        cache.clear()
        self.ws = Workspace.objects.get(slug="demo")
        self.client.force_login(User.objects.get(username="owner"))

    def _page(self, data):
        http = _http_router([
            ("api.anthropic.com", {"data": [{"id": "claude-sonnet-4-5", "type": "model", "display_name": "Claude Sonnet"}]}),
            ("integrate.api.nvidia.com", {"data": [{"id": "meta/llama-3.1-8b-instruct"}]}),
            ("api.groq.com", {"data": [{"id": "llama-3.3-70b"}]}),
            ("api.openai.com", {"data": [{"id": "gpt-4o"}]}),
            ("/api/tags", {"models": []}),
        ])
        with mock.patch("flows.ai_providers._http", http):
            return self.client.post("/workspace/ai/", data, follow=True)

    def test_one_provider_masks_the_key_and_lists_only_its_models(self):
        secret = "sk-ant-workspace-secret"
        page = self._page({"form": "settings", "provider": "anthropic", "api_key": secret,
                           "engine": "anthropic:claude-sonnet-4-5"})
        self.assertNotContains(page, secret)
        self.assertContains(page, "Key saved")
        self.assertContains(page, "anthropic:claude-sonnet-4-5")
        self.assertNotContains(page, "nvidia:meta/llama-3.1-8b-instruct")
        self.ws.refresh_from_db()
        self.assertEqual(self.ws.settings["ai"]["providers"][0]["api_key"], secret)
        self.assertEqual(self.ws.settings["ai"]["provider"], "anthropic")

        page = self._page({"form": "settings", "provider": "anthropic", "api_key": "",
                           "engine": "anthropic:claude-sonnet-4-5"})
        self.assertNotContains(page, secret)
        self.ws.refresh_from_db()
        self.assertEqual(self.ws.settings["ai"]["providers"][0]["api_key"], secret)

        page = self._page({"form": "settings", "provider": "nvidia", "api_key": "nv-workspace-secret",
                           "engine": "nvidia:meta/llama-3.1-8b-instruct",
                           "backup_provider": "anthropic", "backup_engine": "anthropic:claude-sonnet-4-5"})
        self.assertNotContains(page, "nv-workspace-secret")
        self.assertContains(page, "nvidia:meta/llama-3.1-8b-instruct")
        self.assertContains(page, "anthropic:claude-sonnet-4-5")
        self.ws.refresh_from_db()
        ai = self.ws.settings["ai"]
        self.assertEqual(ai["engine"], "nvidia:meta/llama-3.1-8b-instruct")
        self.assertEqual(ai["backup_engine"], "anthropic:claude-sonnet-4-5")
        self.assertEqual(ai["providers"][0]["api_key"], secret)

        page = self._page({"form": "provider_save", "kind": "compat", "label": "Groq",
                           "base_url": "https://api.groq.com/openai/v1", "api_key": "groq-workspace-secret"})
        self.assertNotContains(page, "groq-workspace-secret")
        self.assertNotContains(page, "compat:groq:llama-3.3-70b")
        self.ws.refresh_from_db()
        self.assertEqual({p["id"] for p in self.ws.settings["ai"]["providers"]}, {"anthropic", "nvidia", "groq"})

        page = self._page({"form": "provider_remove", "provider_id": "nvidia"})
        self.assertNotContains(page, "nvidia:meta/llama-3.1-8b-instruct")
        self.assertContains(page, "anthropic:claude-sonnet-4-5")
        self.ws.refresh_from_db()
        self.assertEqual([p["id"] for p in self.ws.settings["ai"]["providers"]], ["anthropic", "groq"])
        self.assertEqual(self.ws.settings["ai"]["engine"], "")
        self.assertEqual(self.ws.settings["ai"]["provider"], "ollama")
        self.assertEqual(self.ws.settings["ai"]["backup_engine"], "anthropic:claude-sonnet-4-5")
        self.assertEqual(self.ws.settings["ai"]["providers"][0]["api_key"], secret)
