#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""oneshot 一键入口测试：python3 -m unittest orchestrator.test_oneshot -v

全部在临时目录里跑：--runs-dir / --base 指向 tmp，外部排版流水线用桩脚本顶替
（真流水线要 Chrome 渲染验收，单测不依赖本机浏览器），LLM 用 stub provider。
覆盖：到达 awaiting_publish、导入包 zip、render 开关、inbox 清理、blocked/超时、
配置错误退出码、幂等重跑、缺件跳过。
"""

from __future__ import annotations

import contextlib
import html
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import orchestrator.agents as agents_module          # noqa: E402
from orchestrator import loop, oneshot               # noqa: E402
from orchestrator.agents.base import Context, Result  # noqa: E402
from orchestrator.agents.writer import Writer        # noqa: E402
from orchestrator.state import Run, RunStore         # noqa: E402

# 冒充 流水线/gzh_pipeline.py：产出双 HTML + PASS 质检报告，并记录调用参数。
FAKE_PIPELINE = '''#!/usr/bin/env python3
"""测试桩：冒充 gzh_pipeline.py，不渲染、直接给 PASS 判定码。"""
import argparse
import json
import pathlib

ap = argparse.ArgumentParser()
ap.add_argument("--md")
ap.add_argument("--name")
ap.add_argument("--outdir")
ap.add_argument("--card", default="")
ap.add_argument("--skip-render", action="store_true")
args = ap.parse_args()
out = pathlib.Path(args.outdir)
out.mkdir(parents=True, exist_ok=True)
(out / (args.name + "_导入片段.html")).write_text("<section>import</section>", encoding="utf-8")
(out / (args.name + "_排版预览.html")).write_text("<html>preview</html>", encoding="utf-8")
(out / (args.name + "_质检报告.md")).write_text(
    "# 质检报告\\n\\n- 判定码：PASS\\n\\n- 总判定：**通过**\\n", encoding="utf-8")
pathlib.Path("pipeline_args.json").write_text(json.dumps({
    "skip_render": args.skip_render, "name": args.name, "md": args.md,
}), encoding="utf-8")
'''

FEISHU_PARAS = [
    "@说话人 1  00:01",
    "大家好，欢迎来到本期节目，我是主持人小雨。今天请到的嘉宾是杨攀。",
    "@说话人 2  00:28",
    "大家好，我是杨攀，在 DSH 做产品。",
]


def make_docx(path: Path, paragraphs: list[str]) -> Path:
    """构造最小 .docx（只要 word/document.xml），不依赖 python-docx。"""
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{html.escape(p)}</w:t></w:r></w:p>'
        for p in paragraphs)
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f'<w:body>{body}</w:body></w:document>')
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", xml)
    return path


class NoopIllustrator:
    """配图桩：本测试不依赖配图，只要把状态推进到 illustrated。"""

    name = "illustrator"
    stage = "critiqued"

    def run(self, ctx) -> Result:
        return Result.ok("配图占位（测试桩）", to="illustrated")


class OneshotCase(unittest.TestCase):
    """临时仓库：orchestrator/ + 流水线/ + base/ + runs/ 全在 tmp 里。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.pkg = self.root / "pkg"
        self.here = self.pkg / "orchestrator"
        self.here.mkdir(parents=True)
        (self.here / "inbox").mkdir()
        (self.pkg / "流水线").mkdir()
        (self.pkg / "流水线" / "gzh_pipeline.py").write_text(FAKE_PIPELINE,
                                                            encoding="utf-8")
        self.base = self.root / "base"
        self.base.mkdir()
        self.runs = self.root / "runs"
        self.video = self.root / "v.mp4"
        self.video.write_bytes(b"\x00" * 2048)
        self.transcript = make_docx(self.root / "转写.docx", FEISHU_PARAS)
        self.agents = {**agents_module.AGENTS, "illustrator": NoopIllustrator()}

    # -------------------------------------------------- 工具
    def run_oneshot(self, *extra: str, video: Path | None = None,
                    transcript: Path | None = None, title: str = "DSH 大肥鱼"):
        argv = [
            "--video", str(video or self.video),
            "--transcript", str(transcript or self.transcript),
            "--title", title,
            "--runs-dir", str(self.runs),
            "--base", str(self.base),
            *extra,
        ]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(loop, "HERE", self.here), \
                patch.object(loop, "BASE", self.base), \
                patch.object(loop, "load_policy",
                             lambda: {"auto_approve": {"topic": True}}), \
                patch.object(loop, "AGENTS", self.agents):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = oneshot.main(argv)
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def summary(output: str) -> dict:
        idx = output.find('{\n  "ok"')
        if idx < 0:                                 # pragma: no cover - 断言失败时好排查
            raise AssertionError(f"输出里没有 JSON 摘要：\n{output}")
        return json.loads(output[idx:])

    def case_dir(self) -> Path:
        dirs = [p for p in self.base.iterdir() if p.is_dir()]
        self.assertEqual(len(dirs), 1, f"案例目录应只有一个：{dirs}")
        return dirs[0].resolve()      # macOS 的 /var 是 /private/var 软链，统一成真实路径


