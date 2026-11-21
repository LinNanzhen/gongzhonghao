#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""podwatch 单测：全部离线（本地 http.server 提供 fixture feed）。

跑法：
    cd gongzhonghao/podwatch && python3 -m unittest test_podwatch -v
或（仓库根）：
    python3 -m unittest discover -s podwatch -p 'test_*.py' -t . -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import (BaseHTTPRequestHandler, SimpleHTTPRequestHandler,
                         ThreadingHTTPServer)
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podwatch as pw  # noqa: E402


# ------------------------------------------------------------------ fixture

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
<channel>
  <title>测试播客</title>
  <link>https://example.com</link>
  <item>
    <title><![CDATA[第 2 期：和 &nbsp;CEO 聊 AI 芯片]]></title>
    <link>https://example.com/ep2</link>
    <guid isPermaLink="false">ep-002</guid>
    <pubDate>{date2}</pubDate>
    <description><![CDATA[第二期提要 & 一些说明]]></description>
    <content:encoded><![CDATA[
      <p>这是第二期的正文第一段，讲大模型与算力。</p>
      <h2 data-t="0:02:11" data-title="开场">开场 <span class="ts-link">0:02:11</span></h2>
      <p>第二段，含<strong>强调</strong>和<a href="https://x.com">链接文字</a>。</p>
      <ul><li>要点 A</li><li>要点 B</li></ul>
      <script>console.log(1)</script>
    ]]></content:encoded>
    <enclosure url="https://example.com/ep2.mp3" length="1234" type="audio/mpeg"/>
    <itunes:duration>1:02:03</itunes:duration>
  </item>
  <item>
    <title>第 1 期：封面图不能当音频</title>
    <link>https://example.com/ep1</link>
    <guid>ep-001</guid>
    <pubDate>{date1}</pubDate>
    <description>第一期提要</description>
    <content:encoded><![CDATA[<p>第一期正文。</p>]]></content:encoded>
    <enclosure url="https://example.com/cover.jpg" length="99" type="image/jpeg"/>
    <itunes:duration>3723</itunes:duration>
  </item>
</channel>
</rss>
"""

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom 测试</title>
  <entry>
    <title>Atom 单集</title>
    <id>atom-1</id>
    <link rel="alternate" href="https://example.com/atom-1"/>
    <link rel="enclosure" href="https://example.com/atom-1.m4a" type="audio/mp4"/>
    <published>2026-11-01T08:00:00+00:00</published>
    <summary>Atom 提要</summary>
    <content type="html">&lt;p&gt;Atom 正文很长...&lt;/p&gt;</content>
  </entry>
</feed>
"""

