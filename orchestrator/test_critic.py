#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""人味检查的单元测试（依据 模板/人味检查表.md）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.agents.critic import scan_humanize  # noqa: E402


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