# ---------------------------------------------------------------- 全链路


class TestFullChain(OneshotCase):
    def test_reaches_awaiting_publish_and_builds_zip(self):
        code, out, err = self.run_oneshot("--guest", "杨攀")
        self.assertEqual(code, 0, out + err)
        data = self.summary(out)
        self.assertTrue(data["ok"])
        self.assertEqual(data["stage"], "awaiting_publish")
        for key in ("run_id", "stage", "case_dir", "zip", "elapsed_sec", "usage"):
            self.assertIn(key, data)
        self.assertGreaterEqual(len(data["steps"]), 10)
        self.assertIn("awaiting_publish", data["steps"][-1])

        run = RunStore(self.runs).get(data["run_id"])
        self.assertEqual(run.stage, "awaiting_publish")
        self.assertEqual(run.status, "active")
        self.assertIn("import", run.artifacts)
        self.assertIn("preview", run.artifacts)
        self.assertIn("qc_report", run.artifacts)
        self.assertIn("speaker_map", run.artifacts, "转写阶段应登记说话人对照产物")

        case = Path(data["case_dir"])
        self.assertEqual(case, self.case_dir())
        self.assertIn("杨攀", case.name, "--guest 应覆盖案例目录名")
        self.assertEqual((case / "媒体路径.txt").read_text(encoding="utf-8").strip(),
                         str(self.video.resolve()))
        self.assertTrue((case / "转写" / "transcript.json").exists(),
                        ".docx 附件应被转成 transcript.json")
        self.assertTrue(list(case.glob("说话人对照_*.md")), "应写说话人对照 md")

        # 导入包：有就打包，缺的不报错
        zip_path = Path(data["zip"])
        self.assertTrue(zip_path.exists())
        self.assertEqual(zip_path.parent, case)
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
        for expected in ("导入说明.md", "导入片段.html", "预览.html", "质检报告.md"):
            self.assertIn(expected, names)
        self.assertNotIn("封面.jpg", names, "缺封面应跳过，不能塞空文件")
        readme = (case / "导入说明.md").read_text(encoding="utf-8")
        self.assertIn("导入步骤", readme)
        self.assertIn("缺失（已跳过", readme)

        # 默认 render 开：桩流水线应收到没有 --skip-render 的调用
        pipeline_args = json.loads((case / "pipeline_args.json").read_text(encoding="utf-8"))
        self.assertFalse(pipeline_args["skip_render"])
        # 默认 ingest 后清理 inbox
        inbox_files = list((self.here / "inbox" / "oneshot").glob("*")) \
            if (self.here / "inbox" / "oneshot").exists() else []
        self.assertEqual(inbox_files, [])

    def test_no_render_flag(self):
        code, out, err = self.run_oneshot("--no-render")
        self.assertEqual(code, 0, out + err)
        case = self.case_dir()
        pipeline_args = json.loads((case / "pipeline_args.json").read_text(encoding="utf-8"))
        self.assertTrue(pipeline_args["skip_render"], "--no-render 应传给排版流水线")

    def test_keep_inbox(self):
        code, out, err = self.run_oneshot("--keep-inbox", "--max-ticks", "300")
        self.assertEqual(code, 0, out + err)
        box = self.here / "inbox" / "oneshot"
        files = sorted(p.name for p in box.iterdir())
        self.assertEqual(len(files), 2, f"应保留主 md + 转写附件：{files}")
        self.assertTrue(any(name.endswith(".md") for name in files))
        self.assertTrue(any(name.endswith(".docx") for name in files))
        md = next(p for p in box.iterdir() if p.suffix == ".md")
        text = md.read_text(encoding="utf-8")
        self.assertIn("DSH 大肥鱼", text)
        self.assertIn("来源：视频", text)
        self.assertIn("转写：", text)

    def test_rerun_same_title_reuses_run(self):
        code1, out1, err1 = self.run_oneshot()
        self.assertEqual(code1, 0, out1 + err1)
        first = self.summary(out1)

        code2, out2, err2 = self.run_oneshot()
        self.assertEqual(code2, 0, out2 + err2)
        second = self.summary(out2)
        self.assertEqual(second["run_id"], first["run_id"], "同标题应复用 run（幂等）")
        self.assertEqual(second["steps"], [])
        self.assertEqual(len(list(self.runs.glob("*/state.json"))), 1)
        self.assertTrue(Path(second["zip"]).exists())

    def test_srt_transcript_input(self):
        srt = self.root / "转写.srt"
        srt.write_text(
            "1\n00:00:01,000 --> 00:00:03,500\n说话人 1: 我是主持人小雨。\n\n"
            "2\n00:00:04,000 --> 00:00:06,000\n说话人 2: 我是杨攀。\n",
            encoding="utf-8")
        code, out, err = self.run_oneshot(transcript=srt)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.summary(out)["stage"], "awaiting_publish")
        segments = json.loads(
            (self.case_dir() / "转写" / "transcript.json").read_text(encoding="utf-8"))
        self.assertEqual(segments["count"], 2)
        self.assertEqual(segments["segments"][0]["speaker"], "说话人 1")

    def test_json_transcript_input(self):
        payload = {"source": "外部转写", "count": 2, "duration": 6.0, "segments": [
            {"start": 0.0, "end": 3.0, "speaker": "说话人 1", "text": "我是主持人小雨。"},
            {"start": 3.0, "end": 6.0, "speaker": "说话人 2", "text": "我是杨攀。"},
        ]}
        path = self.root / "外部转写.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        code, out, err = self.run_oneshot(transcript=path)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.summary(out)["stage"], "awaiting_publish")