BROKEN = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>T</title>
<item><title>坏实体 &nbsp; & 裸与号</title><pubDate>2026-11-02 10:00:00</pubDate>
<description>正文</description></item>
</channel></rss>
"""

PAGE = """<html><head><title>单集页</title><style>body{}</style></head><body>
<nav>subscribe | sign in | cookie</nav>
<article>
<h1>第 3 期</h1>
<p>这是官网抓到的正文段落，长度需要足够长才会被接受。</p>
<p>{filler}</p>
</article>
<footer>版权所有</footer></body></html>
"""


def make_config(tmp: Path, feed_url: str, **podcast_overrides) -> tuple[Path, Path]:
    out = tmp / "out"
    podcast = {
        "id": "demo",
        "name": "测试播客",
        "lang": "zh",
        "tier": "S",
        "feed": feed_url,
        "site": "https://example.com",
        "fulltext": True,
        "transcript": "feed",
    }
    podcast.update(podcast_overrides)
    cfg = {
        "version": 1,
        "defaults": {"baseline_days": 7, "min_fulltext_chars": 100,
                     "min_site_chars": 50, "max_items_per_feed": 60,
                     "timeout": 5, "workers": 2, "audio_max_mb": 10},
        "paths": {"out": str(out)},
        "podcasts": [podcast],
    }
    cfg_path = tmp / "podcasts.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return cfg_path, out


def quiet_main(argv: list[str]) -> int:
    return pw.main(argv)


class _Server:
    """临时目录起一个静态 http 服务，托管 fixture feed。"""

    def __init__(self, directory: Path):
        class _Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *args):        # 测试里不要刷 GET 日志
                pass

        handler = lambda *a, **kw: _Quiet(*a, directory=str(directory), **kw)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/feed.xml"

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def dates() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    return (now - timedelta(days=2)).strftime("%a, %d %b %Y %H:%M:%S +0000"), \
           (now - timedelta(days=1)).strftime("%a, %d %b %Y %H:%M:%S +0000")


# ------------------------------------------------------------------ 解析层

class TestParsing(unittest.TestCase):
    def test_parse_rss_fields_and_enclosure_guard(self):
        d1, d2 = dates()
        items = pw.parse_feed(RSS.format(date1=d1, date2=d2).encode(), {"id": "demo"})
        self.assertEqual(len(items), 2)
        ep2 = next(i for i in items if i["key"] and "ep2" in i["link"])
        self.assertEqual(ep2["title"], "第 2 期：和 CEO 聊 AI 芯片")
        self.assertEqual(ep2["audio"], "https://example.com/ep2.mp3")
        self.assertEqual(ep2["duration"], 3723)
        self.assertTrue(items[0]["content"], "content:encoded 应解析进 content 字段")
        # 封面图（image/jpeg）不能被当成音频
        ep1 = next(i for i in items if "ep1" in i["link"])
        self.assertEqual(ep1["audio"], "")
        self.assertEqual(ep1["duration"], 3723)

    def test_guid_is_dedupe_key(self):
        d1, d2 = dates()
        a = pw.parse_feed(RSS.format(date1=d1, date2=d2).encode(), {})
        b = pw.parse_feed(RSS.format(date1=d1, date2=d2).encode(), {})
        self.assertEqual([i["key"] for i in a], [i["key"] for i in b])

    def test_atom_feed(self):
        items = pw.parse_feed(ATOM.encode(), {})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["link"], "https://example.com/atom-1")
        self.assertEqual(items[0]["audio"], "https://example.com/atom-1.m4a")
        self.assertIsNotNone(items[0]["date"])

    def test_broken_entities_recovered(self):
        items = pw.parse_feed(BROKEN.encode(), {})
        self.assertEqual(len(items), 1)
        self.assertIn("裸与号", items[0]["title"])

    def test_dates(self):
        self.assertIsNotNone(pw.parse_date("Wed, 21 Oct 2026 00:00:00 +0000"))
        self.assertIsNotNone(pw.parse_date("2026-11-01T08:00:00+00:00"))
        self.assertIsNotNone(pw.parse_date("2026-11-02 10:00:00"))
        self.assertIsNone(pw.parse_date("not a date"))
        self.assertIsNone(pw.parse_date(""))

    def test_durations(self):
        self.assertEqual(pw.parse_duration("3723"), 3723)
        self.assertEqual(pw.parse_duration("1:02:03"), 3723)
        self.assertEqual(pw.parse_duration("62:03"), 3723)
        self.assertEqual(pw.parse_duration(""), 0)
        self.assertEqual(pw.parse_duration("abc"), 0)

    def test_slugify_is_filesystem_and_markdown_safe(self):
        for raw in ("a/b:c*d?e", "[AINews] x & y", "“银行也有百亿预算”｜张璐", "   "):
            slug = pw.slugify(raw)
            self.assertTrue(slug)
            self.assertNotIn("/", slug)
            self.assertNotRegex(slug, r"[\s\[\]()|｜“”&,，。:?*\"<>]")


class TestHtml(unittest.TestCase):
    def test_html_to_markdown_structure(self):
        d1, d2 = dates()
        item = pw.parse_feed(RSS.format(date1=d1, date2=d2).encode(), {})[0]
        md = pw.html_to_markdown(item["content"])
        self.assertIn("## ", md)                       # h2 标题
        self.assertIn("大模型与算力", md)               # 段落文字
        self.assertIn("要点 A", md)                     # 列表
        self.assertNotIn("console.log", md)             # script 丢弃
        self.assertIn("[0:02:11]", md)                  # 时间戳挪到标题前

    def test_extract_article_skips_nav(self):
        page = PAGE.replace("{filler}", "正文补充。" * 80)
        text = pw.extract_article(page, url="https://example.com/ep3", min_chars=100)
        self.assertGreater(len(text), 200)
        self.assertIn("官网抓到的正文", text)
        self.assertNotIn("订阅", text, "短导航行应被滤掉")

    def test_extract_article_rejects_short_page(self):
        short = "<html><body><article><p>太短了</p></article></body></html>"
        self.assertEqual(pw.extract_article(short), "")


class TestRender(unittest.TestCase):
    def _meta(self, **kw):
        base = {"podcast": "测试播客", "podcast_id": "demo", "title": "第 1 期",
                "date": "2026-11-01", "link": "https://x", "audio": "",
                "duration": "1:00:00", "tier": "S", "lang": "zh",
                "transcript": "feed_content", "transcript_chars": 10,
                "score": 8, "captured_at": "2026-11-10T01:00:00+08:00", "site": ""}
        base.update(kw)
        return base

    def test_article_with_transcript(self):
        md = pw.render_article(self._meta(), description="提要", transcript="正文")
        self.assertIn("# 第 1 期", md)
        self.assertIn("## 提要", md)
        self.assertIn("## 全文", md)
        self.assertIn('podcast: "测试播客"', md)

    def test_article_queued_keeps_notes(self):
        md = pw.render_article(self._meta(transcript="queued"), description="提要",
                               notes="节目笔记内容", queued_reason="只有音频")
        self.assertIn("## 节目笔记", md)
        self.assertIn("## 待转写", md)
        self.assertIn("只有音频", md)
        self.assertNotIn("## 全文", md)

    def test_article_notes_only(self):
        md = pw.render_article(self._meta(transcript="feed_notes"), description="提要",
                               notes="笔记")
        self.assertIn("## 节目笔记", md)
        self.assertNotIn("## 全文", md)

    def test_digest_groups_by_tier(self):
        store = pw.Store(Path(tempfile.mkdtemp()) / "state.json")
        now = pw.iso()
        store.mark("demo", "k1", {"t": "S 级一条", "d": "2026-11-10", "first": now,
                                  "tier": "S", "score": 10, "tr": "feed_content",
                                  "chars": 100, "art": "文章/demo/x.md", "desc": "d"})
        store.mark("demo", "k2", {"t": "B 级一条", "d": "2026-11-10", "first": now,
                                  "tier": "B", "score": 2, "tr": "queued",
                                  "chars": 0, "art": "文章/demo/y.md", "desc": "d"})
        store.mark("demo", "k3", {"t": "基线旧闻", "d": "2026-11-10", "first": now,
                                  "baseline": True, "tier": "S", "score": 1})
        cfg = {"podcasts": [{"id": "demo", "name": "测试播客", "tier": "S"}], "defaults": {}}
        text = pw.build_digest(store, cfg, date=pw.local_date(), out=Path(tempfile.mkdtemp()))
        self.assertIn("S 级 · 必追", text)
        self.assertIn("B 级 · 扫标题", text)
        self.assertIn("S 级一条", text)
        self.assertNotIn("基线旧闻", text, "baseline 条目不应进日报")
        self.assertIn("新增 2 期", text)


class TestStore(unittest.TestCase):
    def test_prune_keeps_newest(self):
        store = pw.Store(Path(tempfile.mkdtemp()) / "s.json")
        for i in range(pw.Store.MAX_SEEN + 20):
            store.mark("demo", f"k{i}", {"first": f"2026-01-{(i % 28) + 1:02d}T00:00:0{i % 10}+00:00"})
        store.prune()
        self.assertEqual(len(store.seen("demo")), pw.Store.MAX_SEEN)

    def test_broken_state_is_backed_up_not_crashed(self):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "state.json"
        path.write_text("{ 这不是 json", encoding="utf-8")
        store = pw.Store(path)                       # 不抛异常
        self.assertEqual(store.data["version"], 1)
        self.assertTrue(list(tmp.glob("state.broken-*.json")), "坏状态应被备份")

    def test_items_on_filters_date_and_baseline(self):
        store = pw.Store(Path(tempfile.mkdtemp()) / "s.json")
        today = pw.local_date()
        yesterday = (datetime.now().astimezone() - timedelta(days=1)).strftime("%Y-%m-%d")
        store.mark("demo", "a", {"first": f"{today}T08:00:00+08:00", "t": "今天"})
        store.mark("demo", "b", {"first": f"{yesterday}T08:00:00+08:00", "t": "昨天"})
        store.mark("demo", "c", {"first": f"{today}T08:00:00+08:00", "t": "基线",
                                "baseline": True})
        self.assertEqual([i["t"] for i in store.items_on(today, days=1)], ["今天"])
        self.assertEqual(len(store.items_on(today, days=2)), 2)


# ------------------------------------------------------------------ 端到端

class TestEndToEnd(unittest.TestCase):
    def test_poll_idempotent_and_digest(self):
        d1, d2 = dates()
        tmp = Path(tempfile.mkdtemp())
        site_dir = tmp / "site"
        site_dir.mkdir()
        (site_dir / "feed.xml").write_text(RSS.format(date1=d1, date2=d2), encoding="utf-8")
        with _Server(site_dir) as feed_url:
            cfg_path, out = make_config(tmp, feed_url)
            base = ["--config", str(cfg_path), "--out", str(out), "--quiet"]
            self.assertEqual(quiet_main(base + ["poll"]), 0)
            arts = list((out / "文章" / "demo").glob("*.md"))
            self.assertEqual(len(arts), 2, "两期都该入档")
            self.assertTrue((out / "state.json").exists())
            # 幂等：再跑一次不应重复入档
            self.assertEqual(quiet_main(base + ["poll"]), 0)
            self.assertEqual(len(list((out / "文章" / "demo").glob("*.md"))), 2)
            # 日报
            self.assertEqual(quiet_main(base + ["digest"]), 0)
            daily = out / "输出" / f"日报_{pw.local_date()}.md"
            self.assertTrue(daily.exists())
            text = daily.read_text(encoding="utf-8")
            self.assertIn("新增 2 期", text)
            self.assertIn("全文（RSS 自带）", text)

    def test_bad_feed_does_not_crash_poll(self):
        d1, d2 = dates()
        tmp = Path(tempfile.mkdtemp())
        site_dir = tmp / "site"
        site_dir.mkdir()
        (site_dir / "feed.xml").write_text(RSS.format(date1=d1, date2=d2), encoding="utf-8")
        with _Server(site_dir) as feed_url:
            cfg_path, out = make_config(tmp, feed_url)
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["podcasts"].append({"id": "dead", "name": "死链", "lang": "en", "tier": "A",
                                    "feed": "http://127.0.0.1:1/none.xml",
                                    "fulltext": True, "transcript": "feed"})
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            rc = quiet_main(["--config", str(cfg_path), "--out", str(out), "--quiet", "poll"])
            self.assertEqual(rc, 0)                   # 单源坏掉不拖垮整轮
            state = json.loads((out / "state.json").read_text(encoding="utf-8"))
            self.assertTrue(state["podcasts"]["dead"]["last_error"])
            self.assertEqual(len(list((out / "文章" / "demo").glob("*.md"))), 2)
            self.assertEqual(quiet_main(["--config", str(cfg_path), "--out", str(out),
                                         "--quiet", "check"]), 0)
            # 全部源都挂掉时退出码 2，便于 cron 报警
            cfg["podcasts"] = [p for p in cfg["podcasts"] if p["id"] == "dead"]
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(quiet_main(["--config", str(cfg_path), "--out", str(out),
                                         "--quiet", "poll"]), 2)

    def test_skip_pattern_marks_seen_without_archiving(self):
        d1, d2 = dates()
        tmp = Path(tempfile.mkdtemp())
        site_dir = tmp / "site"
        site_dir.mkdir()
        (site_dir / "feed.xml").write_text(RSS.format(date1=d1, date2=d2), encoding="utf-8")
        with _Server(site_dir) as feed_url:
            cfg_path, out = make_config(tmp, feed_url, skip=[r"第 2 期"])
            quiet_main(["--config", str(cfg_path), "--out", str(out), "--quiet", "poll"])
            arts = list((out / "文章" / "demo").glob("*.md"))
            self.assertEqual(len(arts), 1)
            self.assertIn("第-1-期", arts[0].name)

    def test_find_and_transcribe_without_backend(self):
        # find 需要联网，这里只验证「没有后端时 transcribe 给出明确提示且不崩」
        tmp = Path(tempfile.mkdtemp())
        cfg_path, out = make_config(tmp, "http://127.0.0.1:1/none.xml")
        queue = out / "队列" / "待转写.jsonl"
        queue.parent.mkdir(parents=True)
        queue.write_text(json.dumps({"pid": "demo", "title": "x", "audio": "http://x/a.mp3"},
                                    ensure_ascii=False) + "\n", encoding="utf-8")
        old = pw.stt_backend
        pw.stt_backend = lambda: None                 # 模拟没配 STT
        try:
            rc = quiet_main(["--config", str(cfg_path), "--out", str(out), "--quiet",
                             "transcribe"])
        finally:
            pw.stt_backend = old
        self.assertEqual(rc, 3)
        self.assertEqual(len(pw.read_queue(out)), 1, "失败时应保留队列")


class TestScore(unittest.TestCase):
    def test_tier_and_keywords_raise_score(self):
        item = {"title": "对话 OpenAI CEO：GPT 与 AGI", "description": "大模型 智能体",
                "duration": 3600}
        s = pw.score_item({"tier": "S"}, item, "feed_content")
        b = pw.score_item({"tier": "B"}, {"title": "旅行", "description": "", "duration": 0}, "none")
        self.assertGreater(s, b)
        self.assertGreaterEqual(s, 10)

    def test_stars_bounds(self):
        for score in (0, 1, 5, 20, 100):
            self.assertEqual(len(pw.stars(score)), 5)


# ------------------------------------------------------------------ STT（mock 服务）

class _STTServer:
    """假的 OpenAI 兼容转写服务：GET feed / GET mp3 / POST /v1/audio/transcriptions。"""

    RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
<channel><title>STT 测试</title>
<item>
  <title>只有音频的一期</title>
  <link>https://example.com/ep9</link>
  <guid>stt-ep-9</guid>
  <pubDate>{date}</pubDate>
  <description>音频节目提要</description>
  <enclosure url="{base}/ep.mp3" length="999" type="audio/mpeg"/>
  <itunes:duration>1800</itunes:duration>
</item>
</channel></rss>
"""

    def __init__(self, reject_verbose: bool = False):
        outer = self
        self.reject_verbose = reject_verbose

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body, ctype="application/octet-stream"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/feed.xml"):
                    rss = outer.RSS.format(date=dates()[1], base=outer.base_url())
                    self._send(200, rss.encode(), "application/rss+xml")
                elif self.path.startswith("/ep.mp3"):
                    self._send(200, b"ID3fake-mp3-bytes", "audio/mpeg")
                else:
                    self._send(404, b"no")

            def do_POST(self):
                if self.path.endswith("/audio/transcriptions"):
                    length = int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(length)
                    if b"model" not in body:
                        self._send(400, b'{"error":"no model"}', "application/json")
                        return
                    if outer.reject_verbose and b"verbose_json" in body:
                        self._send(400, b'{"error":"response_format not supported"}',
                                   "application/json")
                        return
                    if b"verbose_json" in body:
                        payload = {"text": "这是一段测试转写。",
                                   "segments": [{"start": 1.0, "end": 3.5,
                                                 "text": "这是一段测试转写。"},
                                                {"start": 3.5, "end": 6.0, "text": "第二段。"}]}
                    else:                       # json 降级：只有整段文本
                        payload = {"text": "这是一段测试转写。"}
                    self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                               "application/json")
                else:
                    self._send(404, b"no")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def base_url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return f"{self.base_url()}/feed.xml"

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestSTTFlow(unittest.TestCase):
    def _run_flow(self, reject_verbose: bool):
        tmp = Path(tempfile.mkdtemp())
        feed_srv, stt_srv = _STTServer(), _STTServer(reject_verbose=reject_verbose)
        feed_url = feed_srv.__enter__()
        stt_srv.__enter__()
        try:
            cfg_path, out = make_config(tmp, feed_url, fulltext=False, transcript="audio")
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["defaults"]["auto_transcribe"] = False
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            env = {"GZH_STT_BASE_URL": f"{stt_srv.base_url()}/v1",
                   "GZH_STT_API_KEY": "test-key", "GZH_STT_MODEL": "whisper-1"}
            old = {k: os.environ.get(k) for k in env}
            try:
                os.environ.update(env)
                base = ["--config", str(cfg_path), "--out", str(out), "--quiet"]
                self.assertEqual(quiet_main(base + ["poll"]), 0)
                self.assertEqual(quiet_main(base + ["transcribe"]), 0)
                art = next((out / "文章" / "demo").glob("*.md"))
                return art.read_text(encoding="utf-8"), out
            finally:
                for key, value in old.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
        finally:
            feed_srv.__exit__(None, None, None)
            stt_srv.__exit__(None, None, None)

    def test_verbose_json_rejected_falls_back_to_json(self):
        text, out = self._run_flow(reject_verbose=True)
        self.assertIn("## 全文", text)
        self.assertIn("这是一段测试转写", text)
        self.assertEqual(len(pw.read_queue(out)), 0)

    def test_queue_then_transcribe(self):
        tmp = Path(tempfile.mkdtemp())
        feed_srv, stt_srv = _STTServer(), _STTServer()
        feed_url = feed_srv.__enter__()
        stt_srv.__enter__()
        try:
            cfg_path, out = make_config(tmp, feed_url, fulltext=False, transcript="audio")
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["defaults"]["auto_transcribe"] = False     # 先进队列，不自动烧钱
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            env = {"GZH_STT_BASE_URL": f"{stt_srv.base_url()}/v1",
                   "GZH_STT_API_KEY": "test-key", "GZH_STT_MODEL": "whisper-1"}
            old = {k: os.environ.get(k) for k in env}
            try:
                os.environ.update(env)
                base = ["--config", str(cfg_path), "--out", str(out), "--quiet"]
                self.assertEqual(quiet_main(base + ["poll"]), 0)
                self.assertEqual(len(pw.read_queue(out)), 1, "未开自动转写时应入队")
                art = next((out / "文章" / "demo").glob("*.md"))
                self.assertIn("## 待转写", art.read_text(encoding="utf-8"))
                # 手动跑 transcribe：应下载 → 调 mock STT → 回填文章
                self.assertEqual(quiet_main(base + ["transcribe"]), 0)
                self.assertEqual(len(pw.read_queue(out)), 0)
                text = art.read_text(encoding="utf-8")
                self.assertIn("## 全文", text)
                self.assertIn("[0:01]", text)
                self.assertIn("这是一段测试转写", text)
                segs = list((out / "转写" / "demo").glob("*.transcript.json"))
                self.assertEqual(len(segs), 1)
                payload = json.loads(segs[0].read_text(encoding="utf-8"))
                self.assertEqual(payload["count"], 2)
            finally:
                for key, value in old.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
        finally:
            feed_srv.__exit__(None, None, None)
            stt_srv.__exit__(None, None, None)


