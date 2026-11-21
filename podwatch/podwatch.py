#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""podwatch · 播客自动监控（RSS → 去重 → 全文/转写 → 每日日报）

把「值得追的播客」变成每天早上 10 分钟读完的一份日报 + 每期一篇文章归档。
只依赖标准库（Python 3.10+），不需要 key；转写/推送是可选项。

核心链路：
    RSS/Atom 轮询 → guid/link 去重（state.json）→ 取内容
      ├─ feed 自带全文（qiaomu 精读 / Substack 转写）→ 直接入档
      ├─ 官网单集页正文                                → 抓取入档
      └─ 只有音频                                       → 入「待转写」队列（配了 STT 才跑）
    → 每期一份 文章/<节目>/<日期>_<标题>.md + .meta.json
    → 汇总成 输出/日报_YYYY-MM-DD.md

命令：
    poll        抓一轮，把新节目入档
    digest      生成日报（默认今天）
    run         poll + digest（cron 调这一个）
    check       订阅源健康检查（不写状态）
    index       按节目统计已入档篇数
    transcribe  处理「待转写」队列（需要 STT 后端）
    find        用 Apple 播客搜索找 feed
    add         搜索并写入配置

环境变量（都可不配）：
    GZH_STT_BASE_URL / GZH_STT_API_KEY / GZH_STT_MODEL   OpenAI 兼容音频转写接口
    FEISHU_WEBHOOK                                       日报推送到飞书群

示例：
    python3 podwatch.py poll
    python3 podwatch.py digest
    python3 podwatch.py run --notify
    python3 podwatch.py check
    python3 podwatch.py find --keyword "odd lots" | head
"""
from __future__ import annotations

import argparse
import hashlib
import html as htmllib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

# ------------------------------------------------------------------ 常量

# 工作区根：本地默认 /Users/lnz/Desktop/公众号；服务器等非常规目录用 GZH_WORKSPACE 覆盖
ROOT = Path(os.environ.get("GZH_WORKSPACE") or Path(__file__).resolve().parents[2])
HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "podcasts.json"
DEFAULT_OUT = ROOT / "播客监控"

NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "atom": "http://www.w3.org/2005/Atom",
    "podcast": "https://podcastindex.org/namespace/1.0",
}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 podwatch/1.0"
)
TIER_ORDER = {"S": 0, "A": 1, "B": 2, "C": 3}
TIER_LABEL = {"S": "S 级 · 必追", "A": "A 级 · 值得听", "B": "B 级 · 扫标题", "C": "C 级 · 归档"}
TR_LABEL = {
    "feed_content": "全文（RSS 自带）",
    "feed_notes": "仅节目笔记",
    "site_page": "全文（官网抓取）",
    "audio_stt": "语音转写（未校）",
    "queued": "待转写",
    "none": "无",
}
# 选题价值关键词：命中越多越值得优先看
AI_WORDS = (
    "openai", "anthropic", "deepmind", "nvidia", "meta", "microsoft", "google",
    "agi", "gpt", "claude", "gemini", "llm", "大模型", "智能体", "算力", "芯片",
    "英伟达", "黄仁勋", "奥特曼", "scaling", "inference", "transformer", "agent",
    "reasoning", "机器人", "具身", "model", "训练", "推理",
)
ROLE_WORDS = (
    "ceo", "founder", "co-founder", "cto", "president", "research scientist",
    "professor", "创始人", "联合创始人", "对话", "访谈", "独家", "首席",
)


# ------------------------------------------------------------------ 小工具

def now_local() -> datetime:
    return datetime.now().astimezone()


def iso(dt: datetime | None = None) -> str:
    return (dt or now_local()).astimezone().isoformat(timespec="seconds")


def local_date(dt: datetime | None = None) -> str:
    return (dt or now_local()).astimezone().strftime("%Y-%m-%d")


def parse_date(raw: str | None) -> datetime | None:
    """RSS/Atom 的 pubDate / published / updated → aware datetime。"""
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        pass
    token = raw.replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
        try:
            dt = datetime.fromisoformat(token) if fmt is None else datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return dt.replace(tzinfo=dt.tzinfo or timezone.utc)
    return None


def parse_duration(raw: str | None) -> int:
    """itunes:duration 可能是秒数，也可能是 HH:MM:SS / MM:SS。"""
    if not raw:
        return 0
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    parts = [p for p in re.split(r"[:.]", raw) if p.strip()]
    try:
        nums = [int(float(p)) for p in parts]
    except ValueError:
        return 0
    if len(nums) >= 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return nums[0] if nums else 0


def fmt_duration(sec: int) -> str:
    if not sec:
        return "—"
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def sha1(text: str, n: int = 16) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:n]


def slugify(title: str, limit: int = 60) -> str:
    """只保留中文/字母/数字/下划线，其余压成连字符（文件名与 Markdown 链接都安全）。"""
    s = (title or "").strip()
    s = re.sub(r"[^\w\-]+", "-", s, flags=re.UNICODE)
    s = re.sub(r"[-_]{2,}", "-", s).strip("-_.")
    if len(s) > limit:
        s = s[:limit].rstrip("-_.")
    return s or "untitled"


def strip_html(raw: str, limit: int = 0) -> str:
    """去标签 + 反转义 + 压空白（用于标题/摘要这类单行文本）。"""
    if not raw:
        return ""
    txt = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)
    txt = htmllib.unescape(txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:limit] if limit else txt


def cjk_width(text: str) -> int:
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - cjk_width(text))


def log(msg: str, *, quiet: bool = False) -> None:
    stamp = now_local().strftime("%H:%M:%S")
    if not quiet:
        print(f"[{stamp}] {msg}", file=sys.stderr)


def stars(score: int) -> str:
    n = max(1, min(5, int(round(score / 2.0))))
    return "★" * n + "☆" * (5 - n)


def score_item(podcast: dict, item: dict, transcript_kind: str) -> int:
    """无模型也能排序的粗打分：分级 + 关键词 + 时长 + 是否拿到全文。"""
    tier = str(podcast.get("tier", "B")).upper()
    score = {"S": 6, "A": 4, "B": 2, "C": 1}.get(tier, 1)
    hay = f"{item.get('title', '')} {item.get('description', '')}".lower()
    score += min(3, sum(1 for w in AI_WORDS if w in hay))
    score += min(2, sum(1 for w in ROLE_WORDS if w in hay))
    if item.get("duration", 0) >= 45 * 60:
        score += 2
    if transcript_kind in ("feed_content", "site_page", "audio_stt"):
        score += 2
    return score


# ------------------------------------------------------------------ HTTP

class FetchError(RuntimeError):
    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = status


def _ssl_context():
    """python.org 的 Python 常缺根证书；有 certifi 就用它的 CA。"""
    try:
        import certifi
        import ssl
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return None


_SSL = _ssl_context()


def ensure_http(url: str) -> str:
    """外部输入（feed 里的 link/audio）只允许 http/https，挡掉 file:// 等本地读原语。"""
    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise FetchError(f"不支持的协议：{scheme or '(空)'}（只允许 http/https）")
    return url


def http_get(url: str, *, timeout: float = 30.0, retries: int = 2,
             headers: dict | None = None,
             max_bytes: int = 25 * 1024 * 1024) -> tuple[bytes, str]:
    """GET → (bytes, content_type)。网络抖动重试一次；4xx 直接失败；响应有 25MB 上限。"""
    ensure_http(url)
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            **(headers or {}),
        })
        try:
            kwargs = {"timeout": timeout}
            if url.startswith("https") and _SSL is not None:
                kwargs["context"] = _SSL
            with urlopen(req, **kwargs) as resp:
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise FetchError(f"响应超过 {max_bytes // (1024 * 1024)} MB 上限：{url}")
                    chunks.append(chunk)
                return b"".join(chunks), resp.headers.get("Content-Type", "")
        except HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code not in (408, 429):
                raise FetchError(f"HTTP {exc.code}", exc.code) from exc
            last = exc
        except (URLError, TimeoutError, OSError) as exc:
            last = exc
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise FetchError(f"{type(last).__name__}: {last}")


