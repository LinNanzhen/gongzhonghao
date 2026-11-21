#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""产物版本化（T3）的单测。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.agents.base import Context  # noqa: E402
from orchestrator.agents.writer import Writer  # noqa: E402
from orchestrator.providers.stub import StubProvider  # noqa: E402
from orchestrator.state import Run  # noqa: E402
from orchestrator.versions import next_version  # noqa: E402


class TestNextVersion(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def touch(self, *names: str) -> None:
        for n in names:
            (self.dir / n).write_text("x", encoding="utf-8")

    def test_empty_dir_starts_at_one(self):
        self.assertEqual(next_version(self.dir, r"嘉宾_v(\d+)_排版预览\.html"), 1)

    def test_continues_after_max(self):
        self.touch("嘉宾_v1_排版预览.html", "嘉宾_v2_排版预览.html")
        self.assertEqual(next_version(self.dir, r"嘉宾_v(\d+)_排版预览\.html"), 3)

    def test_gap_uses_max_not_count(self):
        self.touch("嘉宾_v1_排版预览.html", "嘉宾_v3_排版预览.html")
        self.assertEqual(next_version(self.dir, r"嘉宾_v(\d+)_排版预览\.html"), 4)

    def test_other_guests_do_not_interfere(self):
        self.touch("别人_v9_排版预览.html", "嘉宾_v1_排版预览.html")
        self.assertEqual(next_version(self.dir, r"嘉宾_v(\d+)_排版预览\.html"), 2)

    def test_different_suffix_ignored(self):
        self.touch("嘉宾_v5_导入片段.html")
        self.assertEqual(next_version(self.dir, r"嘉宾_v(\d+)_排版预览\.html"), 1)

    def test_writer_naming(self):
        self.touch("嘉宾_人味版_v1.md")
        self.assertEqual(next_version(self.dir, r"嘉宾_人味版_v(\d+)\.md"), 2)

    def test_legacy_old_name_not_matched(self):
        self.touch("嘉宾_人味版.md", "嘉宾_人味版_v1_旧稿.md")
        self.assertEqual(next_version(self.dir, r"嘉宾_人味版_v(\d+)\.md"), 1)

    def test_start_offset(self):
        self.assertEqual(next_version(self.dir, r"嘉宾_v(\d+)_排版预览\.html", start=7), 7)

    def test_missing_dir_returns_start(self):
        self.assertEqual(
            next_version(self.dir / "不存在", r"嘉宾_v(\d+)_排版预览\.html"), 1)

    def test_non_numeric_version_ignored(self):
        self.touch("嘉宾_vX_排版预览.html")
        self.assertEqual(next_version(self.dir, r"嘉宾_v(\d+)_排版预览\.html"), 1)


class TestWriterVersioning(unittest.TestCase):
    """T3b：writer 重跑另存 `_vN`，历史命名算 v1，不覆盖。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        # 案例目录必须符合 NN_嘉宾_案例
        self.case = self.dir / "16_苏姿丰回归_案例"
        self.case.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _writer(self):
        run = Run(run_id="r1", stage="researched")
        ctx = Context(run=run, case_dir=self.case,
                      orch_dir=self.dir / "orchestrator", provider=StubProvider())
        return Writer(), ctx

    def test_first_run_is_v1(self):
        w, ctx = self._writer()
        r = w.run(ctx)
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.artifacts["draft"], "苏姿丰回归_人味版_v1.md")
        self.assertTrue((self.case / "苏姿丰回归_人味版_v1.md").exists())

    def test_rerun_does_not_overwrite(self):
        w, ctx = self._writer()
        w.run(ctx)
        before = (self.case / "苏姿丰回归_人味版_v1.md").read_text(encoding="utf-8")
        r2 = w.run(ctx)
        self.assertEqual(r2.artifacts["draft"], "苏姿丰回归_人味版_v2.md")
        self.assertEqual((self.case / "苏姿丰回归_人味版_v1.md").read_text(encoding="utf-8"), before)

    def test_legacy_name_counts_as_v1(self):
        (self.case / "苏姿丰回归_人味版.md").write_text("历史稿", encoding="utf-8")
        w, ctx = self._writer()
        r = w.run(ctx)
        self.assertEqual(r.artifacts["draft"], "苏姿丰回归_人味版_v2.md")
        self.assertEqual((self.case / "苏姿丰回归_人味版.md").read_text(encoding="utf-8"), "历史稿")

    def test_draft_artifact_is_relative(self):
        w, ctx = self._writer()
        r = w.run(ctx)
        self.assertFalse(Path(r.artifacts["draft"]).is_absolute())


if __name__ == "__main__":
    unittest.main(verbosity=2)