class TestSectionsAndInbox(unittest.TestCase):
    def test_markdown_sections_to_segments(self):
        md = ("导语\n\n## [0:02:11] 第一节\n正文 A\n\n"
              "## [0:15:00] 第二节\n正文 B\n\n## [0:30:00] 第三节\n正文 C\n")
        segs = pw.markdown_sections_to_segments(md)
        self.assertEqual(len(segs), 3)
        self.assertEqual(segs[0]["start"], 131.0)
        self.assertEqual(segs[1]["start"], 900.0)
        self.assertIn("正文 A", segs[0]["text"])
        self.assertGreaterEqual(segs[-1]["end"], segs[-1]["start"])

    def test_sections_need_at_least_three(self):
        self.assertEqual(pw.markdown_sections_to_segments("## [0:01] x\n正文"), [])

    def test_inbox_command_is_idempotent(self):
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "out"
        store = pw.Store(out / "state.json")
        art_dir = out / "文章" / "demo"
        art_dir.mkdir(parents=True)
        (art_dir / "2026-11-10_x.md").write_text("# x", encoding="utf-8")
        seg_dir = out / "转写" / "demo"
        seg_dir.mkdir(parents=True)
        (seg_dir / "2026-11-10_x.transcript.json").write_text('{"segments": []}',
                                                              encoding="utf-8")
        store.mark("demo", "k", {"t": "测试一期", "d": pw.local_date(), "first": pw.iso(),
                                 "tier": "S", "tr": "feed_content", "chars": 100,
                                 "art": "文章/demo/2026-11-10_x.md",
                                 "link": "https://example.com/ep", "dur": "1:00:00"})
        store.save()
        cfg_path = tmp / "cfg.json"
        cfg_path.write_text(json.dumps({"podcasts": [{"id": "demo", "name": "测试",
                                                      "tier": "S"}], "defaults": {}}),
                            encoding="utf-8")
        inbox = tmp / "inbox"
        argv = ["--config", str(cfg_path), "--out", str(out), "--quiet", "inbox",
                "--dir", str(inbox), "--tiers", "S"]
        self.assertEqual(pw.main(argv), 0)
        self.assertEqual(len(list(inbox.glob("*.md"))), 1)
        self.assertEqual(len(list(inbox.glob("*.json"))), 1, "转写附件应同名复制")
        self.assertIn("https://example.com/ep", (inbox / "播客_测试一期.md").read_text(encoding="utf-8"))
        self.assertEqual(pw.main(argv), 0)                        # 重复投递不重复写
        self.assertEqual(len(list(inbox.glob("*.md"))), 1)


