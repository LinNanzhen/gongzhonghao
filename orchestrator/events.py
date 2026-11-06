#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事件源：inbox 手动投递 + sources.json 定时扫描。

按 SOP v9 §05：
- 幂等键 = 规范化链接的 sha1 前 12 位；
- RSS 扫描失败只记 warning，不阻塞本轮其他 run。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .state import Source, normalize_url, run_id_for, run_id_for_text
from .transcript import FEISHU_LINE, SRT_TIME

URL_RE = re.compile(r"https?://[^\s<>\"')]+")


# 允许与主文件同名的转写附件
TRANSCRIPT_EXT = {".srt", ".vtt", ".json", ".txt", ".md"}


@dataclass
class Event:
    source: Source
    run_id: str
    attachments: list[Path] = field(default_factory=list)

    def __str__(self) -> str:
        extra = f"  +{len(self.attachments)}附件" if self.attachments else ""
        return f"{self.run_id}  {self.source.kind:5s}  {self.source.url}{extra}"


# ---------------------------------------------------------------- inbox


def parse_inbox_file(path: Path) -> Source | None:
    """一个投递文件 = 一行链接 + 可选备注。

    支持 .md / .txt（首行链接）与 .json（{url,title,notes}）。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    if path.suffix == ".json":
        try:
            d = json.loads(text)
        except json.JSONDecodeError:
            return None
        url = (d.get("url") or "").strip()
        if not url:
            return None
        return Source(kind="inbox", url=normalize_url(url),
                      title=(d.get("title") or "").strip(),
                      notes=(d.get("notes") or "").strip(),
                      channel=(d.get("channel") or "").strip())

    lines = [ln.strip() for ln in text.splitlines()]
    url = ""
    for ln in lines:
        m = URL_RE.search(ln)
        if m:
            url = m.group(0)
            break
    if not url:
        # 没有链接：本地素材。若内容本身是转写稿，用文件名当标题，
        # 否则（首行 @说话人 00:01 之类的）会取出一串没意义的东西。
        if _looks_like_transcript(text):
            return Source(kind="local", url="", title=path.stem,
                          notes=f"本地转写：{path.name}")
        title = next((ln.lstrip("# ").strip() for ln in lines if ln and not ln.startswith("#")),
                     path.stem)
        return Source(kind="local", url="", title=title,
                      notes=" | ".join(ln for ln in lines if ln and not ln.startswith("#"))[:300])
    # 链接所在行之后的非空行作为备注
    idx = next(i for i, ln in enumerate(lines) if url in ln)
    notes = " ".join(ln for ln in lines[idx + 1:] if ln and not ln.startswith("#"))
    title = ""
    for ln in lines[:idx]:
        if ln and not ln.startswith("#"):
            title = ln.strip("# ").strip()
            break
    return Source(kind="inbox", url=normalize_url(url), title=title, notes=notes)


def _looks_like_transcript(text: str) -> bool:
    """有说话人时间戳或 SRT 时间轴，就当作转写稿本身。"""
    return bool(FEISHU_LINE.search(text) or SRT_TIME.search(text)
                or text.lstrip().startswith("WEBVTT"))


def scan_inbox(inbox_dir: Path) -> list[Event]:
    """一个 stem 一组：主文件（.md 优先）+ 同名转写附件。

    例：`David_Baker.md` + `David_Baker.srt` → 一个事件，srt 作为附件随任务复制。
    只有 `.srt`/`.vtt` 没有主文件时，也视为本地素材。
    """
    out: list[Event] = []
    if not inbox_dir.is_dir():
        return out

    files = [p for p in sorted(inbox_dir.iterdir())
             if p.is_file() and not p.name.startswith(".")
             and not p.name.upper().startswith("README")]
    groups: dict[str, list[Path]] = {}
    for p in files:
        groups.setdefault(p.stem, []).append(p)

    for stem, group in groups.items():
        primary = (next((p for p in group if p.suffix.lower() == ".md"), None)
                   or next((p for p in group if p.suffix.lower() in (".json", ".txt")), None))
        if primary is None:
            subs = [p for p in group if p.suffix.lower() in (".srt", ".vtt", ".json")]
            if not subs:
                continue
            # 只有转写、没有主文件：文件名即标题，转写本身作为附件
            out.append(Event(
                source=Source(kind="local", url="", title=stem,
                              notes=f"本地转写：{', '.join(p.name for p in subs)}"),
                run_id=run_id_for_text(stem),
                attachments=[p for p in group if p.suffix.lower() in TRANSCRIPT_EXT],
            ))
            continue

        try:
            content = primary.read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = ""
        if _looks_like_transcript(content):
            # 主文件本身就是转写稿：它自己也要作为附件复制进案例
            attachments = [p for p in group if p.suffix.lower() in TRANSCRIPT_EXT]
        else:
            attachments = [p for p in group if p != primary
                           and p.suffix.lower() in TRANSCRIPT_EXT]
        src = parse_inbox_file(primary)
        if src is None:
            continue
        rid = run_id_for(src.url) if src.url else run_id_for_text(stem)
        out.append(Event(source=src, run_id=rid, attachments=attachments))
    return out


# ---------------------------------------------------------------- RSS


def _text(el, *tags: str) -> str:
    for tag in tags:
        found = el.find(tag)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
        found = el.find(f"{{*}}{tag}")
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return ""


def _entry_url(el) -> str:
    for link in el.findall("{*}link"):
        href = link.get("href")
        if href:
            return href
        if link.text and link.text.strip():
            return link.text.strip()
    return _text(el, "guid", "id")


def fetch_feed(url: str, timeout: float = 20.0) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "gzh-orchestrator/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    entries = root.findall(".//{*}entry") or root.findall(".//item")
    out = []
    for e in entries:
        out.append({
            "url": _entry_url(e),
            "title": _text(e, "title"),
            "published": _text(e, "published", "updated", "pubDate"),
        })
    return out


def scan_sources(sources_path: Path, *, days: int = 7, timeout: float = 20.0,
                 warn=None) -> tuple[list[Event], list[str]]:
    """返回 (事件列表, 警告列表)。网络失败不抛异常。"""
    events: list[Event] = []
    warnings: list[str] = []
    if not sources_path.exists():
        return events, warnings
    try:
        cfg = json.loads(sources_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return events, [f"sources.json 解析失败：{exc}"]

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for feed in cfg.get("feeds", []):
        url = feed.get("url", "")
        if not url:
            continue
        name = feed.get("name", url)
        max_items = int(feed.get("max_items", 5))
        try:
            entries = fetch_feed(url, timeout=timeout)
        except (urllib.error.URLError, ET.ParseError, OSError, ValueError) as exc:
            warnings.append(f"{name}: 抓取失败（{exc}）")
            continue

        for e in entries[:max_items]:
            if not e["url"]:
                continue
            # YouTube Atom 的 published 形如 2026-10-30T12:00:00+00:00
            pub = e.get("published", "")
            if pub:
                try:
                    dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt < cutoff:
                        continue
                except ValueError:
                    try:
                        from email.utils import parsedate_to_datetime
                        if parsedate_to_datetime(pub) < cutoff:
                            continue
                    except (TypeError, ValueError):
                        pass
            src = Source(kind="rss", url=normalize_url(e["url"]),
                         title=e.get("title", ""), channel=name, published_at=pub)
            events.append(Event(source=src, run_id=run_id_for(src.url)))
    return events, warnings


def dedupe(events: list[Event], existing_ids: set[str]) -> list[Event]:
    seen = set(existing_ids)
    out = []
    for ev in events:
        if ev.run_id in seen:
            continue
        seen.add(ev.run_id)
        out.append(ev)
    return out
