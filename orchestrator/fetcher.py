#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""素材抓取：缺转写时尝试从 YouTube 取字幕。

用 yt-dlp（不依赖 API key）。取不到就返回 None，由 transcriber 退回 blocked——
宁可停在"缺转写"，也不要把空稿当成功。

前提：环境里装了 yt-dlp。云端由 workflow 安装；本地 brew/pip 装一次即可。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

DEFAULT_LANGS = "en.*,zh.*,en,zh-Hans,zh-CN"


class FetchError(RuntimeError):
    pass


def yt_dlp_version() -> str | None:
    exe = shutil.which("yt-dlp")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def fetch_subtitles(url: str, out_dir: Path, *, langs: str = DEFAULT_LANGS,
                    timeout: int = 300) -> Path | None:
    """下载字幕（优先人工字幕，其次自动字幕），返回 .vtt 路径。

    失败返回 None，不抛异常——调用方据此落 blocked。
    """
    exe = shutil.which("yt-dlp")
    if not exe:
        return None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        exe,
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", langs,
        "--sub-format", "vtt",
        "--convert-subs", "vtt",
        "--no-playlist",
        "--no-warnings",
        "-o", str(out_dir / "%(id)s.%(ext)s"),
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        (out_dir / "yt-dlp.log").write_text(f"timeout after {timeout}s", encoding="utf-8")
        return None
    except OSError as exc:
        (out_dir / "yt-dlp.log").write_text(f"OSError: {exc}", encoding="utf-8")
        return None

    (out_dir / "yt-dlp.log").write_text(
        f"exit={proc.returncode}\n--- stdout ---\n{proc.stdout[-4000:]}\n"
        f"--- stderr ---\n{proc.stderr[-4000:]}\n", encoding="utf-8")

    candidates = sorted(out_dir.glob("*.vtt"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def looks_like_youtube(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


def _ssl_context():
    """有 certifi 就用它的根证书；没有返回 None。"""
    try:
        import ssl
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return None


def fetch_oembed(url: str, timeout: float = 10.0) -> dict:
    """用 YouTube oEmbed 补标题/频道。失败返回 {}，不抛异常。"""
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    if not looks_like_youtube(url):
        return {}
    endpoint = ("https://www.youtube.com/oembed?format=json&url="
                + urllib.parse.quote(url, safe=""))
    req = urllib.request.Request(endpoint, headers={"User-Agent": "gzh-orchestrator/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError):
        # macOS 上 Python 常缺根证书；有 certifi 就再试一次
        ctx = _ssl_context()
        if ctx is None:
            return {}
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, ValueError):
            return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return {"title": (data.get("title") or "").strip(),
            "channel": (data.get("author_name") or "").strip()}