class TestFixesFromReview(unittest.TestCase):
    """针对独立验收报告里问题的回归测试。"""

    RSS_DUP = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>双发测试</title>
<item><title>同一集的双平台镜像</title><link>https://a.example/ep1</link>
<guid>mirror-a</guid><pubDate>{date}</pubDate><description>提要</description>
<content:encoded><![CDATA[<p>正文A，足够长。</p>]]></content:encoded></item>
<item><title>同一集的双平台镜像</title><link>https://b.example/ep1</link>
<guid>mirror-b</guid><pubDate>{date}</pubDate><description>提要</description>
<content:encoded><![CDATA[<p>正文B，足够长。</p>]]></content:encoded></item>
</channel></rss>
"""

    RSS_OLD = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>旧单集</title>
<item><title>一个月前的一期</title><link>https://old.example/ep0</link>
<guid>old-0</guid><pubDate>{date}</pubDate><description>旧提要</description>
<content:encoded><![CDATA[<p>旧正文。</p>]]></content:encoded></item>
</channel></rss>
"""

    def test_batch_dedupe_same_title_same_day(self):
        tmp = Path(tempfile.mkdtemp())
        site_dir = tmp / "site"
        site_dir.mkdir()
        (site_dir / "feed.xml").write_text(
            self.RSS_DUP.format(date=dates()[1]), encoding="utf-8")
        with _Server(site_dir) as feed_url:
            cfg_path, out = make_config(tmp, feed_url)
            self.assertEqual(quiet_main(["--config", str(cfg_path), "--out", str(out),
                                         "--quiet", "poll"]), 0)
            arts = list((out / "文章" / "demo").glob("*.md"))
            self.assertEqual(len(arts), 1, "同批双发只应入档 1 篇")
            state = json.loads((out / "state.json").read_text(encoding="utf-8"))
            marks = list(state["podcasts"]["demo"]["seen"].values())
            self.assertTrue(any(m.get("dup") for m in marks), "重复项应标 dup")

    def test_history_backfills_baseline(self):
        tmp = Path(tempfile.mkdtemp())
        site_dir = tmp / "site"
        site_dir.mkdir()
        old = (datetime.now(timezone.utc) - timedelta(days=40)).strftime(
            "%a, %d %b %Y %H:%M:%S +0000")
        (site_dir / "feed.xml").write_text(self.RSS_OLD.format(date=old), encoding="utf-8")
        with _Server(site_dir) as feed_url:
            cfg_path, out = make_config(tmp, feed_url)
            base = ["--config", str(cfg_path), "--out", str(out), "--quiet"]
            self.assertEqual(quiet_main(base + ["poll"]), 0)
            self.assertEqual(len(list((out / "文章" / "demo").glob("*.md"))), 0,
                             "7 天窗口外先只登记为基线")
            self.assertEqual(quiet_main(base + ["poll", "--history"]), 0)
            self.assertEqual(len(list((out / "文章" / "demo").glob("*.md"))), 1,
                             "--history 应能回灌基线条目")

    def test_file_scheme_rejected(self):
        with self.assertRaises(pw.FetchError):
            pw.http_get("file:///etc/passwd")

    def test_bad_state_structure_backed_up(self):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "state.json"
        path.write_text('{"podcasts": []}', encoding="utf-8")
        store = pw.Store(path)                     # 不抛异常
        self.assertEqual(store.data["version"], 1)
        self.assertTrue(list(tmp.glob("state.broken-*.json")))

    def test_bad_config_structure_rejected(self):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "cfg.json"
        path.write_text('{"podcasts": {"a": 1}}', encoding="utf-8")
        with self.assertRaises(SystemExit):
            pw.load_config(path)
        path.write_text('{"podcasts": [{"name": "缺 id"}]}', encoding="utf-8")
        with self.assertRaises(SystemExit):
            pw.load_config(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
