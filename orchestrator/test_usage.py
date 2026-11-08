#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成本记账（T5）单元测试：provider usage 解析 + status 今日/本周汇总。

    python3 -m unittest orchestrator.test_usage -v
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import loop  # noqa: E402
from orchestrator.providers.openai_compat import OpenAICompatProvider  # noqa: E402
from orchestrator.providers.stub import StubProvider  # noqa: E402
from orchestrator.state import Run, now  # noqa: E402

ZERO_USAGE = {"input": 0, "output": 0, "total": 0, "calls": 0}


def fake_response(payload: dict):
    class _Resp:
        def __init__(self):
            self.body = json.dumps(payload).encode("utf-8")

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp()


def payload(content: str = "正文", usage: dict | None = None) -> dict:
    data = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
    if usage is not None:
        data["usage"] = usage
    return data


def make_provider(**kw) -> OpenAICompatProvider:
    kw.setdefault("api_key", "test-key")
    kw.setdefault("base_url", "http://127.0.0.1:9")
    kw.setdefault("max_retries", 0)
    return OpenAICompatProvider(**kw)


def call(p: OpenAICompatProvider, response: dict) -> str:
    with patch("urllib.request.urlopen", return_value=fake_response(response)):
        return p.complete(task="t", system="s", prompt="p")


class TestProviderUsage(unittest.TestCase):
    def test_usage_parsed(self):
        p = make_provider()
        call(p, payload(usage={"prompt_tokens": 100, "completion_tokens": 20,
                               "total_tokens": 120}))
        self.assertEqual(p.last_usage, {"input": 100, "output": 20, "total": 120})
        self.assertEqual(p.usage_total,
                         {"input": 100, "output": 20, "total": 120, "calls": 1})

    def test_usage_accumulates_across_calls(self):
        p = make_provider()
        call(p, payload(usage={"prompt_tokens": 100, "completion_tokens": 20,
                               "total_tokens": 120}))
        call(p, payload(usage={"prompt_tokens": 200, "completion_tokens": 30,
                               "total_tokens": 230}))
        self.assertEqual(p.last_usage, {"input": 200, "output": 30, "total": 230})
        self.assertEqual(p.usage_total,
                         {"input": 300, "output": 50, "total": 350, "calls": 2})

    def test_usage_total_falls_back_to_input_output(self):
        p = make_provider()
        call(p, payload(usage={"prompt_tokens": 7, "completion_tokens": 3}))
        self.assertEqual(p.last_usage, {"input": 7, "output": 3, "total": 10})

    def test_missing_usage_marks_none(self):
        p = make_provider()
        call(p, payload())     # 响应里没有 usage
        self.assertEqual(p.last_usage, {"input": None, "output": None, "total": None})
        self.assertEqual(p.usage_total,
                         {"input": 0, "output": 0, "total": 0, "calls": 1})

    def test_partial_usage_marks_missing_fields(self):
        p = make_provider()
        call(p, payload(usage={"prompt_tokens": 5}))
        self.assertEqual(p.last_usage, {"input": 5, "output": None, "total": 5})
        self.assertEqual(p.usage_total["total"], 5)

    def test_failed_call_does_not_count(self):
        p = make_provider(max_retries=0)
        bad = {"choices": [{"message": {"content": "  "}, "finish_reason": "length"}]}
        with patch("urllib.request.urlopen", return_value=fake_response(bad)):
            with self.assertRaises(Exception):
                p.complete(task="t", system="s", prompt="p")
        self.assertEqual(p.usage_total, ZERO_USAGE)

    def test_stub_provider_zero_usage(self):
        stub = StubProvider()
        self.assertIsNone(stub.last_usage)
        self.assertEqual(stub.usage_total, ZERO_USAGE)


class TestUsageSummary(unittest.TestCase):
    """status 的今日/本周汇总（按 run 最近一次 history 时间归日）。"""

    TODAY = date(2026, 11, 12)         # 周四（本周一 = 11-09）

    def _run(self, rid: str, usage: dict, at: str) -> Run:
        return Run(run_id=rid, usage=usage, history=[{"at": at, "to": "drafted"}])

    def test_today_and_week(self):
        runs = [
            self._run("a", {"input": 100, "output": 20, "total": 120, "calls": 1},
                      "2026-11-12T12:00:00+00:00"),
            self._run("b", {"input": 50, "output": 10, "total": 60, "calls": 1},
                      "2026-11-09T12:00:00+00:00"),      # 本周四，不是今天
            self._run("c", {"input": 1, "output": 1, "total": 2, "calls": 1},
                      "2026-10-01T12:00:00+00:00"),      # 上周
        ]
        summary = loop.usage_summary(runs, today=self.TODAY)
        today = summary["today"]
        self.assertEqual(today["runs"], 1)
        self.assertEqual(today["reported"], 1)
        self.assertEqual(today["total"], 120)
        self.assertEqual(today["input"], 100)
        week = summary["week"]
        self.assertEqual(week["runs"], 2)
        self.assertEqual(week["total"], 180)

    def test_unreported_is_marked_not_zeroed(self):
        runs = [self._run("a", {}, "2026-11-12T12:00:00+00:00"),
                self._run("b", {"total": 0, "calls": 2},
                          "2026-11-12T13:00:00+00:00")]
        summary = loop.usage_summary(runs, today=self.TODAY)
        self.assertEqual(summary["today"]["reported"], 0)
        self.assertEqual(summary["today"]["unreported"], 2)
        self.assertEqual(summary["today"]["total"], 0)
        self.assertIn("未上报", loop._usage_line("今日", summary["today"]))

    def test_no_runs_today(self):
        runs = [self._run("c", {"total": 2}, "2026-10-01T12:00:00+00:00")]
        summary = loop.usage_summary(runs, today=self.TODAY)
        self.assertEqual(summary["today"]["runs"], 0)
        self.assertIn("无记录", loop._usage_line("今日", summary["today"]))

    def test_old_run_without_history_uses_updated_at(self):
        run = Run(run_id="a", usage={"total": 5},
                  updated_at="2026-11-12T12:00:00+00:00", history=[])
        summary = loop.usage_summary([run], today=self.TODAY)
        self.assertEqual(summary["today"]["total"], 5)

    def test_summary_ignores_bad_history_time(self):
        run = Run(run_id="a", usage={"total": 5}, history=[{"at": "not-a-time"}])
        run.updated_at = "also-bad"
        run.created_at = "bad-too"
        summary = loop.usage_summary([run], today=self.TODAY)
        self.assertEqual(summary["today"]["runs"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
