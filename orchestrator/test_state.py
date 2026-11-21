#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""状态机单元测试：python3 -m unittest orchestrator.test_state -v"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.state import (  # noqa: E402
    IllegalTransition, Run, RunStore, Source, normalize_url, now, run_id_for,
)


class TestTransitions(unittest.TestCase):
    def test_happy_path(self):
        r = Run(run_id="x")
        for to in ["triaged", "transcribed", "ledgered", "researched", "drafted",
                   "critiqued", "illustrated", "composed", "qc_passed", "awaiting_publish",
                   "published", "archived"]:
            r.advance(to, "test")
        self.assertEqual(r.stage, "archived")
        self.assertEqual(r.status, "done")
        self.assertEqual(len(r.history), 12)

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


class TestTimeUTC(unittest.TestCase):
    """T2b：新时间戳一律 UTC。"""

    def test_now_is_utc(self):
        stamp = now()
        self.assertTrue(stamp.endswith("+00:00"), stamp)
        dt = datetime.fromisoformat(stamp)
        self.assertEqual(dt.utcoffset(), timezone.utc.utcoffset(None))

    def test_events_log_new_line_is_utc(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp))
            run = store.create("rid1", Source(title="t"), "case")
            store.save(run, "advance\ttest")
            lines = store.log_path("rid1").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertTrue(lines[-1].startswith("20"), lines[-1])
            self.assertIn("+00:00", lines[-1])

    def test_historical_local_line_not_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp))
            run = store.create("rid2", Source(title="t"), "case")
            old = "2026-11-06T13:09:09+08:00\tcreate\tlocal"
            log = store.log_path("rid2")
            text = log.read_text(encoding="utf-8")
            log.write_text(old + "\n" + text, encoding="utf-8")
            store.save(run, "advance\ttest")
            lines = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], old, "历史记录不应被重写")
            self.assertIn("+00:00", lines[-1])


class TestUsageField(unittest.TestCase):
    """T5b：Run.usage 字段与旧 state.json 兼容。"""

    def test_default_empty(self):
        self.assertEqual(Run(run_id="x").usage, {})

    def test_to_dict_includes_usage(self):
        r = Run(run_id="x")
        r.add_usage({"input": 10, "output": 5, "total": 15, "calls": 1})
        d = r.to_dict()
        self.assertEqual(d["usage"]["total"], 15)

    def test_from_dict_backfills_usage(self):
        r = Run.from_dict({"run_id": "x", "stage": "new", "source": {},
                           "history": [], "rework": {}})
        self.assertEqual(r.usage, {})

    def test_from_dict_keeps_usage(self):
        usage = {"input": 100, "output": 20, "total": 120, "calls": 1}
        r = Run.from_dict({"run_id": "x", "usage": dict(usage)})
        self.assertEqual(r.usage, usage)

    def test_add_usage_accumulates(self):
        r = Run(run_id="x")
        r.add_usage({"input": 10, "output": 5, "total": 15, "calls": 1})
        r.add_usage({"input": 20, "output": 3, "total": 23, "calls": 1})
        self.assertEqual(r.usage, {"input": 30, "output": 8, "total": 38, "calls": 2})

    def test_add_usage_ignores_garbage(self):
        r = Run(run_id="x")
        r.add_usage({"total": None, "calls": "2", "input": "bad", "extra": 1})
        self.assertEqual(r.usage, {"calls": 2, "extra": 1})


class TestBrokenState(unittest.TestCase):
    """T2c：坏 state.json 要可见，但不影响其他 run。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = RunStore(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, run_id: str, text: str) -> None:
        d = self.root / run_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(text, encoding="utf-8")

    def test_all_collects_broken(self):
        good = self.store.create("good", Source(title="ok"), "case")
        self.store.save(good)
        self._write("bad", "{这不是 JSON")
        runs = self.store.all()
        self.assertEqual([r.run_id for r in runs], ["good"])
        self.assertEqual(len(self.store.broken), 1)
        item = self.store.broken[0]
        self.assertIn("bad", item["path"])
        self.assertIn("state.json", item["path"])
        self.assertTrue(item["error"])

    def test_broken_does_not_affect_get(self):
        self.store.save(self.store.create("good", Source(title="ok"), "case"))
        self._write("bad", "[1, 2, 3]")     # 合法 JSON 但不是对象
        runs = self.store.all()
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(self.store.broken), 1)
        self.assertIsNotNone(self.store.get("good"))

    def test_broken_cleared_after_fix(self):
        self._write("bad", "not json")
        self.store.all()
        self.assertEqual(len(self.store.broken), 1)
        self._write("bad", json.dumps({"run_id": "bad"}))
        runs = self.store.all()
        self.assertEqual(self.store.broken, [])
        self.assertEqual([r.run_id for r in runs], ["bad"])


class TestIds(unittest.TestCase):
    def test_normalize_youtu_be(self):
        self.assertEqual(normalize_url("https://youtu.be/abc123DEF"),
                         "https://www.youtube.com/watch?v=abc123DEF")

    def test_normalize_youtube_live_and_shorts(self):
        expected = "https://www.youtube.com/watch?v=1mu3x97th90"
        for u in ("https://www.youtube.com/live/1mu3x97th90?si=abc",
                  "https://www.youtube.com/shorts/1mu3x97th90",
                  "https://m.youtube.com/watch?v=1mu3x97th90",
                  "https://youtu.be/1mu3x97th90"):
            self.assertEqual(normalize_url(u), expected, u)

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
