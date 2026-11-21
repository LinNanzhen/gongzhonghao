#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""说话人自动对照测试：python3 -m unittest orchestrator.test_speakers -v"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.speakers import (  # noqa: E402
    guess_speaker_map, heuristic_speaker_map, render_speaker_map, speaker_labels,
)

# 中文访谈：主持人口播 + 嘉宾名单 + 两个自我介绍
CHINESE = [
    {"start": 0, "speaker": "说话人 1",
     "text": "大家好，欢迎来到本期节目，我是主持人小雨。今天请到的嘉宾是杨攀、张三。"},
    {"start": 30, "speaker": "说话人 2", "text": "大家好，我是杨攀，在 DSH 做产品。"},
    {"start": 60, "speaker": "说话人 3", "text": "我是张三，负责游戏发行。"},
    {"start": 90, "speaker": "说话人 1", "text": "那我们开始。"},
]

ENGLISH = [
    {"start": 0, "speaker": "说话人 1",
     "text": "Welcome to the show, I'm your host Emily Chang."},
    {"start": 30, "speaker": "说话人 2", "text": "I'm Lisa Su, CEO of AMD."},
]


class FakeProvider:
    """按顺序返回预设回复；回复是异常就抛出，模拟 provider 失败。"""

    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.requests: list[dict] = []

    def complete(self, *, task: str, system: str, prompt: str,
                 max_tokens: int = 2000, temperature: float = 0.7) -> str:
        self.calls += 1
        self.requests.append({"task": task, "system": system, "prompt": prompt,
                              "max_tokens": max_tokens, "temperature": temperature})
        reply = self.replies.pop(0) if self.replies else ""
        if isinstance(reply, BaseException):
            raise reply
        return reply


# ---------------------------------------------------------------- 基础


class TestLabels(unittest.TestCase):
    def test_ordered_dedup(self):
        segs = [{"speaker": "说话人 2"}, {"speaker": "说话人 1"},
                {"speaker": "说话人 2"}, {"speaker": ""}]
        self.assertEqual(speaker_labels(segs), ["说话人 2", "说话人 1"])

    def test_empty(self):
        self.assertEqual(speaker_labels([{"speaker": ""}]), [])
        self.assertEqual(speaker_labels([]), [])


# ---------------------------------------------------------------- LLM


class TestLlmMap(unittest.TestCase):
    def test_json_direct(self):
        provider = FakeProvider([
            '{"说话人 1": {"name": "小雨", "abbr": "XY"}, '
            '"说话人 2": {"name": "杨攀", "abbr": "YP"}}'])
        mapping = guess_speaker_map(CHINESE, provider)
        self.assertEqual(mapping["说话人 1"]["name"], "小雨")
        self.assertEqual(mapping["说话人 1"]["abbr"], "XY")
        self.assertEqual(mapping["说话人 2"]["name"], "杨攀")
        self.assertIn("模型", mapping["说话人 2"]["basis"])

    def test_code_fence_and_noise(self):
        provider = FakeProvider([
            '好的，结果如下：\n```json\n{"说话人 3": {"name": "张三", "abbr": "ZS"}}\n```\n'])
        mapping = guess_speaker_map(CHINESE, provider)
        self.assertEqual(mapping["说话人 3"]["name"], "张三")

    def test_prompt_and_task(self):
        provider = FakeProvider(['{"说话人 1": {"name": "小雨"}}'])
        guess_speaker_map(CHINESE, provider)
        request = provider.requests[0]
        self.assertEqual(request["task"], "speakers.map")
        self.assertEqual(request["temperature"], 0)
        self.assertIn("说话人 1", request["prompt"])
        self.assertIn("欢迎来到", request["prompt"])
        self.assertIn("JSON", request["system"])

    def test_retry_after_provider_error(self):
        provider = FakeProvider([RuntimeError("限流"),
                                 '{"说话人 2": {"name": "杨攀", "abbr": "YP"}}'])
        mapping = guess_speaker_map(CHINESE, provider)
        self.assertEqual(provider.calls, 2, "失败应重试 1 次")
        self.assertEqual(mapping["说话人 2"]["name"], "杨攀")

    def test_retry_after_bad_json(self):
        provider = FakeProvider(["不是 JSON",
                                 '{"说话人 2": {"name": "杨攀", "abbr": "YP"}}'])
        mapping = guess_speaker_map(CHINESE, provider)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(mapping["说话人 2"]["name"], "杨攀")

    def test_string_value_form(self):
        provider = FakeProvider(['{"说话人 2": "杨攀 YP"}'])
        mapping = guess_speaker_map(CHINESE, provider)
        self.assertEqual(mapping["说话人 2"]["name"], "杨攀")
        self.assertEqual(mapping["说话人 2"]["abbr"], "YP")

    def test_two_failures_fall_back_to_heuristic(self):
        provider = FakeProvider([RuntimeError("挂了"), "完全不是 JSON"])
        mapping = guess_speaker_map(CHINESE, provider)
        self.assertEqual(provider.calls, 2, "最多重试 1 次，不能无限调")
        self.assertEqual(mapping["说话人 1"]["name"], "小雨")
        self.assertEqual(mapping["说话人 2"]["name"], "杨攀")

    def test_unknown_labels_ignored(self):
        provider = FakeProvider(['{"主持人": {"name": "不该采纳"}}'])
        mapping = guess_speaker_map(CHINESE, provider)
        self.assertNotIn("主持人", mapping)
        self.assertEqual(mapping["说话人 2"]["name"], "杨攀", "应回退启发式")


