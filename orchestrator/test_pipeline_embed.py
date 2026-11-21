#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""流水线图片内联测试：python3 -m unittest orchestrator.test_pipeline_embed -v

背景：正文 md 里图片一般写相对路径（图片/fig_01.jpg）。导入片段要粘到微信编辑器，
本地相对路径会断图，必须在生成 HTML 时内联成 data URI；http(s) 外链保持原样，
文件不存在时保持原样（让质检的「图片链接合规」报 FAIL，不静默吞掉）。
"""
from __future__ import annotations

import base64
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "流水线" / "gzh_pipeline.py"


def load_pipeline():
    spec = importlib.util.spec_from_file_location("gzh_pipeline_under_test", PIPELINE)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class TestEmbedLocalImage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gp = load_pipeline()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "图片").mkdir()
        self.jpg = self.dir / "图片" / "fig_01.jpg"
        # 1x1 JPEG 最小字节（PIL 不可用也能跑）
        self.jpg.write_bytes(base64.b64decode(
            "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
            "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
            "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="))

    def tearDown(self):
        self.tmp.cleanup()

    def test_relative_image_embedded(self):
        url = self.gp._embed_local_image("图片/fig_01.jpg", self.dir)
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))

    def test_http_unchanged(self):
        url = self.gp._embed_local_image("https://mmbiz.qpic.cn/x.jpg", self.dir)
        self.assertEqual(url, "https://mmbiz.qpic.cn/x.jpg")

    def test_data_uri_unchanged(self):
        src = "data:image/png;base64,AAAA"
        self.assertEqual(self.gp._embed_local_image(src, self.dir), src)

    def test_missing_file_kept_as_is(self):
        url = self.gp._embed_local_image("图片/不存在.jpg", self.dir)
        self.assertEqual(url, "图片/不存在.jpg")

    def test_no_md_dir_keeps_relative(self):
        url = self.gp._embed_local_image("图片/fig_01.jpg", None)
        self.assertEqual(url, "图片/fig_01.jpg")

    def test_parse_md_embeds_and_keeps_alt(self):
        md = "# 标题\n\n## 导语\n\n正文。\n\n![图注](图片/fig_01.jpg)\n"
        html = self.gp.parse_md(md, md_dir=self.dir)
        self.assertIn("data:image/jpeg;base64,", html)
        self.assertIn('alt="图注"', html)


class TestImportFragmentSelfContained(unittest.TestCase):
    """导入片段里不应再出现相对路径图片。"""

    @classmethod
    def setUpClass(cls):
        cls.gp = load_pipeline()

    def test_import_contains_no_relative_src(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "图片").mkdir()
            (d / "图片" / "a.jpg").write_bytes(b"\xff\xd8\xff\xd9")
            md = "# T\n\n## 导语\n\nx\n\n![c](图片/a.jpg)\n"
            body = self.gp.parse_md(md, md_dir=d)
            self.assertNotIn('src="图片/a.jpg"', body)
            self.assertIn("data:image/jpeg;base64,", body)


if __name__ == "__main__":
    sys.exit(unittest.main())
