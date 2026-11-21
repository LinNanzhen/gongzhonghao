#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""illustrator agent 单测：happy path（抽帧 3 张 + 封面 + 台账 + 新 md）、幂等、降级矩阵。

降级矩阵覆盖：没有正文 md / 没有视频 / 没有 ffmpeg / 抽帧全失败 / 没有可定位引语 /
Pillow 缺失 —— 一律 ``ok(to="illustrated")``，绝不 blocked、绝不编造图片。
测试用 ffmpeg 离线合成视频（testsrc），不联网。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import figures  # noqa: E402
from orchestrator.agents.base import Context  # noqa: E402
from orchestrator.agents.illustrator import Illustrator  # noqa: E402
from orchestrator.providers.stub import StubProvider  # noqa: E402
from orchestrator.state import Run  # noqa: E402

FFMPEG = figures.find_ffmpeg()
FONT = figures.find_cjk_font() if figures.PIL_AVAILABLE else None
PIL_OK = figures.PIL_AVAILABLE

MD = """# 苏姿丰：把 AMD 从失败里捞出来的人

## 导语

这场访谈里最值得注意的不是结论，而是结论成立的条件。

<!-- 人物卡 -->

## 01｜先被重新定价的是入门岗位

（这一节只推进一个问题。）

> IS：我特别相信这一点
> —— 原视频 00:01

**这一句是本节结论。**

## 02｜能力在飙升，价格还没动

> DP：第二处原话
> EN|The second locatable quote.
> —— 原视频 00:03

## 03｜什么在涨价：意图、判断、品味

> IS：第三处原话
> —— 原视频 00:05

## 来源与说明

[1] 节目名称，发布日期，链接。除署名缩写的对话原话外，本文的叙述、归纳与判断均为作者观点。
"""

CASE_NAME = "16_苏姿丰回归_案例"
GUEST = "苏姿丰回归"
DRAFT_NAME = f"{GUEST}_人味版_v1.md"


def make_video(path: Path, duration: int = 12, size: str = "1280x720") -> bool:
    if not FFMPEG:
        return False
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc=duration={duration}:size={size}:rate=5",
           "-pix_fmt", "yuv420p", "-preset", "ultrafast", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return proc.returncode == 0 and path.exists() and path.stat().st_size > 0


class IllustratorTestBase(unittest.TestCase):
    """每个用例一个干净的临时案例目录；视频在类级只合成一次。"""

    _shared_tmp: tempfile.TemporaryDirectory | None = None
    video: Path | None = None

    @classmethod
    def setUpClass(cls):
        cls._shared_tmp = tempfile.TemporaryDirectory()
        cls.video = Path(cls._shared_tmp.name) / "shared.mp4"
        if not make_video(cls.video):
            cls.video = None

    @classmethod
    def tearDownClass(cls):
        cls._shared_tmp.cleanup()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.case = self.dir / CASE_NAME
        self.case.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    # -- helpers -----------------------------------------------------
    def write_draft(self, text: str = MD, name: str = DRAFT_NAME) -> Path:
        path = self.case / name
        path.write_text(text, encoding="utf-8")
        return path

    def link_media(self, target: Path | None = None) -> None:
        """用「媒体路径.txt」把类级共享视频挂到案例里（base.py 的解析顺序 1）。"""
        video = target or self.video
        if video is None:
            self.skipTest("无法合成测试视频")
        (self.case / "媒体路径.txt").write_text(str(video), encoding="utf-8")

    def ctx(self, draft: str | None = DRAFT_NAME, *, dry_run: bool = False) -> Context:
        artifacts = {"draft": draft} if draft else {}
        run = Run(run_id="illustrator-test", stage="critiqued", artifacts=artifacts)
        return Context(run=run, case_dir=self.case,
                       orch_dir=self.dir / "orchestrator",
                       provider=StubProvider(), dry_run=dry_run)

    def run_agent(self, draft: str | None = DRAFT_NAME, *, dry_run: bool = False):
        return Illustrator().run(self.ctx(draft, dry_run=dry_run))


