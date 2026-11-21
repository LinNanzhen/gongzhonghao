#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配图纯函数（figures.py）的单元测试：引语解析 / 选点 / 插图 / 抽帧 / 封面 / 台账。"""
from __future__ import annotations

import hashlib
import shutil
import statistics
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import figures  # noqa: E402

PIL_OK = figures.PIL_AVAILABLE
FFMPEG = figures.find_ffmpeg()
FONT = figures.find_cjk_font() if PIL_OK else None

WRITER_MD = """# 门槛降到写网文，产品却更难被看见

## 导语

一场圆桌，四个人反复回到同一个落差。

<!-- 人物卡 -->

## 01｜软件和游戏，正在变成网文

杨攀把话说得很直。

> YP：就是软件和游戏网文化了
> —— 原视频 00:07

## 02｜154 万日活，没有一分推广费

归藏报出一组数字。

> GC：然后上周还发了一个他那个
> EN|then last week he posted
> —— 原视频 07:04

## 03｜入口从链接变成仓库名

AI 能力上来之后，封锁松了一道口子。

> GC：我贴个仓库名，你自然会去找
> —— 原视频 62:37-63:01
> —— 原视频 62:37-63:01
"""


def make_video(path: Path, duration: int = 3, size: str = "320x180") -> bool:
    """用 ffmpeg 合成测试视频（离线）；没有 ffmpeg 返回 False。"""
    if not FFMPEG:
        return False
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc=duration={duration}:size={size}:rate=5",
           "-pix_fmt", "yuv420p", "-preset", "ultrafast", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return proc.returncode == 0 and path.exists()


class TestTimecode(unittest.TestCase):
    def test_mmss(self):
        self.assertEqual(figures.parse_timecode("26:59"), ("26:59", 26 * 60 + 59))

    def test_pads_single_digit_minute(self):
        self.assertEqual(figures.parse_timecode("7:04"), ("07:04", 424))

    def test_hhmmss(self):
        self.assertEqual(figures.parse_timecode("01:17:24"), ("01:17:24", 4644))

    def test_minutes_over_59_keep_style(self):
        # 素材里就有 62:37 这种「分超过 59」的写法，抽帧时按 mm:ss 解释即可
        self.assertEqual(figures.parse_timecode("62:37-63:01"), ("62:37", 3757))

    def test_embedded_in_attribution(self):
        self.assertEqual(figures.parse_timecode("Dario Amodei，CEO，路演 00:30")[0], "00:30")

    def test_garbage(self):
        self.assertIsNone(figures.parse_timecode(""))
        self.assertIsNone(figures.parse_timecode("没有时间码"))


class TestQuoteParsing(unittest.TestCase):
    def test_writer_format(self):
        blocks = figures.quote_blocks(WRITER_MD)
        self.assertEqual(len(blocks), 3)
        first = blocks[0]
        self.assertEqual(first.speaker, "YP")
        self.assertEqual(first.text, "就是软件和游戏网文化了")
        self.assertEqual((first.timestamp, first.seconds), ("00:07", 7))
        self.assertEqual(first.section_no, 1)
        self.assertIn("软件和游戏", first.section_title)

    def test_english_line_skipped(self):
        blocks = figures.quote_blocks(WRITER_MD)
        self.assertEqual(blocks[1].text, "然后上周还发了一个他那个")

    def test_end_line_points_at_attribution(self):
        blocks = figures.quote_blocks(WRITER_MD)
        lines = WRITER_MD.splitlines()
        for block in blocks:
            self.assertTrue(lines[block.end_line].strip().startswith("> ——"),
                            "插图应插在「—— 原视频」那行之后")

    def test_range_takes_start(self):
        blocks = figures.quote_blocks(WRITER_MD)
        self.assertEqual(blocks[2].timestamp, "62:37")

    def test_named_attribution_speaker(self):
        md = ("# 标题\n\n## 01｜A\n\n"
              "> 这将是人类历史上最有前景的技术。\n"
              "> EN|This is going to be the most promising technology.\n"
              "> —— Dario Amodei，CEO & Co-Founder，路演 00:30\n")
        block = figures.quote_blocks(md)[0]
        self.assertEqual(block.speaker, "Dario Amodei")
        self.assertEqual(block.timestamp, "00:30")
        self.assertEqual(block.text, "这将是人类历史上最有前景的技术。")

    def test_plain_attribution_is_not_speaker(self):
        md = "## 01｜A\n\n> 谁不会写字呢\n> —— 原视频 00:27\n"
        self.assertEqual(figures.quote_blocks(md)[0].speaker, "")

    def test_without_timestamp_not_locatable(self):
        md = "## 01｜A\n\n> YP：一句没有时间戳的话\n\n正文。\n"
        blocks = figures.quote_blocks(md)
        self.assertEqual(len(blocks), 1)
        self.assertFalse(blocks[0].locatable)
        self.assertIsNone(blocks[0].seconds)

    def test_empty_quote_dropped(self):
        md = "## 01｜A\n\n> —— 原视频 00:07\n"
        self.assertEqual(figures.quote_blocks(md), [])

    def test_no_quote_blocks(self):
        self.assertEqual(figures.quote_blocks("# 只有正文\n\n一段话。\n"), [])

    def test_markdown_emphasis_stripped(self):
        md = "## 01｜A\n\n> YP：**一句加粗原话**\n> —— 原视频 00:07\n"
        self.assertEqual(figures.quote_blocks(md)[0].text, "一句加粗原话")


