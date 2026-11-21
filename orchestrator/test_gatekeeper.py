#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门禁判定契约测试（T1）：python3 -m unittest orchestrator.test_gatekeeper -v

覆盖：
- verdict.parse_report 的三值解析与「未通过/未完成」子串陷阱；
- gatekeeper 对 PASS / FAIL / INCOMPLETE / 报告缺失 / 报告无判定码 的分派；
- 旧报告（只有「总判定：未通过（需修正）」，没有判定码）仍能正确打回；
- 端到端：`gzh_pipeline.py --skip-render` 必须退出码 3 且报告写 `判定码：INCOMPLETE`，
  不能像修复前那样写「总判定：通过」。

全部产物落在临时目录，不写 cases/ 与真实案例目录。
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestrator.agents.base import Context  # noqa: E402
from orchestrator.agents.gatekeeper import Gatekeeper  # noqa: E402
from orchestrator.state import Run  # noqa: E402
from orchestrator.verdict import (  # noqa: E402
    FAIL, INCOMPLETE, LEGACY_FAIL, PASS, parse_report,
)

# 质检通过、且渲染真的跑过（三档都有结果）的报告
PASS_REPORT = """# 质检报告：测试_v1

- 生成时间：2026-11-09 00:00
- 总判定：**通过**
- 判定码：PASS

## 质检门禁

| 检查项 | 结果 | 详情 |
|---|---|---|
| HTML 硬规则门禁 | ✅ | 8+ 项门禁全过 |
| 禁用词/句式 | ✅ | 高危词、句式黑名单均未命中 |

## 渲染验收

| 视口 | 页高 | 横向溢出 | 图片比例 | 截图 |
|---|---|---|---|---|
| 375px | 1200px | 无 | 正常 | ✓ |
| 390px | 1180px | 无 | 正常 | ✓ |
| 430px | 1150px | 无 | 正常 | ✓ |
"""

FAIL_REPORT = """# 质检报告：测试_v1

- 生成时间：2026-11-09 00:00
- 总判定：**未通过（需修正）**
- 判定码：FAIL

## 质检门禁

| 检查项 | 结果 | 详情 |
|---|---|---|
| HTML 硬规则门禁 | ❌ | 残留 div（公众号会整段吞掉） |
| 禁用词/句式 | ❌ | 赋能 |

## 渲染验收

| 视口 | 页高 | 横向溢出 | 图片比例 | 截图 |
|---|---|---|---|---|
| 375px | 1200px | 无 | 正常 | ✗ |
"""

INCOMPLETE_REPORT = """# 质检报告：测试_v1

- 生成时间：2026-11-09 00:00
- 总判定：**未完成（渲染验收未执行）**
- 判定码：INCOMPLETE

## 质检门禁

| 检查项 | 结果 | 详情 |
|---|---|---|
| HTML 硬规则门禁 | ✅ | 8+ 项门禁全过 |

## 渲染验收

已跳过（--skip-render）。**渲染验收未执行，本报告不能作为放行依据。**
"""

# 旧口径报告：T1 之前 pipeline 只写这一行，没有判定码
LEGACY_FAIL_REPORT = """# 质检报告：测试_v1

- 生成时间：2026-11-06 12:56
- 总判定：**未通过（需修正）**

## 质检门禁

| 检查项 | 结果 | 详情 |
|---|---|---|
| HTML 硬规则门禁 | ❌ | 残留 div |
"""

# 历史假通过报告（无判定码）：pipeline 跳过渲染却写「总判定：通过」。
# 这种报告必须解析成 INCOMPLETE，否则门禁的"可信度"补丁对存量报告无效。
LEGACY_SKIP_REPORT = """# 质检报告：测试_v1

- 生成时间：2026-11-06 12:56
- 总判定：**通过**

## 质检门禁

| 检查项 | 结果 | 详情 |
|---|---|---|
| HTML 硬规则门禁 | ✅ | 8+ 项门禁全过 |

## 渲染验收

已跳过（--skip-render）。
"""

NO_VERDICT_REPORT = """# 质检报告：测试_v1

- 生成时间：2026-11-09 00:00

## 质检门禁

| 检查项 | 结果 | 详情 |
|---|---|---|
| HTML 硬规则门禁 | ✅ | 8+ 项门禁全过 |
"""


