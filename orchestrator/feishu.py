#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书妙记：从链接直接拉转写文本。

官方接口：
    GET /open-apis/minutes/v1/minutes/{minute_token}/transcript
        ?need_speaker=true&need_timestamp=true&file_format=srt
    返回文件二进制流；权限 minutes:minutes.transcript:export

鉴权用自建应用的 tenant_access_token：
    FEISHU_APP_ID / FEISHU_APP_SECRET

注意：tenant token 只能访问应用被授权的妙记。403 通常是妙记的
「权限设置」里没有给该应用导出权限，需要妙记所有者放开。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://open.feishu.cn/open-apis"
MINUTES_URL = re.compile(r"/(?:minutes|minute)/([A-Za-z0-9]{10,40})")


class FeishuError(RuntimeError):
    pass


def minute_token(url: str) -> str | None:
    """从妙记链接里取 token；也接受直接给 token。"""
    url = (url or "").strip()
    m = MINUTES_URL.search(url)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{20,26}", url):
        return url
    return None


def is_minutes_url(url: str) -> bool:
    return "feishu" in url or "larksuite" in url


def _post_json(path: str, payload: dict, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raise FeishuError(f"HTTP {exc.code}：{_snippet(exc.read())}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise FeishuError(f"网络错误：{exc}") from None
    if data.get("code") not in (0, None):
        raise FeishuError(f"code={data.get('code')} msg={data.get('msg')}")
    return data


def _snippet(raw: bytes, limit: int = 200) -> str:
    text = (raw or b"").decode("utf-8", errors="replace")
    return " ".join(text.split())[:limit]


def tenant_token(app_id: str | None = None, app_secret: str | None = None,
                 timeout: float = 20.0) -> str:
    app_id = app_id or os.environ.get("FEISHU_APP_ID", "")
    app_secret = app_secret or os.environ.get("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        raise FeishuError("缺少飞书应用凭证：设置 FEISHU_APP_ID 与 FEISHU_APP_SECRET")
    data = _post_json("/auth/v3/tenant_access_token/internal",
                      {"app_id": app_id, "app_secret": app_secret}, timeout)
    token = data.get("tenant_access_token")
    if not token:
        raise FeishuError(f"未返回 tenant_access_token：{data}")
    return token


def download_transcript(token: str, *, auth: str | None = None,
                        file_format: str = "srt", timeout: float = 60.0) -> bytes:
    """拉取妙记转写，默认 srt（带时间戳，便于引文定位）。"""
    auth = auth or tenant_token()
    query = urllib.parse.urlencode({
        "need_speaker": "true",
        "need_timestamp": "true",
        "file_format": file_format,
    })
    url = f"{BASE}/minutes/v1/minutes/{token}/transcript?{query}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {auth}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        code, biz = "", None
        try:
            body = json.loads(raw.decode("utf-8", errors="replace"))
            biz = body.get("code")
            code = f"code={biz} msg={body.get('msg')}"
        except ValueError:
            code = _snippet(raw)
        hint = {
            2091002: "找不到该妙记，检查 token",
            2091003: "妙记还没转写完成，稍后再试",
            2091004: "妙记已删除",
            2091005: "无导出权限：在妙记「权限设置」里给应用放开导出",
        }.get(biz) or {
            403: "无导出权限：检查妙记的权限设置",
            404: "找不到该妙记",
        }.get(exc.code, "")
        raise FeishuError(f"HTTP {exc.code} {code} {hint}".strip()) from None
    except (urllib.error.URLError, OSError) as exc:
        raise FeishuError(f"网络错误：{exc}") from None


def fetch_minutes_transcript(url_or_token: str, out_dir: Path, *,
                             file_format: str = "srt") -> Path | None:
    """一步到位：链接/token → 落盘 srt。失败返回 None（调用方落 blocked）。"""
    token = minute_token(url_or_token)
    if not token:
        return None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        raw = download_transcript(token, file_format=file_format)
    except FeishuError as exc:
        (out_dir / "feishu.log").write_text(str(exc), encoding="utf-8")
        return None
    if not raw.strip():
        (out_dir / "feishu.log").write_text("接口返回空内容", encoding="utf-8")
        return None
    ext = ".srt" if file_format == "srt" else ".txt"
    path = out_dir / f"飞书妙记{ext}"
    path.write_bytes(raw)
    return path