def block(order: int, section: int, ts: str, text: str = "一句足够长的原话") -> figures.QuoteBlock:
    parsed = figures.parse_timecode(ts)
    return figures.QuoteBlock(order=order, section_no=section, section_title=f"0{section}｜节",
                              speaker="YP", text=text, timestamp=parsed[0],
                              seconds=parsed[1], end_line=order * 10)


class TestSelection(unittest.TestCase):
    def test_spreads_over_sections(self):
        blocks = [block(i, i, f"00:{i:02d}") for i in range(6)]
        picks = figures.select_quotes(blocks, 3)
        self.assertEqual([p.section_no for p in picks], [0, 3, 5])

    def test_fewer_sections_than_figures(self):
        blocks = [block(0, 0, "00:01"), block(1, 0, "00:02"), block(2, 1, "00:03"),
                  block(3, 1, "00:04"), block(4, 1, "00:05")]
        picks = figures.select_quotes(blocks, 3)
        self.assertEqual(len(picks), 3)
        self.assertEqual(len({p.timestamp for p in picks}), 3)

    def test_document_order(self):
        blocks = [block(i, 5 - i, f"00:0{i}") for i in range(5)]
        picks = figures.select_quotes(blocks, 3)
        self.assertEqual([p.order for p in picks], sorted(p.order for p in picks))

    def test_same_timestamp_not_used_twice(self):
        blocks = [block(0, 0, "00:01"), block(1, 1, "00:01"), block(2, 2, "00:02"),
                  block(3, 3, "00:02"), block(4, 4, "00:03")]
        picks = figures.select_quotes(blocks, 3)
        self.assertEqual(len({p.timestamp for p in picks}), len(picks))

    def test_skips_unlocatable(self):
        blocks = [block(0, 0, "00:01"),
                  figures.QuoteBlock(1, 1, "01｜节", "YP", "无时间戳", "", None, 20),
                  block(2, 2, "00:03")]
        picks = figures.select_quotes(blocks, 3)
        self.assertNotIn("", [p.timestamp for p in picks])
        self.assertEqual(len(picks), 2)

    def test_empty_and_zero(self):
        self.assertEqual(figures.select_quotes([], 3), [])
        self.assertEqual(figures.select_quotes([block(0, 0, "00:01")], 0), [])

    def test_count_one(self):
        picks = figures.select_quotes([block(i, i, f"00:{i + 1:02d}") for i in range(4)], 1)
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0].section_no, 0)