# ---------------------------------------------------------------- 失败路径


class TestBlockedPaths(OneshotCase):
    def test_empty_transcript_blocks_with_exit_3(self):
        empty = make_docx(self.root / "空.docx", ["", "   "])
        code, out, err = self.run_oneshot(transcript=empty)
        self.assertEqual(code, 3, out + err)
        data = self.summary(out)
        self.assertFalse(data["ok"])
        self.assertIn("blocked", data["reason"])
        self.assertEqual(data["stage"], "triaged")

    def test_max_ticks_timeout_exits_3(self):
        code, out, err = self.run_oneshot("--max-ticks", "1")
        self.assertEqual(code, 3, out + err)
        data = self.summary(out)
        self.assertFalse(data["ok"])
        self.assertIn("max-ticks", data["reason"])
        self.assertEqual(data["stage"], "triaged")

    def test_human_gate_not_auto_approved_exits_3(self):
        """policy 关掉 topic 自动放行时，停在 triaged 人工闸门，不能硬推。"""
        argv = ["--video", str(self.video), "--transcript", str(self.transcript),
                "--title", "闸门测试", "--runs-dir", str(self.runs), "--base", str(self.base)]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(loop, "HERE", self.here), \
                patch.object(loop, "BASE", self.base), \
                patch.object(loop, "load_policy", lambda: {"auto_approve": {}}), \
                patch.object(loop, "AGENTS", self.agents):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = oneshot.main(argv)
        self.assertEqual(code, 3, out.getvalue() + err.getvalue())
        data = self.summary(out.getvalue())
        self.assertIn("人工闸门", data["reason"])
        self.assertEqual(data["stage"], "triaged")
        self.assertEqual(data["zip"], "")


class TestConfigErrors(OneshotCase):
    def test_missing_video(self):
        code, out, err = self.run_oneshot(video=self.root / "不存在.mp4")
        self.assertEqual(code, 2)
        self.assertIn("视频不存在", err)

    def test_missing_transcript(self):
        code, out, err = self.run_oneshot(transcript=self.root / "不存在.docx")
        self.assertEqual(code, 2)
        self.assertIn("转写文档不存在", err)

    def test_unsupported_transcript_suffix(self):
        pdf = self.root / "转写.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        code, out, err = self.run_oneshot(transcript=pdf)
        self.assertEqual(code, 2)
        self.assertIn("不支持的转写格式", err)

    def test_broken_docx(self):
        bad = self.root / "坏.docx"
        bad.write_bytes(b"not a zip")
        code, out, err = self.run_oneshot(transcript=bad)
        self.assertEqual(code, 2)
        self.assertIn("解析失败", err)

    def test_unknown_provider(self):
        code, out, err = self.run_oneshot("--provider", "nope")
        self.assertEqual(code, 2)
        self.assertIn("provider", err)

    def test_bad_max_ticks(self):
        code, out, err = self.run_oneshot("--max-ticks", "0")
        self.assertEqual(code, 2)
        self.assertIn("max-ticks", err)