class TestContract(IllustratorTestBase):
    def test_agent_identity(self):
        agent = Illustrator()
        self.assertEqual(agent.name, "illustrator")
        self.assertEqual(agent.stage, "critiqued")

    def test_success_target_is_illustrated(self):
        """状态机契约：critiqued --illustrator--> illustrated（由 composer 接着排版）。"""
        self.write_draft()
        self.link_media()
        result = self.run_agent()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.to, "illustrated")
        self.assertTrue(Run(run_id="x", stage="critiqued").can("illustrated"))

    def test_no_draft_degrades(self):
        empty = self.dir / "empty_case"
        empty.mkdir()
        run = Run(run_id="x", stage="critiqued", artifacts={})
        result = Illustrator().run(Context(run=run, case_dir=empty,
                                           orch_dir=self.dir / "o", provider=StubProvider()))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.to, "illustrated")
        self.assertIn("正文 md", result.note)


@unittest.skipUnless(FFMPEG and PIL_OK, "需要 ffmpeg + Pillow")
class TestHappyPath(IllustratorTestBase):
    def test_three_figures_cover_and_ledger(self):
        self.write_draft()
        self.link_media()
        result = self.run_agent()
        self.assertEqual(result.status, "ok", result.note)
        self.assertEqual(result.to, "illustrated")
        for key in ("draft", "figure_ledger"):
            self.assertIn(key, result.artifacts, result.artifacts)
        self.assertTrue(result.artifacts["draft"].endswith("_插图.md"))
        for value in result.artifacts.values():
            self.assertFalse(Path(value).is_absolute())
            self.assertTrue((self.case / value).exists(), value)

        new_md = (self.case / result.artifacts["draft"]).read_text(encoding="utf-8")
        self.assertEqual(new_md.count("](图片/fig_0"), 3)
        for n in (1, 2, 3):
            self.assertIn(f"](图片/fig_0{n}.jpg)", new_md)
        self.assertIn("（截自原视频 ", new_md)
        self.assertIn("▲ IS：", new_md)
        # 原稿内容不丢
        self.assertIn("## 来源与说明", new_md)
        self.assertIn("> EN|The second locatable quote.", new_md)
        # 每张图都紧跟在「—— 原视频」归属行之后
        lines = new_md.splitlines()
        for idx, line in enumerate(lines):
            if line.startswith("!["):
                prev = next((ln for ln in reversed(lines[:idx]) if ln.strip()), "")
                self.assertTrue(prev.strip().startswith("> ——"), f"图前应是归属行：{prev!r}")

    def test_figure_size_1000(self):
        self.write_draft()
        self.link_media()
        result = self.run_agent()
        with figures.Image.open(self.case / "图片" / "fig_01.jpg") as img:
            self.assertEqual(img.size[0], 1000)
            self.assertEqual(img.mode, "RGB")
        self.assertTrue(result.note)

    def test_cover_900x383(self):
        self.write_draft()
        self.link_media()
        result = self.run_agent()
        if FONT is None:
            self.assertNotIn("cover", result.artifacts)
            self.assertIn("字体", result.note)
            return
        self.assertIn("cover", result.artifacts)
        cover = self.case / result.artifacts["cover"]
        self.assertTrue(cover.name.startswith("封面_"))
        with figures.Image.open(cover) as img:
            self.assertEqual(img.size, (900, 383))

    def test_ledger_records_timestamps_and_sha(self):
        self.write_draft()
        self.link_media()
        result = self.run_agent()
        ledger = (self.case / result.artifacts["figure_ledger"]).read_text(encoding="utf-8")
        for token in ("# 图源记录", "| 图 | 时间戳 | 原视频 | 文件 | 处理 | 用途 | SHA256 |",
                      "| fig_01 | 00:01 |", "| fig_02 | 00:03 |", "| fig_03 | 00:05 |",
                      "图片/fig_01.jpg", "1000px + USM"):
            self.assertIn(token, ledger)
        sha = figures.sha256_file(self.case / "图片" / "fig_01.jpg")
        self.assertIn(sha, ledger)
        if FONT is not None:
            # 封面取第二张正文图（避免与 fig_01 同帧重复），时间戳随候选而变
            import re as _re
            self.assertTrue(_re.search(r"\| 封面 \| \d{2}:\d{2} \|", ledger))

    def test_rerun_writes_new_version_and_no_duplicate(self):
        self.write_draft()
        self.link_media()
        first = self.run_agent()
        second = self.run_agent()      # writer 的 draft 不变，重新配图
        self.assertNotEqual(first.artifacts["draft"], second.artifacts["draft"])
        self.assertTrue(second.artifacts["draft"].endswith("_v2_插图.md"))
        second_md = (self.case / second.artifacts["draft"]).read_text(encoding="utf-8")
        self.assertEqual(second_md.count("](图片/fig_0"), 3, "同路径不得重复插入")

    def test_illustrated_draft_stays_idempotent(self):
        self.write_draft()
        self.link_media()
        first = self.run_agent()
        self.write_draft()             # 故意只保留原稿
        already = (self.case / first.artifacts["draft"]).read_text(encoding="utf-8")
        (self.case / "已插图.md").write_text(already, encoding="utf-8")
        result = self.run_agent(draft="已插图.md")
        out = (self.case / result.artifacts["draft"]).read_text(encoding="utf-8")
        self.assertEqual(out.count("](图片/fig_0"), 3, "已插过图的正文不得重复插")

    def test_media_fallback_in_case_dir(self):
        """没有「媒体路径.txt」时，base.media_path 应能在案例目录里找到视频。"""
        if self.video is None:
            self.skipTest("无法合成测试视频")
        shutil.copy(self.video, self.case / "原片.mp4")
        self.write_draft()
        (self.case / "媒体路径.txt").unlink(missing_ok=True)
        result = self.run_agent()
        self.assertIn("draft", result.artifacts)
        self.assertTrue((self.case / "图片" / "fig_01.jpg").exists())

    def test_no_font_skips_cover_only(self):
        self.write_draft()
        self.link_media()
        with unittest.mock.patch.object(figures, "find_cjk_font", return_value=None):
            result = self.run_agent()
        self.assertEqual(result.status, "ok")
        self.assertNotIn("cover", result.artifacts)
        self.assertIn("draft", result.artifacts)
        self.assertGreaterEqual((self.case / result.artifacts["draft"]).read_text(
            encoding="utf-8").count("](图片/fig_0"), 1)
        self.assertIn("字体", result.note)

    def test_no_h1_skips_cover(self):
        no_h1 = MD.replace("# 苏姿丰：把 AMD 从失败里捞出来的人\n", "")
        self.write_draft(no_h1)
        self.link_media()
        result = self.run_agent()
        self.assertNotIn("cover", result.artifacts)
        self.assertIn("H1", result.note)

    def test_dry_run_writes_nothing(self):
        self.write_draft()
        self.link_media()
        before = sorted(p.name for p in self.case.iterdir())
        result = self.run_agent(dry_run=True)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.to, "illustrated")
        self.assertEqual(sorted(p.name for p in self.case.iterdir()), before)
        self.assertEqual(result.artifacts, {})

    def test_figure_count_capped(self):
        extra = "".join(f"\n## 0{n + 4}｜第{n + 4}节\n\n> IS：第{n + 4}句原话\n"
                        f"> —— 原视频 00:0{n + 6}\n" for n in range(4))
        self.write_draft(MD + extra)
        self.link_media()
        result = self.run_agent()
        out = (self.case / result.artifacts["draft"]).read_text(encoding="utf-8")
        self.assertEqual(out.count("](图片/fig_0"), 3)