class TestInsert(unittest.TestCase):
    MD = ("# 标题\n\n## 01｜A\n\n"
          "> YP：一句话\n"
          "> —— 原视频 00:07\n"
          "\n"
          "正文。\n")

    def test_insert_after_block(self):
        out = figures.insert_figure(self.MD, 5, "▲ YP：一句话（截自原视频 00:07）",
                                    "图片/fig_01.jpg")
        self.assertIn("> —— 原视频 00:07\n\n![▲ YP：一句话（截自原视频 00:07）]"
                      "(图片/fig_01.jpg)\n\n正文。", out)

    def test_idempotent_same_path(self):
        once = figures.insert_figure(self.MD, 5, "▲ YP：一句话", "图片/fig_01.jpg")
        twice = figures.insert_figure(once, 5, "▲ YP：一句话", "图片/fig_01.jpg")
        self.assertEqual(once, twice)
        self.assertEqual(once.count("](图片/fig_01.jpg)"), 1)

    def test_multiple_inserts_descending_lines(self):
        md = ("# 标题\n\n## 01｜A\n\n> YP：第一句\n> —— 原视频 00:01\n\n"
              "中间。\n\n## 02｜B\n\n> GC：第二句\n> —— 原视频 00:02\n\n结尾。\n")
        lines = md.splitlines()
        first_end = next(i for i, ln in enumerate(lines) if "00:01" in ln)
        second_end = next(i for i, ln in enumerate(lines) if "00:02" in ln)
        out = figures.insert_figures(md, [(first_end, "图片/fig_01.jpg", "▲ YP：第一句"),
                                          (second_end, "图片/fig_02.jpg", "▲ GC：第二句")])
        positions = [out.index("](图片/fig_01.jpg)"), out.index("](图片/fig_02.jpg)")]
        self.assertLess(positions[0], positions[1])
        self.assertIn("中间。", out)
        self.assertIn("结尾。", out)

    def test_insert_at_end(self):
        out = figures.insert_figure("# 标题\n", 0, "▲ 图注", "图片/fig_01.jpg")
        self.assertIn("](图片/fig_01.jpg)", out)


class TestTitle(unittest.TestCase):
    def test_split_at_comma(self):
        self.assertEqual(figures.split_title("门槛降到写网文，产品却更难被看见"),
                         ["门槛降到写网文", "产品却更难被看见"])

    def test_picks_balanced_separator(self):
        self.assertEqual(figures.split_title("很短：中间那句话明显更长的后半部分"),
                         ["很短", "中间那句话明显更长的后半部分"])

    def test_no_punct_two_lines(self):
        lines = figures.split_title("一二三四五六七八")
        self.assertEqual(len(lines), 2)
        self.assertEqual("".join(lines), "一二三四五六七八")

    def test_english_breaks_on_space(self):
        lines = figures.split_title("Anthropic 把 AI 故事换成账")
        self.assertEqual(len(lines), 2)
        self.assertEqual("".join(lines).replace(" ", ""),
                         "Anthropic把AI故事换成账")

    def test_short_title_single_line(self):
        self.assertEqual(figures.split_title("短标题"), ["短标题"])

    def test_parse_h1(self):
        self.assertEqual(figures.parse_h1("# **标题**\n\n正文"), "标题")

    def test_parse_h1_missing(self):
        self.assertEqual(figures.parse_h1("## 扫码\n\n正文"), "")


class TestCaption(unittest.TestCase):
    def test_format(self):
        self.assertEqual(figures.caption("GC", "链接贴不了，就贴仓库名", "26:59"),
                         "▲ GC：链接贴不了，就贴仓库名（截自原视频 26:59）")

    def test_truncates_to_20_chars(self):
        long_text = "一二三四五六七八九十一二三四五六七八九十多余的话"
        cap = figures.caption("YP", long_text, "00:07")
        head = cap.split("：", 1)[1].split("（截自")[0]
        self.assertEqual(len(head), 21)   # 20 字 + 省略号
        self.assertTrue(head.endswith("…"))

    def test_without_speaker(self):
        self.assertEqual(figures.caption("", "一句话", "00:07"),
                         "▲ 一句话（截自原视频 00:07）")


class TestLedger(unittest.TestCase):
    def test_table_and_rows(self):
        text = figures.render_ledger(
            guest="DSH 大肥鱼", date_str="2026-11-12", media="/media/a.mp4",
            figures=[{"name": "fig_01", "timestamp": "07:04", "file": "图片/fig_01.jpg",
                      "purpose": "开篇：现场", "sha256": "a" * 64}],
            cover={"timestamp": "07:04", "file": "封面_2026-11-12_DSH 大肥鱼.jpg",
                   "sha256": "b" * 64})
        self.assertIn("# 图源记录 · DSH 大肥鱼", text)
        self.assertIn("| 图 | 时间戳 | 原视频 | 文件 | 处理 | 用途 | SHA256 |", text)
        self.assertIn("| fig_01 | 07:04 |", text)
        self.assertIn("图片/fig_01.jpg", text)
        self.assertIn("a" * 64, text)
        self.assertIn("| 封面 | 07:04 |", text)

    def test_notes_rendered(self):
        text = figures.render_ledger(
            guest="嘉宾", date_str="2026-11-12", media="a.mp4", figures=[],
            notes=["找不到 ffmpeg：跳过正文图与封面"])
        self.assertIn("- 找不到 ffmpeg：跳过正文图与封面", text)
        self.assertIn("## 说明", text)

    def test_sha256_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.bin"
            p.write_bytes(b"hello")
            self.assertEqual(figures.sha256_file(p),
                             hashlib.sha256(b"hello").hexdigest())