class TestParseReport(unittest.TestCase):
    def test_three_values(self):
        self.assertEqual(parse_report(PASS_REPORT), PASS)
        self.assertEqual(parse_report(FAIL_REPORT), FAIL)
        self.assertEqual(parse_report(INCOMPLETE_REPORT), INCOMPLETE)

    def test_substring_trap(self):
        """「未通过」「未完成」都含子串「通过」，必须先判否定式。"""
        self.assertEqual(parse_report("- 总判定：**未通过（需修正）**"), FAIL)
        self.assertEqual(parse_report("- 总判定：**未完成（渲染验收未执行）**"), INCOMPLETE)
        self.assertNotEqual(parse_report("- 总判定：**未完成（渲染验收未执行）**"), PASS)

    def test_legacy_values(self):
        self.assertEqual(parse_report(f"- 总判定：{LEGACY_FAIL}"), FAIL)
        self.assertEqual(parse_report("- 总判定：**通过**"), PASS)

    def test_code_wins_over_total(self):
        text = "- 总判定：**未通过（需修正）**\n- 判定码：PASS\n"
        self.assertEqual(parse_report(text), PASS)

    def test_legacy_skip_render_is_incomplete(self):
        """旧报告「总判定：通过」+ 跳过渲染 → 必须判 INCOMPLETE（T1 假通过根因）。"""
        self.assertEqual(parse_report(LEGACY_SKIP_REPORT), INCOMPLETE)
        # 确定性坏（未通过）优先于"没跑完"
        self.assertEqual(
            parse_report("- 总判定：**未通过（需修正）**\n已跳过（--skip-render）。"), FAIL)
        # 旧报告里「渲染验收失败或未执行」也不能算通过
        self.assertEqual(
            parse_report("- 总判定：**通过**\n渲染验收失败或未执行。"), INCOMPLETE)

    def test_unparseable(self):
        self.assertIsNone(parse_report(NO_VERDICT_REPORT))
        self.assertIsNone(parse_report(""))
        self.assertIsNone(parse_report(None))


class TestGatekeeper(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.case_dir = Path(self._tmp.name) / "01_测试嘉宾_案例"
        self.case_dir.mkdir()
        self.run = Run(run_id="gate-test")
        self.ctx = Context(run=self.run, case_dir=self.case_dir,
                           orch_dir=ROOT / "orchestrator", provider=None)

    def _report(self, text: str, *, relative: bool = True, name: str = "测试_v1_质检报告.md") -> Path:
        path = self.case_dir / name
        path.write_text(text, encoding="utf-8")
        # 新契约存相对 case_dir 的路径；这里顺带验证 base.artifact_path 能解析
        self.run.artifacts["qc_report"] = name if relative else str(path)
        return path

    def test_pass_goes_qc_passed(self):
        self._report(PASS_REPORT)
        result = Gatekeeper().run(self.ctx)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.to, "qc_passed")

    def test_fail_rework_with_detail(self):
        self._report(FAIL_REPORT)
        result = Gatekeeper().run(self.ctx)
        self.assertEqual(result.status, "rework")
        self.assertIn("2 项 FAIL", result.reason)
        self.assertIn("HTML 硬规则门禁", result.reason)
        self.assertIsNone(result.to)

    def test_incomplete_blocked_not_passed(self):
        """渲染没跑 = 未完成，必须 blocked，绝不能变成通过/打回。"""
        self._report(INCOMPLETE_REPORT)
        result = Gatekeeper().run(self.ctx)
        self.assertEqual(result.status, "blocked")
        self.assertIn("渲染验收未完成", result.reason)
        self.assertIsNone(result.to)

    def test_missing_report_rework(self):
        result = Gatekeeper().run(self.ctx)
        self.assertEqual(result.status, "rework")
        self.assertIn("找不到质检报告", result.reason)

    def test_report_without_verdict_blocked(self):
        self._report(NO_VERDICT_REPORT)
        result = Gatekeeper().run(self.ctx)
        self.assertEqual(result.status, "blocked")
        self.assertIn("不可信", result.reason)

    def test_legacy_fail_report_still_reworked(self):
        """旧报告只有「总判定：未通过（需修正）」，修复前会解析成 None。"""
        self._report(LEGACY_FAIL_REPORT)
        result = Gatekeeper().run(self.ctx)
        self.assertEqual(result.status, "rework")
        self.assertIn("HTML 硬规则门禁", result.reason)

    def test_legacy_skip_render_blocked(self):
        """存量假通过报告：gatekeeper 必须 blocked，不得 qc_passed。"""
        self._report(LEGACY_SKIP_REPORT)
        result = Gatekeeper().run(self.ctx)
        self.assertEqual(result.status, "blocked")
        self.assertIn("渲染验收未完成", result.reason)
        self.assertIsNone(result.to)

    def test_absolute_artifact_path_compat(self):
        """旧 run 的 artifacts 可能是绝对路径，仍要能读到。"""
        self._report(PASS_REPORT, relative=False)
        result = Gatekeeper().run(self.ctx)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.to, "qc_passed")

    def test_qc_passed_advances_to_publish_gate(self):
        self.run.stage = "qc_passed"
        result = Gatekeeper().run(self.ctx)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.to, "awaiting_publish")