NO_TS_MD = """# 苏姿丰：把 AMD 从失败里捞出来的人

## 01｜先被重新定价的是入门岗位

> IS：我特别相信这一点

## 来源与说明

声明。
"""


@unittest.skipUnless(FFMPEG and PIL_OK, "需要 ffmpeg + Pillow")
class TestTranscriptFallback(IllustratorTestBase):
    """正文引语缺「—— 原视频」时，用转写稿（artifact 优先，其次 转写/transcript.json）补时间戳。"""

    def write_transcript(self, path: Path, start: float = 2.0,
                         text: str = "我特别相信这一点，真的非常相信。") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"segments": [{"start": %s, "end": %s, "speaker": "说话人 1", '
            '"text": "%s"}]}' % (start, start + 3, text),
            encoding="utf-8")

    def ctx_with_transcript(self, transcript_artifact: str | None = None) -> Context:
        artifacts = {"draft": DRAFT_NAME}
        if transcript_artifact:
            artifacts["transcript"] = transcript_artifact
        run = Run(run_id="illustrator-test", stage="critiqued", artifacts=artifacts)
        return Context(run=run, case_dir=self.case,
                       orch_dir=self.dir / "orchestrator", provider=StubProvider())

    def test_transcript_in_case_dir_fills_timestamp(self):
        self.write_draft(NO_TS_MD)
        self.link_media()
        self.write_transcript(self.case / "转写" / "transcript.json", start=2.0)
        result = Illustrator().run(self.ctx_with_transcript())
        self.assertEqual(result.status, "ok")
        self.assertIn("draft", result.artifacts)
        out = (self.case / result.artifacts["draft"]).read_text(encoding="utf-8")
        self.assertIn("（截自原视频 00:02）", out)
        self.assertIn("转写稿", result.note)

    def test_transcript_artifact_takes_priority(self):
        self.write_draft(NO_TS_MD)
        self.link_media()
        self.write_transcript(self.case / "转写" / "transcript.json", start=2.0)
        self.write_transcript(self.case / "自定义.json", start=4.0)
        result = Illustrator().run(self.ctx_with_transcript("自定义.json"))
        out = (self.case / result.artifacts["draft"]).read_text(encoding="utf-8")
        self.assertIn("（截自原视频 00:04）", out)
        self.assertNotIn("（截自原视频 00:02）", out)

    def test_unmatched_transcript_still_degrades(self):
        self.write_draft(NO_TS_MD)
        self.link_media()
        self.write_transcript(self.case / "转写" / "transcript.json",
                              start=2.0, text="完全不相干的另一段话")
        result = Illustrator().run(self.ctx_with_transcript())
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.artifacts, {})
        self.assertIn("引语", result.note)