class TestTranscriptFallback(unittest.TestCase):
    """transcript 只做兜底：引语块缺「—— 原视频」时去转写稿里找 start。"""

    SEGMENTS = [
        {"start": 7.0, "end": 12.0, "speaker": "说话人 1",
         "text": "我特别相信这一点，真的非常相信。"},
        {"start": 424.0, "end": 430.0, "speaker": "Ilya Sutskever",
         "text": "We believe $1 of compute at Anthropic goes further."},
    ]

    def test_format_seconds(self):
        self.assertEqual(figures.format_seconds(7), "00:07")
        self.assertEqual(figures.format_seconds(4644), "01:17:24")
        self.assertEqual(figures.format_seconds(0), "00:00")

    def test_load_transcript_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript.json"
            path.write_text('{"segments": [{"start": 1.0, "text": "一句"}]}',
                            encoding="utf-8")
            segments = figures.load_transcript(path)
            self.assertEqual(len(segments), 1)
            self.assertFalse(figures.load_transcript(Path(tmp) / "missing.json"))

    def test_find_segment_fuzzy(self):
        seg = figures.find_transcript_segment("我特别相信这一点", self.SEGMENTS)
        self.assertIsNotNone(seg)
        self.assertEqual(seg["start"], 7.0)

    def test_find_segment_ignores_punctuation_case(self):
        seg = figures.find_transcript_segment(
            "we believe $1 of compute at anthropic", self.SEGMENTS)
        self.assertIsNotNone(seg)
        self.assertEqual(seg["speaker"], "Ilya Sutskever")

    def test_find_segment_no_match(self):
        self.assertIsNone(figures.find_transcript_segment("完全对不上的一句话", self.SEGMENTS))

    def test_locate_fills_missing_timestamp(self):
        quote = figures.QuoteBlock(0, 0, "01｜节", "IS", "我特别相信这一点", "", None, 4)
        located = figures.locate_quotes([quote], self.SEGMENTS)[0]
        self.assertEqual((located.timestamp, located.seconds), ("00:07", 7))
        self.assertEqual(located.speaker, "IS", "正文已有的说话人优先，不被转写稿覆盖")

    def test_locate_keeps_existing_timestamp(self):
        quote = figures.QuoteBlock(0, 0, "01｜节", "IS", "我特别相信这一点", "00:03", 3, 4)
        self.assertEqual(figures.locate_quotes([quote], self.SEGMENTS)[0].timestamp, "00:03")

    def test_locate_named_speaker_only(self):
        quote = figures.QuoteBlock(0, 0, "01｜节", "", "we believe $1 of compute", "", None, 4)
        located = figures.locate_quotes([quote], self.SEGMENTS)[0]
        self.assertEqual(located.speaker, "Ilya Sutskever")

    def test_locate_keeps_generic_speaker_empty(self):
        quote = figures.QuoteBlock(0, 0, "01｜节", "", "我特别相信这一点", "", None, 4)
        located = figures.locate_quotes([quote], self.SEGMENTS)[0]
        self.assertEqual(located.speaker, "", "「说话人 1」不是人名，不能当图注说话人")

    def test_locate_no_segments_is_noop(self):
        quote = figures.QuoteBlock(0, 0, "01｜节", "IS", "我特别相信这一点", "", None, 4)
        self.assertEqual(figures.locate_quotes([quote], []), [quote])


