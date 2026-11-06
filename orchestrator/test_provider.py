#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenAI 兼容 provider 的单元测试（不需要真实 key 与网络）。

    python3 -m unittest orchestrator.test_provider -v
"""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.providers.base import ProviderError, get_provider  # noqa: E402
from orchestrator.providers.openai_compat import OpenAICompatProvider  # noqa: E402


def resp(payload: dict):
    class _R:
        def __init__(self):
            self.body = json.dumps(payload).encode("utf-8")

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _R()


def http_error(code: int, message: str = '{"error":{"message":"bad"}}'):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(message.encode()))


OK = {"choices": [{"message": {"content": "写好的正文"}, "finish_reason": "stop"}]}


def provider(**kw):
    kw.setdefault("api_key", "test-key")
    kw.setdefault("base_url", "http://127.0.0.1:9")
    kw.setdefault("backoff", 0)
    return OpenAICompatProvider(**kw)


class TestHappyPath(unittest.TestCase):
    def test_returns_content(self):
        with patch("urllib.request.urlopen", return_value=resp(OK)) as m:
            out = provider().complete(task="writer.draft", system="s", prompt="p")
        self.assertEqual(out, "写好的正文")
        self.assertEqual(m.call_count, 1)

    def test_request_shape(self):
        with patch("urllib.request.urlopen", return_value=resp(OK)) as m:
            provider(model="explicit-model").complete(
                task="t", system="sys", prompt="usr", max_tokens=100, temperature=0.2)
        req = m.call_args.args[0]
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["model"], "explicit-model")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual(body["messages"][1], {"role": "user", "content": "usr"})
        self.assertEqual(body["max_tokens"], 100)
        self.assertEqual(req.headers.get("Authorization"), "Bearer test-key")
        self.assertTrue(req.full_url.endswith("/chat/completions"))


class TestErrors(unittest.TestCase):
    def test_missing_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ProviderError) as ctx:
                provider(api_key="").complete(task="t", system="s", prompt="p")
        self.assertIn("GZH_LLM_API_KEY", str(ctx.exception))

    def test_401_not_retried(self):
        with patch("urllib.request.urlopen", side_effect=http_error(401)) as m:
            with self.assertRaises(ProviderError):
                provider(max_retries=2).complete(task="t", system="s", prompt="p")
        self.assertEqual(m.call_count, 1, "401 不应重试")

    def test_400_not_retried(self):
        with patch("urllib.request.urlopen", side_effect=http_error(400)) as m:
            with self.assertRaises(ProviderError):
                provider(max_retries=2).complete(task="t", system="s", prompt="p")
        self.assertEqual(m.call_count, 1)

    def test_500_retried_then_ok(self):
        with patch("urllib.request.urlopen",
                   side_effect=[http_error(500), resp(OK)]) as m:
            out = provider(max_retries=2).complete(task="t", system="s", prompt="p")
        self.assertEqual(out, "写好的正文")
        self.assertEqual(m.call_count, 2)

    def test_500_exhausts(self):
        with patch("urllib.request.urlopen", side_effect=http_error(503)) as m:
            with self.assertRaises(ProviderError):
                provider(max_retries=1).complete(task="t", system="s", prompt="p")
        self.assertEqual(m.call_count, 2, "1 次首发 + 1 次重试")

    def test_network_error_retried(self):
        with patch("urllib.request.urlopen",
                   side_effect=[urllib.error.URLError("boom"), resp(OK)]):
            out = provider(max_retries=1).complete(task="t", system="s", prompt="p")
        self.assertEqual(out, "写好的正文")

    def test_empty_choices(self):
        with patch("urllib.request.urlopen", return_value=resp({"choices": []})):
            with self.assertRaises(ProviderError) as ctx:
                provider().complete(task="t", system="s", prompt="p")
        self.assertIn("choices", str(ctx.exception))

    def test_empty_content(self):
        payload = {"choices": [{"message": {"content": "   "}, "finish_reason": "length"}]}
        with patch("urllib.request.urlopen", return_value=resp(payload)):
            with self.assertRaises(ProviderError) as ctx:
                provider().complete(task="t", system="s", prompt="p")
        self.assertIn("空内容", str(ctx.exception))

    def test_reasoning_eats_budget(self):
        payload = {"choices": [{"message": {"content": "", "reasoning_content": "想了很久"},
                                "finish_reason": "length"}]}
        with patch("urllib.request.urlopen", return_value=resp(payload)):
            with self.assertRaises(ProviderError) as ctx:
                provider().complete(task="t", system="s", prompt="p")
        self.assertIn("max_tokens", str(ctx.exception))

    def test_non_json(self):
        class _R:
            def read(self):
                return b"<html>502 Bad Gateway</html>"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch("urllib.request.urlopen", return_value=_R()):
            with self.assertRaises(ProviderError) as ctx:
                provider().complete(task="t", system="s", prompt="p")
        self.assertIn("不是 JSON", str(ctx.exception))


class TestFactory(unittest.TestCase):
    def test_aliases(self):
        for name in ("deepseek", "kimi", "qwen", "openai", "openai-compat"):
            self.assertIsInstance(get_provider(name, api_key="k"), OpenAICompatProvider, name)

    def test_stub_ignores_extra(self):
        self.assertEqual(get_provider("stub", model="x").name, "stub")

    def test_unknown(self):
        with self.assertRaises(ProviderError):
            get_provider("nope")

    def test_presets_per_vendor(self):
        cases = {
            "deepseek": ("https://api.deepseek.com/v1", "deepseek-flash"),
            "kimi": ("https://api.moonshot.cn/v1", "moonshot-v1-8k"),
            "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
            "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
        }
        for name, (base, model) in cases.items():
            p = get_provider(name, api_key="k")
            self.assertEqual(p.base_url, base, name)
            self.assertEqual(p.model, model, name)

    def test_explicit_overrides_preset(self):
        with patch.dict(os.environ, {}, clear=True):
            p = get_provider("kimi", api_key="k", model="custom", base_url="http://x/v1")
        self.assertEqual(p.model, "custom")
        self.assertEqual(p.base_url, "http://x/v1")

    def test_env_overrides_preset(self):
        with patch.dict(os.environ, {"GZH_LLM_MODEL": "env-model"}, clear=False):
            p = get_provider("kimi", api_key="k")
        self.assertEqual(p.model, "env-model")
        self.assertEqual(p.base_url, "https://api.moonshot.cn/v1")

    def test_env_defaults(self):
        with patch.dict(os.environ, {"GZH_LLM_BASE_URL": "http://env/v1",
                                     "GZH_LLM_MODEL": "env-model",
                                     "GZH_LLM_API_KEY": "env-key"}):
            p = get_provider("deepseek")
        self.assertEqual(p.base_url, "http://env/v1")
        self.assertEqual(p.model, "env-model")
        self.assertEqual(p.api_key, "env-key")


if __name__ == "__main__":
    unittest.main(verbosity=2)
