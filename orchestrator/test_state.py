#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""状态机单元测试：python3 -m unittest orchestrator.test_state -v"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.state import (  # noqa: E402
    IllegalTransition, Run, Source, normalize_url, run_id_for,
)


class TestTransitions(unittest.TestCase):
    def test_happy_path(self):
        r = Run(run_id="x")
        for to in ["triaged", "transcribed", "ledgered", "researched", "drafted",
                   "critiqued", "composed", "qc_passed", "awaiting_publish",
                   "published", "archived"]:
            r.advance(to, "test")
        self.assertEqual(r.stage, "archived")
        self.assertEqual(r.status, "done")
        self.assertEqual(len(r.history), 11)

    def test_illegal_forward(self):
        r = Run(run_id="x")
        with self.assertRaises(IllegalTransition):
            r.advance("drafted", "test")   # 不能跳步

    def test_no_skip_gate(self):
        r = Run(run_id="x")
        r.advance("triaged", "scout")
        with self.assertRaises(IllegalTransition):
            r.advance("awaiting_publish", "test")

    def test_rewind_from_drafted(self):
        r = Run(run_id="x", stage="drafted")
        r.rewind("researched", "critic", "标题是话题词")
        self.assertEqual(r.stage, "researched")
        self.assertEqual(r.rework["count"], 1)

    def test_rewind_from_composed(self):
        r = Run(run_id="x", stage="composed")
        r.rewind("critiqued", "gatekeeper", "质检 FAIL")
        self.assertEqual(r.stage, "critiqued")

    def test_rewind_illegal(self):
        r = Run(run_id="x", stage="new")
        with self.assertRaises(IllegalTransition):
            r.rewind("researched", "x", "y")

    def test_rewind_limit_blocks(self):
        r = Run(run_id="x", stage="drafted")
        for i in range(4):
            try:
                r.rewind("researched", "critic", f"第 {i} 次")
            except IllegalTransition:
                break
            r.stage = "drafted"   # 模拟再次进入
        self.assertEqual(r.status, "blocked")
        self.assertIn("打回超过", r.error or "")

    def test_error_limit(self):
        r = Run(run_id="x")
        for _ in range(3):
            r.record_error("scout", RuntimeError("boom"))
        self.assertEqual(r.status, "blocked")


class TestGates(unittest.TestCase):
    def test_gate_blocks(self):
        r = Run(run_id="x", stage="triaged")
        self.assertEqual(r.waiting_gate, "topic")

    def test_gate_passed_allows(self):
        r = Run(run_id="x", stage="triaged", gates={"topic": True, "publish": False})
        self.assertIsNone(r.waiting_gate)

    def test_publish_gate(self):
        r = Run(run_id="x", stage="awaiting_publish")
        self.assertEqual(r.waiting_gate, "publish")

    def test_from_dict_backfills_gates(self):
        r = Run.from_dict({"run_id": "x", "stage": "triaged",
                           "source": {"url": "u"}, "history": [], "rework": {}})
        self.assertEqual(r.gates, {"topic": False, "publish": False})


class TestIds(unittest.TestCase):
    def test_normalize_youtu_be(self):
        self.assertEqual(normalize_url("https://youtu.be/abc123DEF"),
                         "https://www.youtube.com/watch?v=abc123DEF")

    def test_strip_tracking(self):
        a = normalize_url("https://example.com/v/1?utm_source=x&si=y")
        self.assertEqual(a, "https://example.com/v/1")

    def test_idempotent_id(self):
        a = run_id_for("https://www.youtube.com/watch?v=abc&si=zz")
        b = run_id_for("https://www.youtube.com/watch?v=abc")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