class TestFrameExtraction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls._tmp.name)
        cls.video = cls.dir / "v.mp4"
        cls.have_video = make_video(cls.video, duration=3)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_find_ffmpeg_matches_shutil(self):
        self.assertEqual(figures.find_ffmpeg(), shutil.which("ffmpeg"))

    @unittest.skipUnless(FFMPEG and PIL_OK, "需要 ffmpeg + Pillow")
    def test_extract_frame(self):
        if not self.have_video:
            self.skipTest("合成测试视频失败")
        out = self.dir / "frame.jpg"
        ok, err = figures.extract_frame(self.video, 1, out)
        self.assertTrue(ok, err)
        with figures.Image.open(out) as img:
            self.assertEqual(img.size, (320, 180))

    @unittest.skipUnless(FFMPEG, "需要 ffmpeg")
    def test_beyond_duration_fails_softly(self):
        if not self.have_video:
            self.skipTest("合成测试视频失败")
        ok, err = figures.extract_frame(self.video, 9999, self.dir / "none.jpg")
        self.assertFalse(ok)
        self.assertTrue(err)

    def test_missing_video_fails_softly(self):
        ok, err = figures.extract_frame(self.dir / "missing.mp4", 1, self.dir / "x.jpg")
        self.assertFalse(ok)
        self.assertIn("不存在", err)

    def test_missing_ffmpeg_binary_fails_softly(self):
        ok, err = figures.extract_frame(self.dir / "any.mp4", 1, self.dir / "x.jpg",
                                        ffmpeg="/definitely/not/ffmpeg")
        self.assertFalse(ok)
        self.assertTrue(err)


