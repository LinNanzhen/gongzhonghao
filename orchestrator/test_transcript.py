#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""转写稿解析测试：python3 -m unittest orchestrator.test_transcript -v"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.transcript import (  # noqa: E402
    parse_feishu, parse_srt, parse_text, parse_vtt, to_seconds,
)

FEISHU = """@说话人 1  00:01
第一段内容，跨了两行。
第二行。

@说话人 2  00:28
第二段内容。

@说话人 2  01:11
第三段。
"""

FEISHU_HMS = """@张三  1:02:03
带小时的时间戳。
"""

SRT = """1
00:00:01,000 --> 00:00:03,500
第一句

2
00:00:04,000 --> 00:00:06,000
第二句
"""

PLAIN = "没有任何时间戳的一段话。\n第二行。"

# YouTube 自动字幕：滚动累积（第二块包含第一块）
VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.000 align:start position:0%

hello

00:00:02.000 --> 00:00:04.000 align:start position:0%
hello
world

00:00:04.000 --> 00:00:06.000 align:start position:0%
world again
"""



class TestTimes(unittest.TestCase):
    def test_mm_ss(self):
        self.assertEqual(to_seconds("00:28"), 28.0)

    def test_hh_mm_ss(self):
        self.assertEqual(to_seconds("1:02:03"), 3723.0)

    def test_srt_comma(self):
        self.assertEqual(to_seconds("00:00:03,500"), 3.5)


class TestFeishu(unittest.TestCase):
    def test_segments(self):
        segs = parse_feishu(FEISHU)
        self.assertEqual(len(segs), 3)
        self.assertEqual(segs[0]["speaker"], "说话人 1")
        self.assertEqual(segs[0]["start"], 1.0)
        self.assertEqual(segs[1]["start"], 28.0)

    def test_multiline_body(self):
        segs = parse_feishu(FEISHU)
        self.assertIn("跨了两行", segs[0]["text"])
        self.assertIn("第二行", segs[0]["text"])

    def test_end_filled(self):
        segs = parse_feishu(FEISHU)
        for s in segs:
            self.assertGreater(s["end"], s["start"])
        self.assertEqual(segs[0]["end"], 28.0, "end 应等于下一段 start")

    def test_hour_timestamp(self):
        segs = parse_feishu(FEISHU_HMS)
        self.assertEqual(segs[0]["speaker"], "张三")
        self.assertEqual(segs[0]["start"], 3723.0)


class TestSrt(unittest.TestCase):
    def test_segments(self):
        segs = parse_srt(SRT)
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0]["start"], 1.0)
        self.assertEqual(segs[0]["end"], 3.5)
        self.assertEqual(segs[0]["text"], "第一句")


class TestVtt(unittest.TestCase):
    def test_rolling_dedupe(self):
        segs = parse_vtt(VTT)
        self.assertEqual(len(segs), 2, [s["text"] for s in segs])
        self.assertEqual(segs[0]["text"], "hello world")
        self.assertEqual(segs[0]["start"], 0.0)
        self.assertEqual(segs[0]["end"], 4.0)
        self.assertEqual(segs[1]["text"], "world again")

    def test_tags_stripped(self):
        raw = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<c.colorE5E5E5>hi</c> <00:00:01.500>there\n"
        segs = parse_vtt(raw)
        self.assertEqual(segs[0]["text"], "hi there")

    def test_dispatch_by_header(self):
        self.assertEqual(parse_text(VTT)[0]["text"], "hello world")


class TestDispatch(unittest.TestCase):
    def test_detect_feishu(self):
        segs = parse_text(FEISHU)
        self.assertEqual(segs[0]["speaker"], "说话人 1")

    def test_detect_srt(self):
        segs = parse_text(SRT)
        self.assertEqual(segs[1]["start"], 4.0)

    def test_detect_plain(self):
        segs = parse_text(PLAIN)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["start"], 0.0)
        self.assertIn("时间戳", segs[0]["text"])

    def test_empty(self):
        self.assertEqual(parse_text("   \n\n  "), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