def decode_body(raw: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    enc = m.group(1) if m else "utf-8"
    for candidate in (enc, "utf-8", "gb18030"):
        try:
            return raw.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def download_audio(url: str, dest: Path, *, max_mb: int = 300,
                   timeout: float = 120.0) -> Path:
    """流式下载音频，超限中止（避免误下 1GB 母带）。"""
    ensure_http(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = Request(url, headers={"User-Agent": USER_AGENT})
    kwargs = {"timeout": timeout}
    if url.startswith("https") and _SSL is not None:
        kwargs["context"] = _SSL
    total = 0
    limit = max_mb * 1024 * 1024
    try:
        with urlopen(req, **kwargs) as resp, dest.open("wb") as fh:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise FetchError(f"音频超过 {max_mb} MB 上限，已中止")
                fh.write(chunk)
    except HTTPError as exc:
        dest.unlink(missing_ok=True)
        raise FetchError(f"HTTP {exc.code}", exc.code) from exc
    except (URLError, TimeoutError, OSError) as exc:
        dest.unlink(missing_ok=True)
        raise FetchError(f"下载失败：{exc}") from exc
    except FetchError:
        dest.unlink(missing_ok=True)
        raise
    return dest


# ------------------------------------------------------------------ XML / RSS

def _fix_entities(text: str) -> str:
    """修坏 feed：未转义的 & 和 HTML 命名实体（ET 不认）。"""
    text = re.sub(r"&(?!#\d+;|#x[0-9a-fA-F]+;|[a-zA-Z][\w.\-]*;)", "&amp;", text)
    for name, ch in htmllib.entities.html5.items():
        token = f"&{name};" if not name.endswith(";") else f"&{name}"
        if token in text:
            text = text.replace(token, f"&#{ord(ch)};")
    return text


def parse_xml(raw: bytes) -> ET.Element:
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        text = raw.decode("utf-8", "replace").lstrip("\ufeff")
        return ET.fromstring(_fix_entities(text))


def _first_text(el: ET.Element, paths: list[str]) -> str:
    for path in paths:
        found = el.find(path, NS)
        if found is not None and (found.text or "").strip():
            return (found.text or "").strip()
    return ""


AUDIO_EXT = (".mp3", ".m4a", ".m4b", ".aac", ".ogg", ".oga", ".opus", ".wav",
             ".flac", ".mp4", ".webm")


def _pick_audio(el: ET.Element) -> str:
    """挑真正的音频 enclosure。

    Substack 之类会把封面图放进 <enclosure>（type=image/jpeg），
    直接取 url 会把图片当音频下载——这里按 MIME 与扩展名双保险。
    """
    candidates: list[tuple[str, str]] = []
    enc = el.find("enclosure")
    if enc is not None:
        candidates.append(((enc.get("url") or "").strip(), (enc.get("type") or "").lower()))
    for alt in el.findall("atom:link", NS):
        if alt.get("rel") == "enclosure":
            candidates.append(((alt.get("href") or "").strip(), (alt.get("type") or "").lower()))
    for url, mime in candidates:
        if not url:
            continue
        path = url.lower().split("?")[0]
        if mime.startswith("audio/"):
            return url
        if mime.startswith("video/") and path.endswith((".mp4", ".m4a", ".m4b")):
            return url
        if path.endswith(AUDIO_EXT):
            return url
    return ""


def parse_feed(raw: bytes, podcast: dict) -> list[dict]:
    """RSS 2.0 / Atom → 统一条目列表（不做去重）。"""
    root = parse_xml(raw)
    entries: list[ET.Element] = []
    is_atom = root.tag.endswith("}feed") or root.tag == "feed"
    if is_atom:
        entries = root.findall("atom:entry", NS) or root.findall("entry")
    else:
        channel = root.find("channel")
        if channel is None:
            channel = root
        entries = channel.findall("item")

    items: list[dict] = []
    for el in entries:
        title = strip_html(_first_text(el, ["title", "atom:title"]))
        link = _first_text(el, ["link"])
        if not link:
            for alt in el.findall("atom:link", NS):
                if alt.get("rel", "alternate") == "alternate" and alt.get("href"):
                    link = (alt.get("href") or "").strip()
                    break
        guid = _first_text(el, ["guid", "atom:id"])
        pub = parse_date(_first_text(el, ["pubDate", "atom:published", "atom:updated",
                                         "published", "updated", "dc:date"]))
        description = _first_text(el, ["description", "atom:summary", "summary"])
        content = _first_text(el, ["content:encoded", "atom:content", "content"])
        if not content and description and len(description) > 2000:
            content = description                 # 有的源把全文塞在 description
        audio = _pick_audio(el)
        duration = parse_duration(_first_text(el, ["itunes:duration"]))
        if not duration:
            m = re.search(r"(?:length|duration)[\"']?\s*[:=]\s*[\"']?(\d{3,6})", description)
            duration = int(m.group(1)) if m else 0

        key_src = guid or link or f"{title}|{pub.isoformat() if pub else ''}"
        items.append({
            "key": sha1(key_src),
            "title": title or "(无标题)",
            "link": link,
            "date": pub,
            "description": strip_html(description),
            "description_html": description,
            "content": content,
            "audio": audio,
            "duration": duration,
        })
    return [it for it in items if it["title"]]


# ------------------------------------------------------------------ HTML → Markdown

class _MarkdownParser(HTMLParser):
    """把 feed 正文/单集页 HTML 转成可读 Markdown。

    只保留结构（段落/标题/列表/引用/代码），丢弃图片与脚本；链接只留文字。
    """

    BLOCK_START = {
        "p": "\n\n", "div": "\n", "section": "\n", "blockquote": "\n\n> ",
        "pre": "\n\n", "hr": "\n\n---\n\n", "table": "\n", "tr": "\n",
        "ul": "\n", "ol": "\n", "figure": "\n", "figcaption": "\n",
    }
    HEADINGS = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### ",
                "h5": "##### ", "h6": "###### "}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0
        self.heading: str | None = None
        self.heading_buf: list[str] = []
        self.cell_first = True

    # -- 内部
    def _flush_heading(self) -> None:
        if self.heading is None:
            return
        text = re.sub(r"\s+", " ", "".join(self.heading_buf)).strip()
        self.heading_buf = []
        if text:
            # qiaomu 的 h2 尾巴带时间戳（数据来自 span），挪到标题前更好扫读
            m = re.search(r"^(.*?)\s+(\d{1,2}:\d{2}(?::\d{2})?)$", text)
            if m and m.group(1).strip():
                text = f"[{m.group(2)}] {m.group(1)}"
            self.parts.append(f"\n\n{self.HEADINGS[self.heading]}{text}\n\n")
        self.heading = None

    # -- HTMLParser 回调
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("script", "style", "noscript", "svg", "iframe"):
            self.skip_depth += 1
            return
        if tag in self.HEADINGS:
            self._flush_heading()
            self.heading = tag
            self.heading_buf = []
            return
        if tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in ("td", "th"):
            self.parts.append(" | " if not self.cell_first else "")
            self.cell_first = False
        elif tag in self.BLOCK_START:
            self.parts.append(self.BLOCK_START[tag])

    def handle_startendtag(self, tag, attrs):      # <br/> <img/>
        if tag.lower() == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style", "noscript", "svg", "iframe"):
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if tag in self.HEADINGS:
            self._flush_heading()
            return
        if tag == "li":
            self.parts.append("\n")
        elif tag in ("p", "blockquote", "pre", "figure"):
            self.parts.append("\n\n")
        elif tag == "tr":
            self.parts.append("\n")
            self.cell_first = True

    def handle_data(self, data):
        if self.skip_depth:
            return
        if self.heading is not None:
            self.heading_buf.append(data)
            return
        self.parts.append(data)

    # -- 结果
    def result(self) -> str:
        self._flush_heading()
        text = "".join(self.parts).replace("\u00a0", " ").replace("\r\n", "\n")
        lines: list[str] = []
        blank = 0
        for raw_line in text.split("\n"):
            line = re.sub(r"[ \t]+", " ", raw_line).strip()
            if not line:
                blank += 1
                if blank <= 1 and lines:
                    lines.append("")
                continue
            blank = 0
            lines.append(line)
        return "\n".join(lines).strip()


def html_to_markdown(raw: str) -> str:
    if not raw:
        return ""
    parser = _MarkdownParser()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:  # noqa: BLE001  解析器异常不能让整轮挂掉
        return strip_html(raw)
    return parser.result()


class _TextParser(HTMLParser):
    """抽纯文本（标题兜底用）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag.lower() in ("script", "style"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


_FEED_CTA = re.compile(
    r"(subscribe|newsletter|sign in|sign up|cookie|privacy policy|terms of service|"
    r"订阅|登录|注册|分享到|扫码|版权所有|广告)", re.I)


def extract_article(page_html: str, *, url: str = "", min_chars: int = 1200) -> str:
    """从单集页抽正文：优先 <article> → <main> → <body>，并滤掉导航噪声。"""
    for pattern in (r"(?is)<article\b[^>]*>(.*?)</article>",
                    r"(?is)<main\b[^>]*>(.*?)</main>",
                    r"(?is)<body\b[^>]*>(.*?)</body>"):
        m = re.search(pattern, page_html)
        if not m:
            continue
        text = html_to_markdown(m.group(1))
        lines = [ln for ln in text.split("\n") if ln.strip()]
        kept = [ln for ln in lines if not (len(ln) < 80 and _FEED_CTA.search(ln))]
        cleaned = "\n".join(kept).strip()
        if len(cleaned) >= min_chars:
            return cleaned
    return ""


# ------------------------------------------------------------------ 状态

class Store:
    """state.json：每个节目已见条目 + 抓取错误。只增不改，按条数截断。"""

    MAX_SEEN = 600

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"version": 1, "updated": "", "podcasts": {}, "runs": []}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("顶层不是对象")
                pods = loaded.get("podcasts", {})
                if not isinstance(pods, dict):
                    raise ValueError("podcasts 不是对象")
                for pid, pod in pods.items():
                    if not isinstance(pod, dict) or not isinstance(pod.get("seen") or {}, dict):
                        raise ValueError(f"节目 {pid} 的结构异常")
                self.data.update(loaded)
            except (OSError, ValueError) as exc:
                # 坏状态不静默覆盖：挪走再重建，日志留证据
                backup = path.with_suffix(f".broken-{int(time.time())}.json")
                try:
                    path.replace(backup)
                except OSError:
                    pass
                log(f"state.json 解析失败（{exc}），已备份到 {backup.name} 并重建")

    # -- 读取
    def pod(self, pid: str) -> dict:
        return self.data["podcasts"].setdefault(pid, {"seen": {}, "last_poll": "", "last_error": ""})

    def seen(self, pid: str) -> dict:
        return self.pod(pid).setdefault("seen", {})

    def has(self, pid: str, key: str) -> bool:
        return key in self.seen(pid)

    def mark(self, pid: str, key: str, meta: dict) -> None:
        self.seen(pid)[key] = meta

    def items_on(self, date: str, days: int = 1) -> list[dict]:
        """按「首次入档日期」取条目，date 为结束日（含），days 为回溯天数。"""
        end = datetime.strptime(date, "%Y-%m-%d").date()
        start = end - timedelta(days=max(1, days) - 1)
        out: list[dict] = []
        for pid, pod in self.data["podcasts"].items():
            for key, meta in (pod.get("seen") or {}).items():
                first = (meta.get("first") or "")[:10]
                try:
                    fday = datetime.strptime(first, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if meta.get("baseline") or not (start <= fday <= end):
                    continue
                out.append({"pid": pid, "key": key, **meta})
        out.sort(key=lambda x: (TIER_ORDER.get(str(x.get("tier", "B")).upper(), 9),
                                -int(x.get("score", 0)),
                                x.get("d", "")))
        return out

    # -- 写入
    def prune(self) -> None:
        for pod in self.data["podcasts"].values():
            seen = pod.get("seen") or {}
            if len(seen) <= self.MAX_SEEN:
                continue
            ordered = sorted(seen.items(), key=lambda kv: kv[1].get("first", ""), reverse=True)
            pod["seen"] = dict(ordered[: self.MAX_SEEN])

    def add_run(self, record: dict) -> None:
        runs = self.data.setdefault("runs", [])
        runs.append(record)
        del runs[:-50]

    def save(self) -> None:
        self.prune()
        self.data["updated"] = iso()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


# ------------------------------------------------------------------ 转写（可选）

def stt_backend() -> dict | None:
    """有 API 用 API；有本地命令用本地命令；都没有返回 None。"""
    base = (os.environ.get("GZH_STT_BASE_URL") or "").rstrip("/")
    key = os.environ.get("GZH_STT_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    if base and key:
        return {"kind": "api", "base": base, "key": key,
                "model": os.environ.get("GZH_STT_MODEL") or "whisper-1"}
    # 本地/远程自有转写器：命令形如 `stt-remote`，接受 `<音频> -o <输出目录>`，
    # 在输出目录里写 `<stem>.transcript.json`（数组 [{start,end,text}]）。
    gzh_cmd = (os.environ.get("GZH_STT_CMD") or "").strip()
    if gzh_cmd:
        return {"kind": "gzh", "cmd": gzh_cmd}
    for exe, kind in (("mlx_whisper", "mlx"), ("whisper-cli", "whispercpp"),
                      ("whisper-cpp", "whispercpp"), ("whisper", "whisper")):
        path = shutil.which(exe)
        if path:
            return {"kind": kind, "exe": path}
    return None


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = "----podwatch" + sha1(str(time.time()), 16)
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                      f"{value}\r\n".encode())
    for name, (filename, blob, mime) in files.items():
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
            f"filename=\"{filename}\"\r\nContent-Type: {mime}\r\n\r\n".encode())
        chunks.append(blob)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _stt_post(backend: dict, body: bytes, ctype: str) -> dict:
    req = Request(f"{backend['base']}/audio/transcriptions", data=body,
                  headers={"Authorization": f"Bearer {backend['key']}", "Content-Type": ctype})
    kwargs = {"timeout": 600}
    if _SSL is not None:
        kwargs["context"] = _SSL
    try:
        with urlopen(req, **kwargs) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except HTTPError as exc:
        raise FetchError(f"STT HTTP {exc.code}: {exc.read()[:300]!r}", exc.code) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"STT 请求失败：{exc}") from exc


def _api_transcribe(backend: dict, path: Path, *, language: str = "") -> dict:
    """OpenAI 兼容 /audio/transcriptions：先要 verbose_json（带时间戳），
    端点不支持时降级 json（只有整段文本）。"""
    blob = path.read_bytes()
    last: FetchError | None = None
    for fmt in ("verbose_json", "json"):
        fields = {"model": backend["model"], "response_format": fmt}
        if language:
            fields["language"] = language
        body, ctype = _multipart(fields, {"file": (path.name, blob, "application/octet-stream")})
        try:
            data = _stt_post(backend, body, ctype)
        except FetchError as exc:
            last = exc
            if fmt == "verbose_json" and exc.status in (400, 415, 422):
                continue
            raise
        segments = []
        for seg in data.get("segments") or []:
            segments.append({"start": float(seg.get("start", 0)), "end": float(seg.get("end", 0)),
                             "speaker": "", "text": (seg.get("text") or "").strip()})
        text = (data.get("text") or "").strip()
        if not segments and text:            # json 降级：整段一个伪段，至少能读
            segments = [{"start": 0.0, "end": max(30.0, len(text) / 5.0),
                         "speaker": "", "text": text}]
        return {"text": text, "segments": segments}
    raise last or FetchError("STT 请求失败")


def _run_local_stt(backend: dict, path: Path) -> dict:
    """本地 whisper CLI → 产出 srt，再解析成段。"""
    out_dir = Path(tempfile.mkdtemp(prefix="podwatch-stt-"))
    stem = out_dir / "out"
    kind = backend["kind"]
    if kind == "gzh":                      # 自有转写器（本地 .tools/stt 或 ECS 远程）
        cmd = [*shlex.split(backend["cmd"]), str(path), "-o", str(out_dir)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if proc.returncode != 0:
            raise FetchError(f"自有 STT 失败：{proc.stderr[-300:]}")
        seg_file = next(iter(sorted(out_dir.glob("*.transcript.json"))), None)
        if seg_file is None:
            raise FetchError("自有 STT 未产出 .transcript.json")
        data = json.loads(seg_file.read_text(encoding="utf-8"))
        segments = [{"start": float(s.get("start", 0)), "end": float(s.get("end", 0)),
                     "speaker": "", "text": (s.get("text") or "").strip()}
                    for s in data if (s.get("text") or "").strip()]
        return {"text": " ".join(s["text"] for s in segments), "segments": segments}
    if kind == "mlx":
        cmd = [backend["exe"], str(path), "--model", "mlx-community/whisper-large-v3-turbo",
               "--output-format", "srt", "--output-dir", str(out_dir), "--verbose", "False"]
    elif kind == "whispercpp":
        model = os.environ.get("GZH_STT_MODEL_PATH") or "models/ggml-base.bin"
        cmd = [backend["exe"], "-m", model, "-f", str(path), "-osrt", "-of", str(stem),
               "-l", "auto"]
    else:  # openai-whisper
        cmd = [backend["exe"], str(path), "--model", os.environ.get("GZH_STT_MODEL") or "base",
               "--output_format", "srt", "--output_dir", str(out_dir)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        raise FetchError(f"本地 STT 失败：{proc.stderr[-300:]}")
    srt = next(iter(out_dir.glob("*.srt")), None)
    if srt is None:
        raise FetchError("本地 STT 未产出 srt")
    return parse_srt_text(srt.read_text(encoding="utf-8", errors="replace"))


SRT_TIME = re.compile(r"(\d{1,2}:\d{2}:\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2})[,.](\d{1,3})")


def parse_srt_text(text: str) -> dict:
    segments = []
    for block in re.split(r"\n\s*\n", text.strip()):
        m = SRT_TIME.search(block)
        if not m:
            continue
        def secs(hms: str, ms: str) -> float:
            h, mi, s = [int(x) for x in hms.split(":")]
            return h * 3600 + mi * 60 + s + int(ms) / 1000.0
        start = secs(m.group(1), m.group(2))
        end = secs(m.group(3), m.group(4))
        body = block[m.end():].strip()
        body = re.sub(r"<[^>]+>", "", body)
        if body:
            segments.append({"start": start, "end": end, "speaker": "", "text": body})
    text = "\n".join(s["text"] for s in segments)
    return {"text": text, "segments": segments}


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def split_audio(path: Path, out_dir: Path, *, chunk_sec: int = 600) -> list[Path]:
    """大文件切片（25MB API 限制）：转 32k 单声道 mp3，按 10 分钟切。"""
    exe = _ffmpeg()
    if exe is None:
        raise FetchError("需要 ffmpeg 切片：brew install ffmpeg")
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(out_dir / "chunk_%03d.mp3")
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-i", str(path),
           "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
           "-f", "segment", "-segment_time", str(chunk_sec), "-reset_timestamps", "1",
           pattern]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        raise FetchError(f"ffmpeg 切片失败：{proc.stderr[-300:]}")
    chunks = sorted(out_dir.glob("chunk_*.mp3"))
    if not chunks:
        raise FetchError("ffmpeg 切片没有产出文件")
    return chunks


def transcribe_audio(audio_url: str, work_dir: Path, *, cfg: dict,
                     language: str = "") -> dict:
    """下载 → (必要时切片) → 转写，返回 {text, segments, backend}。"""
    backend = stt_backend()
    if backend is None:
        raise FetchError(
            "没有可用的转写后端：配置 GZH_STT_BASE_URL + GZH_STT_API_KEY（OpenAI 兼容），"
            "或安装 mlx_whisper / whisper-cli / whisper")
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        suffix = Path(urlparse(audio_url).path).suffix or ".mp3"
        audio = download_audio(audio_url, work_dir / f"episode{suffix}",
                               max_mb=int(cfg.get("audio_max_mb", 300)))
        size_mb = audio.stat().st_size / 1024 / 1024
        chunks = [audio] if size_mb <= 24 else split_audio(audio, work_dir / "chunks")
        all_segments: list[dict] = []
        texts: list[str] = []
        offset = 0.0
        for idx, chunk in enumerate(chunks):
            if backend["kind"] == "api":
                got = _api_transcribe(backend, chunk, language=language)
            else:
                got = _run_local_stt(backend, chunk)
            for seg in got["segments"]:
                all_segments.append({**seg, "start": seg["start"] + offset,
                                     "end": seg["end"] + offset})
            texts.append(got["text"])
            if got["segments"]:
                offset = all_segments[-1]["end"] if all_segments else offset
            else:
                offset += 600.0 * (idx + 1)
        if not all_segments and texts:
            all_segments = [{"start": 0.0, "end": float(len(texts) * 60), "speaker": "",
                             "text": "\n".join(texts)}]
        return {"text": "\n".join(texts).strip(), "segments": all_segments,
                "backend": backend["kind"]}
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)   # 成功失败都不留音频


def _stamp_seconds(stamp: str) -> float:
    parts = [int(x) for x in re.findall(r"\d+", stamp)]
    if len(parts) >= 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return float(parts[0]) if parts else 0.0


SECTION_TS = re.compile(r"^## \[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.*)$", re.M)


def markdown_sections_to_segments(markdown: str) -> list[dict]:
    """把「## [00:02:11] 小节标题」结构转成分段时间戳。

    qiaomu 精读自带章节时间戳；没有逐句时间戳时，用章节级时间戳给流水线
    一个可用的 transcript.json（引文仍以原文页回听为准）。
    """
    matches = list(SECTION_TS.finditer(markdown or ""))
    if len(matches) < 3:
        return []
    segments: list[dict] = []
    for idx, match in enumerate(matches):
        start = _stamp_seconds(match.group(1))
        end = (_stamp_seconds(matches[idx + 1].group(1))
               if idx + 1 < len(matches) else start + 600.0)
        tail = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdown)
        body = markdown[match.end():tail].strip()
        body = re.sub(r"\n{2,}", "\n", body)
        if not body:
            continue
        segments.append({"start": round(start, 3), "end": round(max(end, start + 1), 3),
                         "speaker": "", "text": body})
    return segments


def segments_to_markdown(segments: list[dict]) -> str:
    """带时间戳的转写正文（每段一个时间戳，方便回听）。"""
    lines = []
    for seg in segments:
        stamp = fmt_duration(int(seg["start"]))
        lines.append(f"[{stamp}] {seg['text'].strip()}")
    return "\n\n".join(lines).strip()


# ------------------------------------------------------------------ 配置 / 路径

def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"找不到配置：{path}")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(f"配置不是合法 JSON：{exc}")
    cfg.setdefault("defaults", {})
    if not isinstance(cfg.get("podcasts"), list):
        raise SystemExit(f"配置里 podcasts 必须是数组：{path}")
    for idx, pod in enumerate(cfg["podcasts"], 1):
        if not isinstance(pod, dict) or not pod.get("id"):
            raise SystemExit(f"配置第 {idx} 个节目缺 id：{pod!r}")
        if not isinstance(pod.get("feed", ""), str):
            raise SystemExit(f"配置第 {idx} 个节目的 feed 不是字符串：{pod!r}")
    cfg.setdefault("podcasts", [])
    defaults = {
        "baseline_days": 7,
        "min_fulltext_chars": 6000,
        "min_site_chars": 3000,
        "max_items_per_feed": 60,
        "baseline_max_per_feed": 3,
        "timeout": 30,
        "workers": 6,
        "audio_max_mb": 300,
    }
    for key, value in defaults.items():
        cfg["defaults"].setdefault(key, value)
    cfg.setdefault("paths", {})
    return cfg


def save_config(path: Path, cfg: dict) -> None:
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_out(cfg: dict, override: str = "") -> Path:
    if override:
        path = Path(override).expanduser()
        return path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()
    raw = (cfg.get("paths") or {}).get("out") or ""
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_absolute() else (ROOT / p).resolve()
    return DEFAULT_OUT


def find_podcast(cfg: dict, pid: str) -> dict | None:
    for pod in cfg["podcasts"]:
        if pod.get("id") == pid:
            return pod
    return None


def select_podcasts(cfg: dict, ids: list[str] | None) -> list[dict]:
    if not ids:
        return [p for p in cfg["podcasts"] if p.get("enabled", True)]
    chosen = []
    for pid in ids:
        pod = find_podcast(cfg, pid)
        if pod is None:
            raise SystemExit(f"配置里没有节目：{pid}（可用 check 或看 podcasts.json）")
        chosen.append(pod)
    return chosen


# ------------------------------------------------------------------ 抓取单期

KIND_LABEL_OK = ("feed_content", "site_page", "audio_stt")


def _declared_fulltext(podcast: dict) -> bool | str:
    value = podcast.get("fulltext", "auto")
    if isinstance(value, bool):
        return value
    return str(value).lower() if value else "auto"


def _follow_transcript_link(page_html: str, page_url: str, podcast: dict,
                             defaults: dict) -> str:
    """单集页正文太短时，在页面里找「transcript」链接再抓一层。

    只在同域名内、且链接包含本集 slug 时优先；最多试 2 个候选，避免抓串。
    """
    pattern = podcast.get("site_transcript_link")
    if pattern is None:
        pattern = "transcript"
    if not pattern:
        return ""
    slug = urlparse(page_url).path.rstrip("/").split("/")[-1]
    host = urlparse(page_url).netloc
    candidates: list[tuple[int, str]] = []
    for href in re.findall(r'href=["\']([^"\']+)["\']', page_html):
        href = htmllib.unescape(href).strip()
        if not href or not re.search(pattern, href, re.I):
            continue
        full = urljoin(page_url, href)
        if urlparse(full).netloc != host:
            continue
        score = 0
        if slug and slug in full:
            score += 2
        if full.rstrip("/") != page_url.rstrip("/"):
            score += 1
        candidates.append((score, full))
    # 有 slug 时只认「链接里带本集 slug」的候选，避免抓到别的集的 transcript
    ranked = sorted(set(candidates), key=lambda c: -c[0])
    if slug:
        chosen = [url for score, url in ranked if score >= 2][:2]
    else:
        chosen = [url for score, url in ranked if score >= 1][:1]
    for url in chosen:
        try:
            page, ctype = http_get(url, timeout=float(defaults["timeout"]))
            text = extract_article(decode_body(page, ctype), url=url,
                                   min_chars=int(defaults["min_site_chars"]))
            if text:
                return text
        except FetchError:
            continue
    return ""


def capture_item(cfg: dict, podcast: dict, item: dict, *, work_dir: Path,
                 dry_run: bool = False, quiet: bool = False) -> dict:
    """决定这一期能拿到什么内容：feed 全文 / 官网正文 / 音频转写 / 排队。"""
    defaults = cfg["defaults"]
    mode = str(podcast.get("transcript", "auto")).lower()
    feed_text = html_to_markdown(item.get("content") or "")
    declared = _declared_fulltext(podcast)
    min_full = int(defaults["min_fulltext_chars"])
    result = {"kind": "none", "text": "", "segments": [], "reason": ""}

    # ① feed 自带全文
    if feed_text and (declared is True or (declared == "auto" and len(feed_text) >= min_full)):
        if mode in ("auto", "feed"):
            return {"kind": "feed_content", "text": feed_text, "segments": [], "reason": ""}
    # ② 官网单集页（抽不到就跟随页面里的 transcript 链接，Lex 的转写就挂在独立页）
    if mode in ("auto", "site") and item.get("link"):
        try:
            page, ctype = http_get(item["link"], timeout=float(defaults["timeout"]))
            body = decode_body(page, ctype)
            text = extract_article(body, url=item["link"],
                                   min_chars=int(defaults["min_site_chars"]))
            if text:
                return {"kind": "site_page", "text": text, "segments": [], "reason": ""}
            followed = _follow_transcript_link(body, item["link"], podcast, defaults)
            if followed:
                return {"kind": "site_page", "text": followed, "segments": [], "reason": ""}
        except FetchError as exc:
            result["reason"] = f"官网抓取失败：{exc}"
    # ③ 音频转写
    if mode in ("auto", "audio") and item.get("audio"):
        backend = stt_backend()
        if backend is None:
            result.update(kind="queued",
                          reason=result["reason"] or "只有音频，未配置 STT 后端")
            return result
        if not defaults.get("auto_transcribe", True):
            result.update(kind="queued", reason="auto_transcribe=false，只入队不自动转写")
            return result
        if dry_run:
            result.update(kind="queued", reason="dry-run：跳过实际转写")
            return result
        try:
            got = transcribe_audio(item["audio"], work_dir, cfg=defaults,
                                   language=str(podcast.get("lang", "")))
            return {"kind": "audio_stt", "text": got["text"],
                    "segments": got["segments"], "reason": f"backend={got['backend']}"}
        except FetchError as exc:
            result.update(kind="queued", reason=f"转写失败：{exc}")
            return result
    if mode == "none":
        return {"kind": "none", "text": "", "segments": [],
                "reason": "配置为 transcript=none：只记标题与提要"}
    result["kind"] = "queued" if item.get("audio") else "none"
    if not result["reason"]:
        result["reason"] = "未配置转写路径" if result["kind"] == "queued" else "该期无正文、无音频"
    return result


def render_article(meta: dict, *, description: str = "", notes: str = "",
                   transcript: str = "", queued_reason: str = "",
                   audio_url: str = "") -> str:
    """一期一篇 Markdown：front matter + 提要 + 全文/待转写。"""
    lines = ["---"]
    for key in ("podcast", "podcast_id", "title", "date", "link", "audio", "duration",
                "tier", "lang", "transcript", "transcript_chars", "score",
                "captured_at", "site"):
        value = meta.get(key, "")
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines += ["---", "", f"# {meta.get('title', '')}", ""]

    bits = [f"节目：{meta.get('podcast', '')}（{meta.get('tier', '')} 级）",
            f"发布：{meta.get('date', '—')}",
            f"时长：{meta.get('duration') or '—'}"]
    lines.append("> " + " ｜ ".join(bits))
    if meta.get("link"):
        lines.append(f"> 链接：{meta['link']}")
    content_label = TR_LABEL.get(meta.get("transcript", "none"), "未知")
    if transcript:
        content_label += f"（{len(transcript)} 字）"
    elif notes:
        content_label += f" ｜ 节目笔记 {len(notes)} 字"
    lines.append(f"> 入档：{meta.get('captured_at', '')} ｜ 内容：{content_label}")
    lines.append("")

    if description:
        lines += ["## 提要", "", description.strip(), ""]

    if transcript:
        lines += ["## 全文", "", transcript.strip(), ""]
    elif meta.get("transcript") == "none":
        lines += ["## 存档说明", "",
                  "该节目在配置里是 `transcript=none`：只记录标题、提要和时间点，不下载音频、不进转写队列。",
                  "要全文请把 `podcasts.json` 里该节目的 `transcript` 改成 `audio`（需要 STT）或 `site`（官网有文字稿时）。",
                  ""]
    else:
        if notes:
            lines += ["## 节目笔记", "", notes.strip(), ""]
        lines += ["## 待转写", "",
                  "本期只抓到节目提要（和节目笔记），尚未拿到全文。", ""]
        if queued_reason:
            lines.append(f"- 原因：{queued_reason}")
        if audio_url:
            lines.append(f"- 音频：{audio_url}")
        lines.append("- 入队后运行：`python3 podwatch.py transcribe`（需先配置 STT 后端）")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------------ poll

def _fetch_one(podcast: dict, timeout: float) -> tuple[dict, list[dict], str]:
    feed = (podcast.get("feed") or "").strip()
    if not feed:
        return podcast, [], "配置里没有 feed 地址"
    try:
        raw, ctype = http_get(feed, timeout=timeout)
        items = parse_feed(raw, podcast)
        return podcast, items, ""
    except FetchError as exc:
        return podcast, [], str(exc)
    except ET.ParseError as exc:
        return podcast, [], f"XML 解析失败：{exc}"
    except Exception as exc:  # noqa: BLE001  单源异常不拖垮整轮
        return podcast, [], f"{type(exc).__name__}: {exc}"


def norm_title(title: str) -> str:
    """标题归一化：去标点/空白/大小写，用于识别同一集的多平台镜像。"""
    return re.sub(r"[^\w]+", "", (title or "").lower())[:90]


def _same_episode(item: dict, seen_dates: set[str], item_date: str) -> bool:
    """标题归一化相同、且发布日期相差 ≤2 天 → 视为同一集。"""
    try:
        d1 = datetime.strptime(item_date, "%Y-%m-%d").date()
    except ValueError:
        return bool(seen_dates)
    for raw in seen_dates:
        try:
            d2 = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            continue
        if abs((d1 - d2).days) <= 2:
            return True
    return False


def cmd_poll(args, cfg: dict) -> int:
    defaults = cfg["defaults"]
    out = resolve_out(cfg, args.out)
    store = Store(out / "state.json")
    podcasts = select_podcasts(cfg, args.podcast)
    if not podcasts:
        log("配置里没有节目", quiet=args.quiet)
        return 1
    since = None
    if args.since:
        try:
            since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=now_local().tzinfo)
        except ValueError:
            raise SystemExit("--since 格式应为 YYYY-MM-DD")
    baseline = since is None and not args.history

    log(f"轮询 {len(podcasts)} 个节目（workers={args.workers}）", quiet=args.quiet)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        fetched = list(pool.map(
            lambda p: _fetch_one(p, float(args.timeout)), podcasts))

    total_new = 0
    errors: dict[str, str] = {}
    new_items: list[dict] = []
    for podcast, items, err in fetched:
        pid = podcast["id"]
        pod_state = store.pod(pid)
        pod_state["last_error"] = err
        if err:
            errors[pid] = err
            log(f"  ✗ {podcast.get('name', pid)}：{err}", quiet=args.quiet)
            continue
        items = sorted(items, key=lambda it: it["date"].timestamp() if it["date"] else 0,
                       reverse=True)[: int(defaults["max_items_per_feed"])]
        seen = store.seen(pid)
        if (args.history or args.since) and seen:
            # 明确要求回灌时，把「上次只登记未入档」的基线条目放回候选
            for key in [k for k, m in seen.items()
                        if m.get("baseline") and not m.get("skip") and not m.get("dup")]:
                del seen[key]
        first_run = not seen
        cutoff = now_local() - timedelta(days=int(defaults["baseline_days"]))
        if since:
            cutoff = since
        seen_norm: dict[str, set[str]] = {}
        for meta in seen.values():
            nt = meta.get("nt")
            if nt:
                seen_norm.setdefault(nt, set()).add(meta.get("d", ""))
        batch_norm: dict[str, set[str]] = {}      # 本轮已收下的标题（同一批内双发去重）
        newest_allowed: set[str] = set()
        if first_run and baseline:
            keep = int(defaults.get("baseline_max_per_feed", 3))
            # items 已按时间倒序；最近 N 期才入档
            newest_allowed = {it["key"] for it in items[: max(0, keep)]}
        skip_res = []
        for pattern in podcast.get("skip") or []:
            try:
                skip_res.append(re.compile(pattern, re.I))
            except re.error as exc:
                log(f"  ! {podcast.get('name', pid)} 的 skip 正则无效（{pattern}）：{exc}",
                    quiet=args.quiet)
        got = 0
        for item in reversed(items):          # 旧→新，文件名/时间顺序稳定
            if item["key"] in seen:
                continue
            if skip_res and any(r.search(item["title"]) for r in skip_res):
                # 命中过滤规则：登记为已见，不入档也不进日报（比如日更的资讯栏目）
                store.mark(pid, item["key"], {"baseline": True, "skip": True,
                                              "t": item["title"],
                                              "nt": norm_title(item["title"]),
                                              "d": local_date(item["date"]),
                                              "first": iso(now_local())})
                continue
            if first_run and baseline and item["key"] not in newest_allowed:
                store.mark(pid, item["key"], {"baseline": True, "t": item["title"],
                                              "nt": norm_title(item["title"]),
                                              "d": local_date(item["date"]),
                                              "first": iso(now_local())})
                continue
            item_day = local_date(item["date"]) if item["date"] else local_date()
            nt = norm_title(item["title"])
            if nt and (_same_episode(item, seen_norm.get(nt, set()), item_day)
                       or _same_episode(item, batch_norm.get(nt, set()), item_day)):
                store.mark(pid, item["key"], {"baseline": True, "dup": True,
                                              "t": item["title"], "d": item_day,
                                              "nt": nt, "first": iso(now_local())})
                continue
            if baseline and item["date"] and item["date"] < cutoff:
                store.mark(pid, item["key"], {"baseline": True, "t": item["title"],
                                              "nt": norm_title(item["title"]),
                                              "d": local_date(item["date"]),
                                              "first": iso(now_local())})
                continue
            got += 1
            if nt:
                batch_norm.setdefault(nt, set()).add(item_day)
            new_items.append({"podcast": podcast, "item": item})
        pod_state["last_poll"] = iso()
        if got:
            log(f"  + {podcast.get('name', pid)}：{got} 期新节目", quiet=args.quiet)

    # 落盘：逐条抓内容/写文章（顺序执行，避免把站点打爆）
    if not args.dry_run:
        for rec in new_items:
            podcast, item = rec["podcast"], rec["item"]
            pid = podcast["id"]
            work = out / "转写" / pid / sha1(item["key"], 10)
            cap = capture_item(cfg, podcast, item, work_dir=work,
                               dry_run=False, quiet=args.quiet)
            desc = item["description"]
            feed_text = html_to_markdown(item.get("content") or "")
            notes = feed_text if (cap["kind"] not in KIND_LABEL_OK
                                  and len(feed_text) >= 600) else ""
            kind = cap["kind"]
            kind_for_meta = kind if kind != "none" or not notes else "feed_notes"
            score = score_item(podcast, item, kind)
            day = local_date(item["date"]) if item["date"] else local_date()
            stem = f"{day}_{slugify(item['title'])}"
            art_dir = out / "文章" / pid
            art_path = art_dir / f"{stem}.md"
            if art_path.exists():                     # 同日同标题：加哈希后缀
                art_path = art_dir / f"{stem}_{sha1(item['key'], 6)}.md"
            meta = {
                "podcast": podcast.get("name", pid),
                "podcast_id": pid,
                "title": item["title"],
                "date": day,
                "link": item["link"],
                "audio": item["audio"],
                "duration": fmt_duration(item["duration"]),
                "tier": str(podcast.get("tier", "B")).upper(),
                "lang": podcast.get("lang", ""),
                "transcript": kind_for_meta,
                "transcript_chars": len(cap["text"]) if cap["text"] else 0,
                "notes_chars": len(notes),
                "score": score,
                "captured_at": iso(),
                "site": podcast.get("site", ""),
                "desc": desc[:400],
                "queued_reason": cap["reason"] if kind == "queued" else "",
            }
            art_dir.mkdir(parents=True, exist_ok=True)
            art_path.write_text(render_article(
                meta, description=desc, notes=notes,
                transcript=cap["text"] if kind in KIND_LABEL_OK else "",
                queued_reason=cap["reason"] if kind == "queued" else "",
                audio_url=item["audio"]), encoding="utf-8")
            meta_path = art_path.with_suffix(".meta.json")
            meta_path.write_text(json.dumps(dict(meta, key=item["key"]), ensure_ascii=False,
                                            indent=2), encoding="utf-8")
            segs = list(cap["segments"])
            if not segs and kind in ("feed_content", "site_page"):
                segs = markdown_sections_to_segments(cap["text"])
            if segs:
                seg_path = out / "转写" / pid / f"{art_path.stem}.transcript.json"
                seg_path.parent.mkdir(parents=True, exist_ok=True)
                seg_path.write_text(json.dumps({
                    "source": item["link"] or item["audio"],
                    "count": len(segs),
                    "duration": segs[-1]["end"] if segs else 0.0,
                    "segments": segs,
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                shutil.rmtree(work, ignore_errors=True)   # 音频不留底
            seen_norm.setdefault(norm_title(item["title"]), set()).add(day)
            store.mark(pid, item["key"], {
                "t": item["title"],
                "nt": norm_title(item["title"]),
                "d": day,
                "first": iso(),
                "dur": meta["duration"],
                "desc": desc[:240],
                "link": item["link"],
                "art": str(art_path.relative_to(out)),
                "tr": kind_for_meta,
                "chars": meta["transcript_chars"],
                "score": score,
                "tier": meta["tier"],
            })
            total_new += 1
            if kind == "queued":
                append_queue(out, {
                    "pid": pid, "key": item["key"], "title": item["title"],
                    "link": item["link"], "audio": item["audio"], "date": day,
                    "art": str(art_path.relative_to(out)),
                    "reason": cap["reason"], "added": iso(), "attempts": 0,
                })

    store.add_run({"at": iso(), "new": total_new if not args.dry_run else len(new_items),
                   "errors": errors})
    if not args.dry_run:
        store.save()

    if args.dry_run:
        for rec in new_items:
            log(f"  [dry-run] {rec['podcast'].get('name')} · {rec['item']['title']}",
                quiet=args.quiet)
        log(f"dry-run：{len(new_items)} 期新节目（未写入任何文件）", quiet=args.quiet)
    else:
        log(f"完成：新增 {total_new} 期" + (f"，{len(errors)} 个源失败" if errors else ""),
            quiet=args.quiet)
        if errors and len(errors) == len(podcasts):
            log("全部源都失败（多半是网络/代理问题），本轮以退出码 2 结束", quiet=args.quiet)
            return 2
    return 0


def append_queue(out: Path, entry: dict) -> None:
    path = out / "队列" / "待转写.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_queue(out)
    if any(q.get("key") == entry.get("key") for q in existing):
        return
    limit = 500
    if len(existing) >= limit:                    # 长期没配 STT：丢最旧，防无限膨胀
        write_queue(out, existing[-(limit - 1):])
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_queue(out: Path) -> list[dict]:
    path = out / "队列" / "待转写.jsonl"
    if not path.exists():
        return []
    out_items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out_items.append(json.loads(line))
        except ValueError:
            continue
    return out_items


def write_queue(out: Path, entries: list[dict]) -> None:
    path = out / "队列" / "待转写.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
                   encoding="utf-8")
    tmp.replace(path)                       # 原子替换，避免半截文件


# ------------------------------------------------------------------ digest

def build_digest(store: Store, cfg: dict, *, date: str = "", days: int = 1,
                 limit: int = 0, out: Path | None = None,
                 tiers: list[str] | None = None) -> str:
    date = date or local_date()
    all_items = store.items_on(date, days)
    items = all_items
    if tiers:
        wanted = {t.upper() for t in tiers}
        items = [it for it in items if str(it.get("tier", "B")).upper() in wanted]
    hidden = len(all_items) - len(items)
    if limit:
        items = items[:limit]
    pod_map = {p["id"]: p for p in cfg["podcasts"]}
    by_tier: dict[str, list[dict]] = {}
    for it in items:
        by_tier.setdefault(it.get("tier", "B"), []).append(it)

    lines = [f"# 播客日报 · {date}", ""]
    if not items:
        if hidden:
            lines += [f"今天没有 S/A 级新节目；另有 {hidden} 期 B/C 级已入档，"
                      f"加 `--tiers S,A,B` 可看。", ""]
        else:
            lines += ["今天没有新节目。", ""]
    else:
        tier_count = " ｜ ".join(f"{t} {len(by_tier[t])}" for t in TIER_ORDER if t in by_tier)
        full = sum(1 for it in items if it.get("tr") in KIND_LABEL_OK)
        lines += [f"> 新增 {len(items)} 期（{tier_count}）｜已拿到全文 {full} 期"
                  f"｜待转写 {len(items) - full} 期", ""]
        if hidden:
            lines += [f"> 另有 {hidden} 期属于未展示分级（B/C），已入档不列出；"
                      f"加 `--tiers S,A,B` 可看全量。", ""]
        lines += ["## 今日重点", ""]
        # 优先推「已经拿到全文」的，让重点可直接点开读；没有全文的排后面
        highlights = ([it for it in items if it.get("tr") in KIND_LABEL_OK][:3]
                      or items[:3])
        for idx, it in enumerate(highlights, 1):
            lines.append(f"{idx}. **{it.get('t', '')}** — "
                         f"{pod_map.get(it['pid'], {}).get('name', it['pid'])}"
                         f"（{it.get('dur') or '—'}）{stars(it.get('score', 0))}")
        lines.append("")

    for tier in TIER_ORDER:
        group = by_tier.get(tier)
        if not group:
            continue
        lines += [f"## {TIER_LABEL[tier]}（{len(group)} 期）", ""]
        for idx, it in enumerate(group, 1):
            pod = pod_map.get(it["pid"], {})
            name = pod.get("name", it["pid"])
            tag = "🧾" if it.get("tr") in KIND_LABEL_OK else "⏳"
            lines.append(f"### {idx}. {it.get('t', '')}")
            lines.append("")
            lines.append(f"- 节目：{name} ｜ 发布：{it.get('d') or '—'} ｜ "
                         f"时长：{it.get('dur') or '—'} ｜ {stars(it.get('score', 0))}")
            if it.get("link"):
                lines.append(f"- 原文：{it['link']}")
            if it.get("art"):
                art = it["art"]
                base = (out or resolve_out(cfg, "")) / "输出"
                link_path = os.path.relpath((out or resolve_out(cfg, "")) / art, start=base)
                lines.append(f"- 存档：[{Path(art).name}]({link_path}) ｜ "
                             f"{tag} {TR_LABEL.get(it.get('tr', 'none'), '未知')}"
                             f"（{it.get('chars', 0)} 字）")
            if it.get("desc"):
                lines.append(f"- 提要：{it['desc']}")
            lines.append("")

    # 源健康 + 队列（只算启用中的源）
    enabled_pods = [p for p in cfg["podcasts"] if p.get("enabled", True)]
    enabled_ids = {p["id"] for p in enabled_pods}
    bad = [(pid, pod.get("last_error", "")) for pid, pod in store.data["podcasts"].items()
           if pid in enabled_ids and pod.get("last_error")]
    queue = read_queue(out or resolve_out(cfg, ""))
    lines += ["---", "", "## 订阅源状态", ""]
    total = len(enabled_pods)
    extra = len(cfg["podcasts"]) - total
    lines.append(f"- 正常：{total - len(bad)} / {total}"
                 + (f"（另有 {extra} 个停更/噪音源默认关闭）" if extra else ""))
    for pid, err in bad:
        lines.append(f"- ✗ {pod_map.get(pid, {}).get('name', pid)}：{err[:120]}")
    if queue:
        lines += ["", f"## 待转写队列（{len(queue)} 期）", "",
                  "配置 STT 后可批量补全文：`python3 podwatch.py transcribe`", ""]
        for entry in queue[:5]:
            lines.append(f"- {entry.get('date', '')} {entry.get('title', '')}"
                         f"（{entry.get('reason', '')}）")
        if len(queue) > 5:
            lines.append(f"- …另有 {len(queue) - 5} 期")
    lines.append("")
    return "\n".join(lines)


def cmd_digest(args, cfg: dict) -> int:
    out = resolve_out(cfg, args.out)
    store = Store(out / "state.json")
    date = args.date or local_date()
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise SystemExit(f"--date 格式应为 YYYY-MM-DD，收到：{date!r}")
    tiers = [t.strip() for t in (args.tiers or "").split(",") if t.strip()]
    if not tiers:
        tiers = list((cfg.get("digest") or {}).get("tiers") or [])
    text = build_digest(store, cfg, date=date, days=args.days, limit=args.limit,
                        out=out, tiers=tiers or None)
    if args.write:
        target = Path(args.write)
    elif args.days > 1:
        target = out / "输出" / f"最近{args.days}天.md"   # 多日视图不覆盖当日日报
    else:
        target = out / "输出" / f"日报_{date}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    if not args.quiet:
        print(text)
    log(f"日报已写入 {target}", quiet=args.quiet)
    return 0


# ------------------------------------------------------------------ transcribe

def cmd_transcribe(args, cfg: dict) -> int:
    out = resolve_out(cfg, args.out)
    entries = read_queue(out)
    if not entries:
        log("待转写队列为空", quiet=args.quiet)
        return 0
    backend = stt_backend()
    if backend is None:
        log("没有可用的转写后端。可选：\n"
            "  A. 配 OpenAI 兼容接口：export GZH_STT_BASE_URL=... GZH_STT_API_KEY=...\n"
            "  B. 装本地 whisper：brew install whisper-cpp 或 pip install mlx-whisper\n"
            f"队列里现有 {len(entries)} 期待转写。", quiet=args.quiet)
        return 3
    pod_map = {p["id"]: p for p in cfg["podcasts"]}
    done = failed = 0
    remaining: list[dict] = []
    done_keys: set[str] = set()
    failed_meta: dict[str, dict] = {}
    for entry in entries:
        if args.limit and done >= args.limit:
            remaining.append(entry)
            continue
        pid = entry.get("pid", "")
        podcast = pod_map.get(pid, {})
        work = out / "转写" / pid / sha1(str(entry.get("key", "")), 10)
        try:
            got = transcribe_audio(entry.get("audio", ""), work, cfg=cfg["defaults"],
                                   language=str(podcast.get("lang", "")))
        except FetchError as exc:
            entry["attempts"] = int(entry.get("attempts", 0)) + 1
            entry["reason"] = f"{exc}"
            remaining.append(entry)
            failed_meta[str(entry.get("key"))] = entry
            failed += 1
            log(f"  ✗ {entry.get('title')}：{exc}", quiet=args.quiet)
            continue
        if not got["text"]:
            entry["reason"] = "转写结果为空"
            remaining.append(entry)
            failed_meta[str(entry.get("key"))] = entry
            failed += 1
            continue
        update_article_transcript(out, entry, podcast, got)
        shutil.rmtree(work, ignore_errors=True)
        done_keys.add(str(entry.get("key")))
        done += 1
        log(f"  ✓ {entry.get('title')}（{len(got['text'])} 字）", quiet=args.quiet)
    # 重新读一次队列再按 key 合并：转写期间 poll 追加的条目不丢
    merged: list[dict] = []
    for current in read_queue(out):
        key = str(current.get("key"))
        if key in done_keys:
            continue
        merged.append(failed_meta.get(key, current))
    write_queue(out, merged)
    log(f"转写完成 {done} 期，失败 {failed} 期，队列剩 {len(merged)} 期", quiet=args.quiet)
    return 0


def update_article_transcript(out: Path, entry: dict, podcast: dict, got: dict) -> None:
    """转写成功后回填文章：替换「待转写」小节，写 segments 文件。"""
    art = out / entry["art"]
    meta_path = art.with_suffix(".meta.json")
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError:
            meta = {}
    body = segments_to_markdown(got["segments"]) if got["segments"] else got["text"]
    meta.update({"transcript": "audio_stt", "transcript_chars": len(got["text"]),
                 "queued_reason": ""})
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text(render_article(
        meta, description=meta.get("desc", ""), transcript=body,
        audio_url=meta.get("audio", "")), encoding="utf-8")
    if got["segments"]:
        seg_path = out / "转写" / entry["pid"] / f"{art.stem}.transcript.json"
        seg_path.parent.mkdir(parents=True, exist_ok=True)
        seg_path.write_text(json.dumps({
            "source": meta.get("link") or meta.get("audio"),
            "count": len(got["segments"]),
            "duration": got["segments"][-1]["end"] if got["segments"] else 0.0,
            "segments": got["segments"],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    # 更新 state 里对应条目的转写状态
    store = Store(out / "state.json")
    seen = store.seen(entry["pid"])
    if entry.get("key") in seen:
        seen[entry["key"]]["tr"] = "audio_stt"
        seen[entry["key"]]["chars"] = len(got["text"])
        store.save()


# ------------------------------------------------------------------ check / index / find / add / notify

def cmd_check(args, cfg: dict) -> int:
    defaults = cfg["defaults"]
    log(f"检查 {len(cfg['podcasts'])} 个订阅源…", quiet=args.quiet)
    rows = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        fetched = list(pool.map(lambda p: _fetch_one(p, float(defaults["timeout"])),
                                cfg["podcasts"]))
    for podcast, items, err in fetched:
        latest = max((it["date"] for it in items if it["date"]), default=None)
        gap = (now_local() - latest.astimezone()).days if latest else None
        rows.append({
            "id": podcast["id"],
            "name": podcast.get("name", podcast["id"]),
            "enabled": bool(podcast.get("enabled", True)),
            "tier": podcast.get("tier", ""),
            "status": "OK" if not err else "FAIL",
            "items": len(items),
            "latest": local_date(latest) if latest else "—",
            "gap": gap,
            "fulltext": podcast.get("fulltext", "auto"),
            "error": err,
        })
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        header = (pad("节目", 30) + pad("级", 4) + pad("开关", 6) + pad("状态", 6)
                  + pad("期数", 6) + pad("最近更新", 12) + pad("距今天", 8) + "全文")
        print(header)
        print("-" * cjk_width(header))
        for row in rows:
            line = (pad(row["name"][: 28], 30) + pad(row["tier"], 4)
                    + pad("开" if row["enabled"] else "关", 6) + pad(row["status"], 6)
                    + pad(str(row["items"]), 6) + pad(row["latest"], 12)
                    + pad("—" if row["gap"] is None else f"{row['gap']}天", 8)
                    + str(row["fulltext"]))
            print(line)
            if row["error"]:
                print(f"    ↳ {row['error'][:110]}")
    active = [r for r in rows if r["enabled"]]
    bad = [r for r in active if r["status"] != "OK"]
    stale = [r for r in active if r["gap"] is not None and r["gap"] > 180]
    if not args.json:
        print(f"\n启用 {len(active)} / {len(rows)} 个源；"
              f"失败 {len(bad)} 个；启用源中 {len(stale)} 个超过 180 天未更新。")
        if stale:
            print("  长期未更新：" + "、".join(r["name"] for r in stale))
    if bad:
        log(f"{len(bad)} 个源失败", quiet=args.quiet)
    return 0


def cmd_inbox(args, cfg: dict) -> int:
    """把日报里挑中的一期，按 orchestrator/inbox 的规则投递（可重复执行，幂等）。"""
    out = resolve_out(cfg, args.out)
    store = Store(out / "state.json")
    date = args.date or local_date()
    tiers = {t.strip().upper() for t in (args.tiers or "S,A").split(",") if t.strip()}
    items = [it for it in store.items_on(date, args.days)
             if str(it.get("tier", "B")).upper() in tiers
             and it.get("tr") in KIND_LABEL_OK]
    if args.pick:
        items = items[: args.pick]
    if not items:
        log("没有可投递的条目（需要已拿到全文的 S/A 级）", quiet=args.quiet)
        return 1
    inbox_dir = (Path(args.dir).expanduser().resolve() if args.dir
                 else ROOT / "gongzhonghao" / "orchestrator" / "inbox")
    inbox_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for it in items:
        stem = "播客_" + slugify(it.get("t", ""), 46)
        md_path = inbox_dir / f"{stem}.md"
        art = out / it["art"]
        seg = (out / "转写" / it["pid"] / f"{art.stem}.transcript.json")
        if md_path.exists():
            log(f"  跳过（已投递）：{md_path.name}", quiet=args.quiet)
            continue
        if args.dry_run:
            log(f"  [dry-run] 将投递：{stem}", quiet=args.quiet)
            continue
        note = f"来源：{it.get('pid')}｜{it.get('d')}｜{it.get('dur') or ''}｜存档：{it.get('art')}"
        md_path.write_text(
            f"# {it.get('t', '')}\n\n{it.get('link', '')}\n\n{note}\n", encoding="utf-8")
        if seg.exists():
            shutil.copyfile(seg, inbox_dir / f"{stem}.json")
        written += 1
        log(f"  + {md_path.name}", quiet=args.quiet)
    log(f"投递 {written} 期到 {inbox_dir}", quiet=args.quiet)
    log("下一步：cd gongzhonghao && python3 -m orchestrator.loop tick --provider stub"
        "（真跑换 openai-compat，先看 dry-run）", quiet=args.quiet)
    return 0


def cmd_index(args, cfg: dict) -> int:
    out = resolve_out(cfg, args.out)
    store = Store(out / "state.json")
    print(pad("节目", 32) + pad("级", 4) + pad("已入档", 8) + pad("全文", 6) + "最近入档")
    print("-" * 78)
    for podcast in cfg["podcasts"]:
        pid = podcast["id"]
        seen = {k: v for k, v in store.seen(pid).items()
                if not v.get("baseline") and not v.get("skip")}
        total = len(seen)
        full = sum(1 for m in seen.values() if m.get("tr") in KIND_LABEL_OK)
        latest = max((m.get("first", "") for m in seen.values()), default="")
        print(pad(podcast.get("name", pid)[:30], 32) + pad(str(podcast.get("tier", "")), 4)
              + pad(str(total), 8) + pad(str(full), 6) + (latest[:16] or "—"))
    queue = read_queue(out)
    if queue:
        print(f"\n待转写队列：{len(queue)} 期")
    return 0


def itunes_search(keyword: str, limit: int = 8) -> list[dict]:
    from urllib.parse import quote
    url = (f"https://itunes.apple.com/search?term={quote(keyword)}"
           f"&entity=podcast&limit={limit}&country=US")
    raw, ctype = http_get(url, timeout=20)
    data = json.loads(decode_body(raw, ctype))
    results = []
    for item in data.get("results", []):
        results.append({
            "name": str(item.get("collectionName") or item.get("trackName") or "").strip('"\u201c\u201d '),
            "artist": item.get("artistName"),
            "feed": item.get("feedUrl"),
            "count": item.get("trackCount"),
            "genre": item.get("primaryGenreName"),
            "country": item.get("country"),
        })
    return results


def cmd_find(args, cfg: dict) -> int:
    results = itunes_search(args.keyword, args.limit)
    if not results:
        print("没有结果（换个关键词试试，中文节目可用拼音/英文名）")
        return 1
    for idx, row in enumerate(results, 1):
        print(f"{idx}. {row['name']}")
        print(f"   播主：{row['artist']} ｜ 类型：{row['genre']} ｜ 期数：{row['count']}")
        print(f"   RSS：{row['feed']}")
    return 0


def cmd_add(args, cfg: dict, config_path: Path) -> int:
    results = itunes_search(args.keyword, args.limit)
    if not results:
        print("没有搜到，换个关键词")
        return 1
    idx = max(1, min(len(results), int(args.pick))) - 1
    row = results[idx]
    pid = re.sub(r"[^a-z0-9]+", "-", (args.id or row["name"] or "podcast").lower()).strip("-")
    if not pid:
        pid = sha1(row["feed"] or row["name"], 8)
    if find_podcast(cfg, pid) is not None:
        pid = f"{pid}-{sha1(row['feed'] or '', 4)}"
    entry = {
        "id": pid,
        "name": row["name"],
        "lang": args.lang,
        "tier": args.tier.upper(),
        "feed": row["feed"],
        "site": "",
        "fulltext": "auto",
        "transcript": args.transcript,
        "note": f"{row['artist']}｜{row['genre']}｜按 {args.tier.upper()} 级新加，理由待补",
        "tags": [],
    }
    cfg["podcasts"].append(entry)
    save_config(config_path, cfg)
    print(json.dumps(entry, ensure_ascii=False, indent=2))
    log(f"已写入 {config_path}（{len(cfg['podcasts'])} 个节目）")
    return 0


def notify_feishu(text: str, webhook: str) -> bool:
    if not webhook:
        return False
    payload = json.dumps({"msg_type": "text",
                          "content": {"text": text[:15000]}}).encode()
    req = Request(webhook, data=payload, headers={"Content-Type": "application/json"})
    kwargs = {"timeout": 10}
    if _SSL is not None:
        kwargs["context"] = _SSL
    try:
        with urlopen(req, **kwargs) as resp:
            body = resp.read().decode("utf-8", "replace")
        ok = '"code":0' in body.replace(" ", "") or '"StatusCode":0' in body.replace(" ", "")
        log("飞书推送" + ("成功" if ok else f"返回异常：{body[:120]}"))
        return ok
    except (URLError, OSError, TimeoutError) as exc:
        log(f"飞书推送失败：{exc}")
        return False


def cmd_cron(args, cfg: dict) -> int:
    """打印可直接粘贴的 crontab 行（绝对路径已解析，避免手抄出错）。"""
    here = Path(__file__).resolve()
    out = resolve_out(cfg, args.out)
    notify = " --notify" if not args.no_notify else ""
    line = (f"10 8 * * * mkdir -p {out}/日志 && cd {here.parent} "
            f"&& /usr/bin/python3 {here} run{notify} >> {out}/日志/cron.log 2>&1")
    print("把下面一行加进 `crontab -e`：\n")
    print(line)
    print(f"\n日志会写到 {out}/日志/cron.log；macOS 若报 Operation not permitted，"
          f"改用 README §7 的 launchd 方案。")
    print("推送飞书需要先 export FEISHU_WEBHOOK=...；不推送就去掉 --notify。")
    return 0


def cmd_run(args, cfg: dict) -> int:
    args.podcast = getattr(args, "podcast", None)
    args.out = getattr(args, "out", "")
    args.workers = getattr(args, "workers", cfg["defaults"].get("workers", 6))
    args.timeout = getattr(args, "timeout", cfg["defaults"].get("timeout", 30))
    rc = cmd_poll(args, cfg)
    if rc != 0:
        return rc
    out = resolve_out(cfg, args.out)
    store = Store(out / "state.json")
    date = local_date()
    daily = out / "输出" / f"日报_{date}.md"
    daily.parent.mkdir(parents=True, exist_ok=True)
    run_tiers = list((cfg.get("digest") or {}).get("tiers") or [])
    text = build_digest(store, cfg, date=date, days=1, out=out, tiers=run_tiers or None)
    daily.write_text(text, encoding="utf-8")
    weekly = out / "输出" / "最近7天.md"
    weekly.write_text(build_digest(store, cfg, date=date, days=7, out=out,
                                   tiers=run_tiers or None), encoding="utf-8")
    print(text)
    log(f"日报：{daily}")
    if args.notify:
        webhook = os.environ.get((cfg.get("integrations") or {}).get("feishu_webhook_env",
                                                                    "FEISHU_WEBHOOK"), "")
        if not notify_feishu(text, webhook):
            log("未推送（没配 FEISHU_WEBHOOK 或推送失败）")
    return 0


# ------------------------------------------------------------------ 入口

def _common_options() -> argparse.ArgumentParser:
    """让 --config / --out / --quiet 放在子命令前后都能用。"""
    c = argparse.ArgumentParser(add_help=False)
    c.add_argument("--config", default=argparse.SUPPRESS)
    c.add_argument("--out", default=argparse.SUPPRESS)
    c.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS)
    return c


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="podwatch", description="播客自动监控：RSS 轮询 → 全文/转写 → 每日日报")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG), help="配置文件路径")
    ap.add_argument("--out", default="", help="数据目录（默认读配置 paths.out）")
    ap.add_argument("--quiet", action="store_true", help="只打结果，不打过程日志")
    common = _common_options()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_poll = sub.add_parser("poll", help="抓一轮新节目", parents=[common])
    p_poll.add_argument("--podcast", action="append", help="只抓指定 id（可多次）")
    p_poll.add_argument("--since", default="", help="只入档该日期之后发布的（YYYY-MM-DD）")
    p_poll.add_argument("--history", action="store_true", help="首次接入时回灌全部历史")
    p_poll.add_argument("--dry-run", action="store_true", help="只列出新条目，不写文件")
    p_poll.add_argument("--workers", type=int, default=6)
    p_poll.add_argument("--timeout", type=float, default=30)
    p_poll.set_defaults(func=cmd_poll)

    p_dig = sub.add_parser("digest", help="生成日报", parents=[common])
    p_dig.add_argument("--date", default="", help="日报日期 YYYY-MM-DD（默认今天）")
    p_dig.add_argument("--days", type=int, default=1, help="回溯天数（默认 1）")
    p_dig.add_argument("--limit", type=int, default=0, help="最多列出多少期")
    p_dig.add_argument("--tiers", default="", help="只列这些分级，如 S,A（默认读配置 digest.tiers）")
    p_dig.add_argument("--write", default="", help="输出路径（默认 输出/日报_日期.md）")
    p_dig.set_defaults(func=cmd_digest)

    p_check = sub.add_parser("check", help="订阅源健康检查", parents=[common])
    p_check.add_argument("--workers", type=int, default=6)
    p_check.add_argument("--json", action="store_true")
    p_check.set_defaults(func=cmd_check)

    p_index = sub.add_parser("index", help="按节目统计", parents=[common])
    p_index.set_defaults(func=cmd_index)

    p_tr = sub.add_parser("transcribe", help="处理待转写队列", parents=[common])
    p_tr.add_argument("--limit", type=int, default=0)
    p_tr.set_defaults(func=cmd_transcribe)

    p_find = sub.add_parser("find", help="Apple 播客搜索", parents=[common])
    p_find.add_argument("--keyword", required=True)
    p_find.add_argument("--limit", type=int, default=8)
    p_find.set_defaults(func=cmd_find)

    p_add = sub.add_parser("add", help="搜索并写入配置", parents=[common])
    p_add.add_argument("--keyword", required=True)
    p_add.add_argument("--pick", type=int, default=1, help="选第几个搜索结果")
    p_add.add_argument("--id", default="", help="自定义 id")
    p_add.add_argument("--tier", default="A")
    p_add.add_argument("--lang", default="en")
    p_add.add_argument("--transcript", default="auto",
                       choices=["auto", "feed", "site", "audio", "none"])
    p_add.add_argument("--limit", type=int, default=5)
    p_add.set_defaults(func=cmd_add)

    p_inbox = sub.add_parser("inbox", help="把日报里挑中的一期投进出稿流水线", parents=[common])
    p_inbox.add_argument("--date", default="", help="取哪天入档的（默认今天）")
    p_inbox.add_argument("--days", type=int, default=1)
    p_inbox.add_argument("--tiers", default="S,A")
    p_inbox.add_argument("--pick", type=int, default=0, help="只投前 N 期（0=全部）")
    p_inbox.add_argument("--dir", default="", help="投递目录（默认 gongzhonghao/orchestrator/inbox）")
    p_inbox.add_argument("--dry-run", action="store_true", default=False)
    p_inbox.set_defaults(func=cmd_inbox)

    p_cron = sub.add_parser("cron", help="打印 crontab 安装行", parents=[common])
    p_cron.add_argument("--no-notify", action="store_true", help="不推飞书")
    p_cron.set_defaults(func=cmd_cron)

    p_run = sub.add_parser("run", help="poll + digest（cron 用）", parents=[common])
    p_run.add_argument("--notify", action="store_true", help="推送日报到飞书")
    p_run.add_argument("--podcast", action="append", default=None)
    p_run.add_argument("--since", default="")
    p_run.add_argument("--history", action="store_true")
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--workers", type=int, default=6)
    p_run.add_argument("--timeout", type=float, default=30)
    p_run.set_defaults(func=cmd_run)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    config_path = Path(args.config).expanduser().resolve()
    cfg = load_config(config_path)
    try:
        if args.cmd == "add":
            return cmd_add(args, cfg, config_path)
        return args.func(args, cfg)
    except KeyboardInterrupt:
        log("已中断")
        return 130
    except FetchError as exc:
        log(f"网络错误：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