# ---------------------------------------------------------------- 导入包


class TestImportZip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.case = Path(self._tmp.name) / "01_张三_案例"
        self.case.mkdir()
        self.run = Run(run_id="abc123", stage="awaiting_publish", case_dir=self.case.name)

    def names(self, zip_path: Path) -> set[str]:
        with zipfile.ZipFile(zip_path) as zf:
            return set(zf.namelist())

    def test_missing_everything_still_builds_readme_only_zip(self):
        self.run.artifacts = {"import": "不存在_导入片段.html"}
        zip_path = oneshot.build_import_zip(self.case, self.run)
        self.assertTrue(zip_path.exists())
        self.assertEqual(self.names(zip_path), {"导入说明.md"})
        readme = (self.case / "导入说明.md").read_text(encoding="utf-8")
        self.assertIn("缺失（已跳过", readme)
        self.assertIn("导入片段", readme)

    def test_artifacts_and_fallback_globs(self):
        (self.case / "张三_v1_导入片段.html").write_text("<section>x</section>", encoding="utf-8")
        (self.case / "张三_v1_排版预览.html").write_text("<html>x</html>", encoding="utf-8")
        (self.case / "张三_v1_质检报告.md").write_text("- 判定码：PASS\n", encoding="utf-8")
        (self.case / "封面对.jpg").write_bytes(b"jpg")
        (self.case / "图源_2026-11-12_张三.md").write_text("# 图源\n", encoding="utf-8")
        (self.case / "5个备选标题.md").write_text("# 标题\n", encoding="utf-8")
        images = self.case / "图片"
        images.mkdir()
        (images / "fig_01.jpg").write_bytes(b"jpg")
        self.run.artifacts = {"import": "张三_v1_导入片段.html"}
        zip_path = oneshot.build_import_zip(self.case, self.run)
        names = self.names(zip_path)
        for expected in ("导入说明.md", "导入片段.html", "预览.html", "质检报告.md",
                         "封面.jpg", "图源.md", "备选标题.md", "图片/fig_01.jpg"):
            self.assertIn(expected, names)
        readme = (self.case / "导入说明.md").read_text(encoding="utf-8")
        self.assertNotIn("缺失（已跳过", readme)

    def test_artifact_absolute_path_outside_case(self):
        outside = Path(self._tmp.name) / "外部_导入片段.html"
        outside.write_text("<section>x</section>", encoding="utf-8")
        self.run.artifacts = {"import": str(outside)}
        zip_path = oneshot.build_import_zip(self.case, self.run)
        self.assertIn("导入片段.html", self.names(zip_path))


# ---------------------------------------------------------------- writer 接线


class RecordingProvider:
    """记录 prompt 的桩 provider：验证 writer 把说话人对照拼进了上下文。"""

    name = "recording"

    def __init__(self):
        self.prompts: list[str] = []

    def complete(self, *, task: str, system: str, prompt: str,
                 max_tokens: int = 2000, temperature: float = 0.7) -> str:
        self.prompts.append(prompt)
        return "[STUB] 正文占位"


class TestWriterContext(unittest.TestCase):
    def test_speaker_map_is_prepended_to_writer_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = root / "01_张三_案例"
            case.mkdir()
            (case / "说话人对照_2026-11-12_张三.md").write_text(
                "| 标签 | 姓名 | 缩写 | 依据 |\n|---|---|---|---|\n"
                "| 说话人 1 | 张三 | ZS | 自我介绍 |\n", encoding="utf-8")
            provider = RecordingProvider()
            ctx = Context(run=Run(run_id="abc123"), case_dir=case,
                          orch_dir=root / "orchestrator", provider=provider,
                          dry_run=False, render=False)
            result = Writer().run(ctx)
            self.assertEqual(result.status, "ok")
            self.assertTrue(provider.prompts, "writer 应调用模型")
            self.assertIn("说话人对照", provider.prompts[0],
                          "gather_context 前缀应包含说话人对照 md")


if __name__ == "__main__":
    unittest.main(verbosity=2)