class TestRenderFigure(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _make(self, size, mode="RGB"):
        path = self.dir / "raw.png"
        color = (30, 60, 120, 255) if mode == "RGBA" else (30, 60, 120)
        figures.Image.new(mode, size, color).save(path)
        return path

    @unittest.skipUnless(PIL_OK, "需要 Pillow")
    def test_downscale_to_1000(self):
        out = self.dir / "fig.jpg"
        self.assertEqual(figures.render_figure(self._make((1920, 1080)), out), (1000, 562))
        with figures.Image.open(out) as img:
            self.assertEqual(img.size, (1000, 562))
            self.assertEqual(img.mode, "RGB")
            self.assertTrue(img.info.get("progressive"))

    @unittest.skipUnless(PIL_OK, "需要 Pillow")
    def test_no_upscale(self):
        out = self.dir / "fig.jpg"
        self.assertEqual(figures.render_figure(self._make((640, 360)), out), (640, 360))

    @unittest.skipUnless(PIL_OK, "需要 Pillow")
    def test_rgba_converted_to_rgb(self):
        out = self.dir / "fig.jpg"
        figures.render_figure(self._make((100, 50), mode="RGBA"), out)
        with figures.Image.open(out) as img:
            self.assertEqual(img.mode, "RGB")

    def test_missing_pil_raises(self):
        with unittest.mock.patch.object(figures, "PIL_AVAILABLE", False):
            with self.assertRaises(figures.FigureError):
                figures.render_figure(self.dir / "x.png", self.dir / "y.jpg")


class TestFont(unittest.TestCase):
    @unittest.skipUnless(PIL_OK, "需要 Pillow")
    def test_font_spec_valid(self):
        if FONT is None:
            self.skipTest("本机没有可用中文字体")
        self.assertTrue(Path(FONT.path).exists())
        self.assertTrue(FONT.family)

    @unittest.skipUnless(PIL_OK, "需要 Pillow")
    def test_prefers_simplified_chinese_face(self):
        """NotoSansCJK/STHeiti 这类 TTC 的 index=0 是 JP/TC，必须扫到 SC 字面。"""
        if FONT is None:
            self.skipTest("本机没有可用中文字体")
        known_sc = any(Path(p).exists() for p in (
            "/System/Library/Fonts/STHeiti Light.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ))
        if not known_sc:
            self.skipTest("本机没有已知的 SC TTC 可验证")
        self.assertIn("SC", FONT.family)

    @unittest.skipUnless(PIL_OK, "需要 Pillow")
    def test_gzh_font_override_and_heiti_index(self):
        heiti = Path("/System/Library/Fonts/STHeiti Light.ttc")
        if not heiti.exists():
            self.skipTest("本机没有 STHeiti Light.ttc")
        spec = figures.find_cjk_font(env={"GZH_FONT": str(heiti)})
        self.assertIsNotNone(spec)
        self.assertEqual(Path(spec.path), heiti)
        self.assertIn("SC", spec.family)
        self.assertEqual(spec.index, 1, "index=0 是 Heiti TC，必须取 index=1 的 Heiti SC")


class TestRenderCover(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (PIL_OK and FONT):
            raise unittest.SkipTest("需要 Pillow + 中文字体")
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls._tmp.name)
        cls.frame = cls.dir / "frame.png"
        figures.Image.new("RGB", (1280, 720), (190, 190, 190)).save(cls.frame)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_size_and_two_lines(self):
        out = self.dir / "cover.jpg"
        meta = figures.render_cover(self.frame, out, "门槛降到写网文，产品却更难被看见", FONT,
                                    footer="DSH 大肥鱼 · 原视频 00:07")
        self.assertEqual(meta["size"], (900, 383))
        self.assertEqual(meta["title_lines"], ["门槛降到写网文", "产品却更难被看见"])
        with figures.Image.open(out) as img:
            self.assertEqual(img.size, (900, 383))
            self.assertEqual(img.mode, "RGB")
            self.assertTrue(img.info.get("progressive"))

    def test_bottom_is_darkened(self):
        out = self.dir / "cover_grad.jpg"
        figures.render_cover(self.frame, out, "门槛降到写网文，产品却更难被看见", FONT)
        with figures.Image.open(out) as img:
            gray = img.convert("L").load()
            top = statistics.mean(gray[x, y] for x in range(0, 900, 30) for y in range(0, 80, 10))
            bottom = statistics.mean(gray[x, y] for x in range(0, 900, 30)
                                     for y in range(300, 380, 10))
        self.assertGreater(top - bottom, 40, f"底部应渐变压暗：top={top} bottom={bottom}")

    def test_title_text_is_drawn(self):
        out = self.dir / "cover_text.jpg"
        figures.render_cover(self.frame, out, "门槛降到写网文，产品却更难被看见", FONT)
        with figures.Image.open(out) as img:
            gray = img.convert("L").load()
            white = sum(1 for x in range(0, 900, 2) for y in range(120, 360, 2)
                        if gray[x, y] > 230)
        self.assertGreater(white, 100, "标题白字应被画出来")

    def test_long_title_shrinks_to_fit(self):
        out = self.dir / "cover_long.jpg"
        title = "一二三四五六七八九十" * 6          # 60 字，必须缩字号才放得下
        meta = figures.render_cover(self.frame, out, title, FONT)
        self.assertLessEqual(meta["font_size"], 30)
        self.assertEqual(len(meta["title_lines"]), 2)
        face = figures.load_font(FONT, meta["font_size"])
        self.assertTrue(all(face.getlength(line) <= 900 - 2 * 54
                            for line in meta["title_lines"]))

    def test_empty_title_raises(self):
        with self.assertRaises(figures.FigureError):
            figures.render_cover(self.frame, self.dir / "x.jpg", "  ", FONT)




class TestFrameQuality(unittest.TestCase):
    """黑帧 / 空镜守卫：writer 引语落在转场黑场时，不能把黑图插进正文。"""

    def setUp(self):
        if not figures.PIL_AVAILABLE:
            self.skipTest("Pillow 不可用")
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _make(self, name, pixels, bg=(0, 0, 0)):
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (320, 180), bg)
        d = ImageDraw.Draw(img)
        for i, (x, y, c) in enumerate(pixels):
            d.rectangle([x, y, x + 40, y + 40], fill=c)
        path = self.dir / name
        img.save(path, "JPEG", quality=90)
        return path

    def test_dark_frame_rejected(self):
        dark = self._make("dark.jpg", [])
        mean, std = figures.frame_stats(dark)
        self.assertLess(mean, 10)
        self.assertFalse(figures.frame_is_usable(dark))

    def test_uniform_frame_rejected(self):
        from PIL import Image
        path = self.dir / "flat.jpg"
        Image.new("RGB", (320, 180), (128, 128, 128)).save(path, "JPEG", quality=90)
        self.assertFalse(figures.frame_is_usable(path))

    def test_normal_frame_accepted(self):
        colorful = self._make("ok.jpg", [
            (10, 10, (240, 200, 120)), (80, 20, (60, 90, 160)),
            (150, 60, (200, 80, 60)), (220, 100, (90, 200, 140)),
            (40, 120, (230, 230, 230)), (260, 40, (30, 30, 30)),
        ], bg=(110, 120, 135))
        self.assertTrue(figures.frame_is_usable(colorful))

    def test_unreadable_frame_is_not_blocking(self):
        bad = self.dir / "broken.jpg"
        bad.write_bytes(b"not an image")
        self.assertTrue(figures.frame_is_usable(bad), "判断不了时不应误杀")

if __name__ == "__main__":
    unittest.main(verbosity=2)