# ---------------------------------------------------------------- 启发式


class TestHeuristic(unittest.TestCase):
    def test_host_guest_and_self_intro(self):
        mapping = heuristic_speaker_map(CHINESE)
        self.assertEqual(mapping["说话人 1"]["name"], "小雨")
        self.assertIn("主持", mapping["说话人 1"]["basis"])
        self.assertEqual(mapping["说话人 2"]["name"], "杨攀")
        self.assertEqual(mapping["说话人 3"]["name"], "张三")

    def test_english_intros(self):
        mapping = heuristic_speaker_map(ENGLISH)
        self.assertEqual(mapping["说话人 1"]["name"], "Emily Chang")
        self.assertEqual(mapping["说话人 1"]["abbr"], "EC")
        self.assertEqual(mapping["说话人 2"]["name"], "Lisa Su")
        self.assertEqual(mapping["说话人 2"]["abbr"], "LS")

    def test_guest_list_only(self):
        segs = [
            {"speaker": "说话人 1",
             "text": "欢迎来到本期节目，今天请到的嘉宾是杨攀（YP）、张三老师。"},
            {"speaker": "说话人 2", "text": "嗯。"},
            {"speaker": "说话人 3", "text": "对。"},
        ]
        mapping = heuristic_speaker_map(segs)
        self.assertEqual(mapping["说话人 1"]["name"], "主持人")
        self.assertEqual(mapping["说话人 2"]["name"], "杨攀")
        self.assertEqual(mapping["说话人 2"]["abbr"], "YP")
        self.assertEqual(mapping["说话人 3"]["name"], "张三")

    def test_no_samples_returns_empty(self):
        segs = [{"speaker": "说话人 1", "text": "嗯。"},
                {"speaker": "说话人 2", "text": "对。"}]
        self.assertEqual(heuristic_speaker_map(segs), {})

    def test_no_labels_returns_empty(self):
        self.assertEqual(heuristic_speaker_map([{"speaker": "", "text": "无标签"}]), {})
        self.assertEqual(heuristic_speaker_map([]), {})


# ---------------------------------------------------------------- 对外与渲染


class TestGuessEntrypoint(unittest.TestCase):
    def test_no_provider_uses_heuristic(self):
        mapping = guess_speaker_map(CHINESE, None)
        self.assertEqual(mapping["说话人 2"]["name"], "杨攀")

    def test_empty_segments(self):
        self.assertEqual(guess_speaker_map([], FakeProvider([])), {})


class TestRender(unittest.TestCase):
    def test_markdown_table(self):
        text = render_speaker_map(
            {"说话人 1": {"name": "小雨", "abbr": "XY", "basis": "主持人口播"},
             "说话人 2": {"name": "杨攀", "abbr": "", "basis": "自我介绍|名单"}},
            guest="测试嘉宾", today="2026-11-12")
        self.assertIn("# 说话人对照 · 测试嘉宾", text)
        self.assertIn("| 标签 | 姓名 | 缩写 | 依据 |", text)
        self.assertIn("| 说话人 1 | 小雨 | XY | 主持人口播 |", text)
        self.assertIn("| 说话人 2 | 杨攀 | — | 自我介绍／名单 |", text,
                      "缺缩写填 —，依据里的竖线不能破坏表格")

    def test_empty_map_still_renders_header(self):
        text = render_speaker_map({}, guest="某期", today="2026-11-12")
        self.assertIn("| 标签 | 姓名 | 缩写 | 依据 |", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