# 最小可过质检的稿子（无禁用词/无占位符/无图片问题）
MIN_MD = """# 测试稿：这是一句判断标题

## 01｜入门岗位先被重新定价

**结论：入门岗位先被重新定价。**

正文一段，讲讲具体发生了什么。

## 02｜任务拆解比岗位名更重要

**结论：任务拆解比岗位名更重要。**

正文一段，讲讲为什么。

## 03｜学习速度决定议价能力

**结论：学习速度决定议价能力。**

正文一段，收个尾。
"""

# 命中高危禁用词 → 质检门禁 FAIL
BAD_MD = MIN_MD + "\n这套方法赋能了整个团队。\n"


class TestRealLegacyReport(unittest.TestCase):
    """仓库内真实历史报告回归：case 16 的「总判定：通过 + 已跳过渲染」。"""

    def test_case16_false_pass_now_incomplete(self):
        report = ROOT / "cases" / "16_苏姿丰回归_案例" / "苏姿丰回归_v1_质检报告.md"
        if not report.exists():  # 精简 checkout 可能没有 cases/
            self.skipTest("缺少案例文件")
        self.assertEqual(parse_report(report.read_text(encoding="utf-8")), INCOMPLETE)


class TestPipelineVerdict(unittest.TestCase):
    """直接跑流水线，验证退出码与报告的判定码契约。"""

    def _run_pipeline(self, tmp: Path, md_text: str, name: str = "gate_test") -> subprocess.CompletedProcess:
        md = tmp / f"{name}.md"
        md.write_text(md_text, encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(ROOT / "流水线" / "gzh_pipeline.py"),
             "--md", str(md), "--name", name, "--outdir", str(tmp), "--skip-render"],
            capture_output=True, text=True, timeout=300,
            cwd=str(ROOT),
        )

    def test_skip_render_exit_3_and_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            proc = self._run_pipeline(tmp_path, MIN_MD)
            self.assertEqual(proc.returncode, 3,
                             f"stdout={proc.stdout[-2000:]}\nstderr={proc.stderr[-1000:]}")
            report_path = tmp_path / "gate_test_质检报告.md"
            self.assertTrue(report_path.exists(), "质检报告必须生成")
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("- 判定码：INCOMPLETE", report)
            self.assertIn("总判定：**未完成（渲染验收未执行）**", report)
            # 关键回归：不得再出现「已跳过渲染 + 总判定通过」的假通过
            self.assertNotIn("总判定：**通过**", report)
            self.assertEqual(parse_report(report), INCOMPLETE)

    def test_qa_fail_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            proc = self._run_pipeline(tmp_path, BAD_MD, name="gate_bad")
            self.assertEqual(proc.returncode, 1,
                             f"stdout={proc.stdout[-2000:]}\nstderr={proc.stderr[-1000:]}")
            report = (tmp_path / "gate_bad_质检报告.md").read_text(encoding="utf-8")
            self.assertIn("- 判定码：FAIL", report)
            self.assertEqual(parse_report(report), FAIL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
