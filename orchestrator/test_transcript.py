#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""转写稿解析测试：python3 -m unittest orchestrator.test_transcript -v"""

from __future__ import annotations

import html
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.transcript import (  # noqa: E402
    convert, load_any, parse_docx, parse_feishu, parse_srt, parse_text,
    parse_vtt, to_payload, to_seconds,
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

SRT_SPEAKER = """1
00:00:01,000 --> 00:00:03,500
说话人 1: 第一句

2
00:00:04,000 --> 00:00:06,000
说话人 2：第二句
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


class TestSrtSpeaker(unittest.TestCase):
    def test_speaker_prefix(self):
        segs = parse_srt(SRT_SPEAKER)
        self.assertEqual([s["speaker"] for s in segs], ["说话人 1", "说话人 2"])
        self.assertEqual(segs[0]["text"], "第一句")
        self.assertEqual(segs[1]["text"], "第二句")


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


# ---------------------------------------------------------------- docx


def make_docx(path: Path, paragraphs: list[str], *, raw_body: str = "") -> Path:
    """构造最小 .docx（只需要 word/document.xml），不依赖 python-docx。"""
    body = raw_body or "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{html.escape(p)}</w:t></w:r></w:p>'
        for p in paragraphs)
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f'<w:body>{body}</w:body></w:document>')
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", xml)
    return path


class TestDocx(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def docx(self, name: str, paragraphs: list[str], **kwargs) -> Path:
        return make_docx(self.dir / name, paragraphs, **kwargs)

    def test_paragraph_split_without_timestamps(self):
        """没有时间轴时按段落切分：start=0 也要能产出 transcript.json。"""
        path = self.docx("plain.docx", ["第一段。", "第二段。", ""])
        segs = load_any(path)
        self.assertEqual([s["text"] for s in segs], ["第一段。", "第二段。"])
        self.assertEqual([s["start"] for s in segs], [0.0, 0.0])
        payload = to_payload(segs, source=path.name)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["segments"][1]["text"], "第二段。")

    def test_feishu_timeline(self):
        path = self.docx("feishu.docx", [
            "@说话人 1  00:01", "大家好，我是主持人小雨。",
            "@说话人 2  00:28", "我是杨攀。",
        ])
        segs = load_any(path)
        self.assertEqual([s["speaker"] for s in segs], ["说话人 1", "说话人 2"])
        self.assertEqual([s["start"] for s in segs], [1.0, 28.0])
        self.assertEqual(segs[0]["text"], "大家好，我是主持人小雨。")

    def test_srt_inside_docx(self):
        path = self.docx("srt.docx", [
            "1\n00:00:01,000 --> 00:00:03,500\n说话人 1: 第一句",
            "2\n00:00:04,000 --> 00:00:06,000\n说话人 2: 第二句",
        ])
        segs = load_any(path)
        self.assertEqual([s["text"] for s in segs], ["第一句", "第二句"])
        self.assertEqual(segs[0]["start"], 1.0)
        self.assertEqual(segs[0]["speaker"], "说话人 1")

    def test_speaker_prefix_without_timeline(self):
        path = self.docx("speaker.docx", ["说话人 1：第一句", "续一行", "说话人 2：第二句"])
        segs = load_any(path)
        self.assertEqual([s["speaker"] for s in segs], ["说话人 1", "说话人 2"])
        self.assertIn("续一行", segs[0]["text"])
        self.assertEqual(segs[1]["text"], "第二句")

    def test_runs_concatenated_and_entities_unescaped(self):
        raw = ('<w:p><w:r><w:t>前半</w:t></w:r><w:r><w:t>后半</w:t></w:r></w:p>'
               '<w:p><w:r><w:t>A &amp; B &lt; C</w:t></w:r></w:p>')
        path = self.docx("runs.docx", [], raw_body=raw)
        segs = load_any(path)
        self.assertEqual([s["text"] for s in segs], ["前半后半", "A & B < C"])

    def test_paragraph_with_tab_and_break(self):
        raw = '<w:p><w:r><w:t>甲</w:t><w:tab/><w:t>乙</w:t><w:br/><w:t>丙</w:t></w:r></w:p>'
        path = self.docx("tab.docx", [], raw_body=raw)
        self.assertEqual(load_any(path)[0]["text"], "甲 乙\n丙")

    def test_empty_docx(self):
        path = self.docx("empty.docx", ["", "   "])
        self.assertEqual(load_any(path), [])
        self.assertEqual(to_payload(load_any(path))["count"], 0)

    def test_docx_parse_function_directly(self):
        segs = parse_docx(["甲。", "乙。"])
        self.assertEqual([s["text"] for s in segs], ["甲。", "乙。"])
        self.assertEqual(parse_docx([]), [])

    def test_bad_docx_raises(self):
        bad = self.dir / "bad.docx"
        bad.write_bytes(b"not a zip")
        with self.assertRaises(ValueError):
            load_any(bad)

    def test_binary_doc_raises_with_hint(self):
        doc = self.dir / "old.doc"
        doc.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 old binary")
        with self.assertRaises(ValueError) as caught:
            load_any(doc)
        self.assertIn("docx", str(caught.exception))

    def test_doc_that_is_actually_docx(self):
        path = make_docx(self.dir / "renamed.doc", ["甲。", "乙。"])
        self.assertEqual([s["text"] for s in load_any(path)], ["甲。", "乙。"])

    def test_convert_writes_payload_json(self):
        path = self.docx("convert.docx", ["一段话。"])
        out = convert(path, self.dir / "transcript.json")
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["segments"][0]["text"], "一段话。")


if __name__ == "__main__":
    unittest.main(verbosity=2)
