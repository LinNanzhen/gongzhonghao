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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .state import Source, normalize_url, run_id_for

URL_RE = re.compile(r"https?://[^\s<>\"')]+")


@dataclass
class Event:
    source: Source
    run_id: str

    def __str__(self) -> str:
        return f"{self.run_id}  {self.source.kind:5s}  {self.source.url}"


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
        return None
    # 链接所在行之后的非空行作为备注
    idx = next(i for i, ln in enumerate(lines) if url in ln)
    notes = " ".join(ln for ln in lines[idx + 1:] if ln and not ln.startswith("#"))
    title = ""
    for ln in lines[:idx]:
        if ln and not ln.startswith("#"):
            title = ln.strip("# ").strip()
            break
    return Source(kind="inbox", url=normalize_url(url), title=title, notes=notes)


def scan_inbox(inbox_dir: Path) -> list[Event]:
    out: list[Event] = []
    if not inbox_dir.is_dir():
        return out
    for p in sorted(inbox_dir.iterdir()):
        if not p.is_file() or p.name.startswith(".") or p.suffix not in (".md", ".txt", ".json"):
            continue
        if p.name.upper().startswith("README"):
            continue
        src = parse_inbox_file(p)
        if src:
            out.append(Event(source=src, run_id=run_id_for(src.url)))
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
