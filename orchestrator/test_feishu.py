#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书妙记接口的单元测试（mock 网络，不需要真实凭证）。"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.feishu import (  # noqa: E402
    FeishuError, download_transcript, fetch_minutes_transcript,
    is_minutes_url, minute_token, tenant_token,
)

SRT = b"""1
00:00:01,000 --> 00:00:03,500
\xe7\xac\xac\xe4\xb8\x80\xe5\x8f\xa5
"""


def resp(payload: bytes):
    class _R:
        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _R()


def http_error(code: int, body: dict):
    return urllib.error.HTTPError("http://x", code, "err", {},
                                  io.BytesIO(json.dumps(body).encode()))


class TestToken(unittest.TestCase):
    def test_url(self):
        self.assertEqual(
            minute_token("https://sample.feishu.cn/minutes/obcnq3b9jl72l83w4f149w9c"),
            "obcnq3b9jl72l83w4f149w9c")

    def test_bare_token(self):
        self.assertEqual(minute_token("obcnq3b9jl72l83w4f149w9c"), "obcnq3b9jl72l83w4f149w9c")

    def test_youtube_is_none(self):
        self.assertIsNone(minute_token("https://www.youtube.com/watch?v=abc"))

    def test_is_minutes(self):
        self.assertTrue(is_minutes_url("https://x.feishu.cn/minutes/abc"))
        self.assertTrue(is_minutes_url("https://x.larksuite.com/minutes/abc"))
        self.assertFalse(is_minutes_url("https://youtube.com/watch?v=1"))


class TestAuth(unittest.TestCase):
    def test_missing_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(FeishuError) as ctx:
                tenant_token()
        self.assertIn("FEISHU_APP_ID", str(ctx.exception))

    def test_token_ok(self):
        with patch("urllib.request.urlopen",
                   return_value=resp(json.dumps(
                       {"code": 0, "tenant_access_token": "t-123"}).encode())):
            self.assertEqual(tenant_token("id", "secret"), "t-123")


class TestDownload(unittest.TestCase):
    def test_srt_bytes(self):
        with patch("urllib.request.urlopen", return_value=resp(SRT)):
            data = download_transcript("tok", auth="bearer-x")
        self.assertEqual(data, SRT)

    def test_permission_denied_hint(self):
        err = http_error(403, {"code": 2091005, "msg": "permission deny"})
        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(FeishuError) as ctx:
                download_transcript("tok", auth="x")
        self.assertIn("权限", str(ctx.exception))

    def test_not_ready_hint(self):
        err = http_error(400, {"code": 2091003, "msg": "minute not ready"})
        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(FeishuError) as ctx:
                download_transcript("tok", auth="x")
        self.assertIn("还没转写完成", str(ctx.exception))


class TestFetch(unittest.TestCase):
    def test_writes_file(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        try:
            with patch("orchestrator.feishu.tenant_token", return_value="t"), \
                 patch("urllib.request.urlopen", return_value=resp(SRT)):
                out = fetch_minutes_transcript(
                    "https://x.feishu.cn/minutes/obcnq3b9jl72l83w4f149w9c", tmp)
            self.assertIsNotNone(out)
            self.assertTrue(out.exists())
            self.assertEqual(out.suffix, ".srt")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_failure_writes_log(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        try:
            with patch("orchestrator.feishu.tenant_token", return_value="t"), \
                 patch("urllib.request.urlopen",
                       side_effect=http_error(403, {"code": 2091005})):
                out = fetch_minutes_transcript(
                    "https://x.feishu.cn/minutes/obcnq3b9jl72l83w4f149w9c", tmp)
            self.assertIsNone(out)
            self.assertTrue((tmp / "feishu.log").exists())
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
