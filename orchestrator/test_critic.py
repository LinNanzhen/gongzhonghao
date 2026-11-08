#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""人味检查与体检表机器判定的单元测试（依据 模板/人味检查表.md）。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.agents.base import Context  # noqa: E402
from orchestrator.agents.critic import Critic, judge_section, scan_humanize  # noqa: E402
from orchestrator.state import Run  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class TestHardFails(unittest.TestCase):
    def test_meaning_inflation(self):
        fails, _ = scan_humanize("这份论文的发表，标志着就业研究进入新阶段。")
        self.assertTrue(any("意义拔高" in f for f in fails))

    def test_closing_cliche(self):
        fails, _ = scan_humanize("让我们拭目以待，看 AI 将把人类带向何方。")
        self.assertTrue(any("套话收尾" in f for f in fails))

    def test_ai_buzzwords(self):
        for word in ("赋能", "抓手", "底层逻辑", "颗粒度", "势能", "破局"):
            fails, _ = scan_humanize(f"这套方法{word}了团队。")
            self.assertTrue(any("AI 高频词" in f for f in fails), word)


class TestWarns(unittest.TestCase):
    def test_false_contrast(self):
        _, warns = scan_humanize("这不仅是更新，更是一次效率的飞跃。")
        self.assertTrue(any("假对比" in w for w in warns))

    def test_fake_depth(self):
        _, warns = scan_humanize("这背后是整个人类与技术关系的重构。")
        self.assertTrue(any("伪深度" in w for w in warns))

    def test_vague_authority(self):
        _, warns = scan_humanize("专家认为这一设计可能减少误操作。")
        self.assertTrue(any("借笼统权威" in w for w in warns))

    def test_warns_do_not_block(self):
        fails, warns = scan_humanize("专家认为这很重要。")
        self.assertEqual(fails, [])
        self.assertTrue(warns)


class TestNoFalsePositive(unittest.TestCase):
    def test_quote_block_ignored(self):
        text = "> 这标志着新时代的到来——受访者原话\n\n正文是干净的。"
        fails, _ = scan_humanize(text)
        self.assertEqual(fails, [], "引文块不算作者笔法")

    def test_clean_text(self):
        text = (
            "## 01｜先被重新定价的是入门岗位\n\n"
            "**这一句是本节结论。**\n\n"
            "他把例子讲得很具体：放射科医生做 26 项任务，读片只是其中一项。\n"
        )
        fails, warns = scan_humanize(text)
        self.assertEqual((fails, warns), ([], []))

    def test_legitimate_closed_loop(self):
        """闭环在控制系统语境合法，不应进入硬伤。"""
        fails, _ = scan_humanize("控制器使用闭环反馈调整输出。")
        self.assertEqual(fails, [])


class TestSectionJudgement(unittest.TestCase):
    """T1b：可复述 / 有证据 两列必须是真机器判定，不能恒真。"""

    def test_bold_judgement_title_is_retellable(self):
        j = judge_section("入门岗位先被重新定价", "**结论：入门岗位先被重新定价。**")
        self.assertTrue(j["retell"])
        self.assertFalse(j["evidence"])   # 无时间戳/引用/原话

    def test_topic_word_title_not_retellable(self):
        j = judge_section("就业现状", "**结论：就业现状很严峻。**")
        self.assertFalse(j["retell"])
        self.assertEqual(j["topic_word"], "现状")

    def test_plain_prose_not_retellable(self):
        j = judge_section("入门岗位先被重新定价", "这一段没有加粗，只是平铺直叙。")
        self.assertFalse(j["retell"])

    def test_evidence_sources(self):
        self.assertTrue(judge_section("判断标题", "他讲到 12:34 时停了一下。")["evidence"])
        self.assertTrue(judge_section("判断标题", "据 [2]，任务被拆成 26 项。")["evidence"])
        self.assertTrue(judge_section("判断标题", "他说「这句话是受访者原话」。" )["evidence"])
        self.assertFalse(judge_section("判断标题", "这里全是作者的转述，没有出处。")["evidence"])

    def test_quote_block_not_counted_as_bold_judgement(self):
        """引文块里的加粗不算作者判断句。"""
        j = judge_section("入门岗位先被重新定价", "> **受访者原话被加粗了**\n")
        self.assertFalse(j["retell"])


