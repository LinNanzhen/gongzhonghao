#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""inbox 投递规则测试（主文件 + 同名转写附件）。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.events import scan_inbox  # noqa: E402


class InboxCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.inbox = self.tmp / "inbox"
        self.inbox.mkdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name: str, text: str) -> Path:
        p = self.inbox / name
        p.write_text(text, encoding="utf-8")
        return p


class TestPairing(InboxCase):
    def test_link_with_attachment(self):
        self.write("David_Baker.md", "David Baker：三十年的设计\nhttps://youtu.be/1mu3x97th90\n")
        self.write("David_Baker.srt", "1\n00:00:01,000 --> 00:00:02,000\nx\n")
        evts = scan_inbox(self.inbox)
        self.assertEqual(len(evts), 1)
        self.assertEqual(evts[0].source.title, "David Baker：三十年的设计")
        self.assertIn("watch?v=1mu3x97th90", evts[0].source.url)
        self.assertEqual([a.name for a in evts[0].attachments], ["David_Baker.srt"])

    def test_link_only(self):
        self.write("x.md", "https://example.com/v/1\n")
        evts = scan_inbox(self.inbox)
        self.assertEqual(len(evts), 1)
        self.assertEqual(evts[0].attachments, [])

    def test_transcript_only(self):
        self.write("本地录音.srt", "1\n00:00:02,000 --> 00:00:04,000\n内容\n")
        evts = scan_inbox(self.inbox)
        self.assertEqual(len(evts), 1)
        self.assertEqual(evts[0].source.kind, "local")
        self.assertEqual(evts[0].source.title, "本地录音")
        self.assertEqual(evts[0].source.url, "")
        self.assertEqual([a.name for a in evts[0].attachments], ["本地录音.srt"])

    def test_readme_skipped(self):
        self.write("README.md", "# 说明\n没有链接\n")
        self.write("x.md", "https://example.com/a\n")
        self.assertEqual([e.source.url for e in scan_inbox(self.inbox)],
                         ["https://example.com/a"])

    def test_two_entries_separate(self):
        self.write("a.md", "https://example.com/1\n")
        self.write("b.md", "https://example.com/2\n")
        self.assertEqual(len(scan_inbox(self.inbox)), 2)

    def test_local_id_stable(self):
        self.write("录音A.srt", "1\n00:00:01,000 --> 00:00:02,000\nx\n")
        first = scan_inbox(self.inbox)[0].run_id
        second = scan_inbox(self.inbox)[0].run_id
        self.assertEqual(first, second, "同一本地素材应得到相同 id")
        self.assertEqual(len(first), 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