@unittest.skipUnless(FFMPEG and PIL_OK, "需要 ffmpeg + Pillow")
class TestDegradation(IllustratorTestBase):
    def assert_degraded_ok(self, result, keyword: str):
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.to, "illustrated")
        self.assertIn(keyword, result.note)

    def test_no_media(self):
        self.write_draft()
        with unittest.mock.patch.dict(os.environ, {"GZH_MEDIA_DIR": ""}):
            result = self.run_agent()
        self.assert_degraded_ok(result, "原视频")
        self.assertFalse((self.case / "图片").exists())
        self.assertFalse(list(self.case.glob("*.jpg")))

    def test_no_ffmpeg(self):
        self.write_draft()
        self.link_media()
        with unittest.mock.patch.object(figures, "find_ffmpeg", return_value=None):
            result = self.run_agent()
        self.assert_degraded_ok(result, "ffmpeg")
        self.assertEqual(result.artifacts, {})

    def test_extraction_all_fails(self):
        self.write_draft()
        self.link_media()
        with unittest.mock.patch.object(figures, "extract_frame",
                                        return_value=(False, "boom")):
            result = self.run_agent()
        self.assert_degraded_ok(result, "抽帧")
        self.assertEqual(result.artifacts, {})

    def test_no_locatable_quotes(self):
        no_quotes = MD.split("## 01")[0] + "## 01｜没有引语的一节\n\n只有转述。\n"
        self.write_draft(no_quotes)
        self.link_media()
        result = self.run_agent()
        self.assert_degraded_ok(result, "引语")
        self.assertEqual(result.artifacts, {})

    def test_pil_missing(self):
        self.write_draft()
        self.link_media()
        with unittest.mock.patch.object(figures, "PIL_AVAILABLE", False):
            result = self.run_agent()
        self.assert_degraded_ok(result, "Pillow")
        self.assertEqual(result.artifacts, {})

    def test_never_blocked(self):
        self.write_draft()
        with unittest.mock.patch.dict(os.environ, {"GZH_MEDIA_DIR": ""}):
            result = self.run_agent()
        self.assertNotEqual(result.status, "blocked")


if __name__ == "__main__":
    unittest.main(verbosity=2)