CLEAN_NO_BOLD = """# 测试稿：一篇没有加粗也没有证据的稿子

## 01｜入门岗位先被重新定价

入门岗位先被重新定价，这是一段转述，没有原话也没有时间戳。

## 02｜任务拆解比岗位名更重要

任务拆解比岗位名更重要，同样是转述。

## 03｜学习速度决定议价能力

学习速度决定议价能力，还是转述。
"""

CLEAN_WITH_BOLD = """# 测试稿：入门岗位先被重新定价

> 他说「入门岗位正在被重新定价，这一点已经很清楚了」。

## 01｜入门岗位先被重新定价

**结论：入门岗位先被重新定价。**

他在 02:15 讲了这个例子。

## 02｜任务拆解比岗位名更重要

**结论：任务拆解比岗位名更重要。**

据 [1]，放射科医生做 26 项任务。

## 03｜学习速度决定议价能力

**结论：学习速度决定议价能力。**

他提到 15:40 的面试片段。

本文归纳：以上判断基于受访者原话。
"""


class TestHealthTable(unittest.TestCase):
    """T1b 回归：体检报告表格不得再出现「6 节全 ✅」。"""

    def _run(self, text: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        case = Path(tmp.name) / "01_测试嘉宾_案例"
        case.mkdir()
        (case / "草稿.md").write_text(text, encoding="utf-8")
        run = Run(run_id="critic-test", artifacts={"draft": "草稿.md"})
        ctx = Context(run=run, case_dir=case, orch_dir=ROOT / "orchestrator", provider=None)
        result = Critic().run(ctx)
        reports = sorted(case.glob("体检报告_*.md"))
        self.assertEqual(len(reports), 1, "体检报告应生成且只有一份")
        return result, reports[0].read_text(encoding="utf-8")

    @staticmethod
    def _cells(report: str, num: str) -> list[str]:
        for line in report.splitlines():
            if line.startswith(f"| {num}｜"):
                return [c.strip() for c in line.strip().strip("|").split("|")]
        raise AssertionError(f"报告里找不到 {num} 节：\n{report}")

    def test_table_not_always_true(self):
        _, report = self._run(CLEAN_NO_BOLD)
        for num in ("01", "02", "03"):
            cells = self._cells(report, num)
            self.assertEqual(cells[1], "❌", f"{num} 节无加粗判断句，可复述应为 ❌\n{report}")
            self.assertEqual(cells[4], "❓", f"{num} 节无证据，有证据应为 ❓\n{report}")
            self.assertEqual(cells[2], "待人工", "「有对手」不得由机器判 ✅")
            self.assertEqual(cells[3], "待人工", "「有代价」不得由机器判 ✅")
            self.assertEqual(cells[5], "需改")
        # 主线不能写死占位符
        self.assertNotIn("待人工填写", report)
        self.assertIn("机器初判", report)

    def test_compliant_draft_scores_machine_columns(self):
        result, report = self._run(CLEAN_WITH_BOLD)
        for num in ("01", "02", "03"):
            cells = self._cells(report, num)
            self.assertEqual(cells[1], "✅", report)
            self.assertEqual(cells[4], "✅", report)
            self.assertEqual(cells[2], "待人工")
            self.assertEqual(cells[3], "待人工")
            self.assertEqual(cells[5], "待人工")
        self.assertEqual(result.status, "ok")
        self.assertIn("机器初判：具备一句话主线的基础", report)
        self.assertNotIn("待人工填写", report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
