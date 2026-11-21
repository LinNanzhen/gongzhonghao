#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""choice_server · 选择回传小服务（webhook 通道的「按钮」落地）

为什么需要它：群自定义机器人（webhook）能推交互卡片，但**收不到按钮回调**——
回调属于应用能力，而应用在本租户被 21001 挡住了。折中办法：卡片按钮不放
`value`，放 `url`；用户点开 → 浏览器访问本服务 → 本服务把选择写进
`state/choice.json`，下一轮 02:00 的 nightly 读它出稿。

用法（在 gongzhonghao/ 下）：
    python3 -m choice_server                 # 默认 0.0.0.0:8788
    GZH_CHOICE_PORT=9000 python3 -m choice_server

环境变量：
    GZH_CHOICE_TOKEN      必填，卡片链接里带的一次性口令（防别人乱点）
    GZH_CHOICE_PORT       默认 8788
    GZH_STATE_DIR         写 choice.json 的位置，默认 <workspace>/.state
    GZH_CHOICE_BASE       由推送端使用，指向本服务，如 http://1.2.3.4:8788

安全说明：只做两件事——校验口令、写一个 json。不读文件系统其他内容，
不接受 POST，参数只用白名单里的 t/i。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import feishu_bot as fb  # noqa: E402  （复用 load_env / LAST_RUN / record_choice）


PORT = int(os.environ.get("GZH_CHOICE_PORT") or 8788)
TOKEN = (os.environ.get("GZH_CHOICE_TOKEN") or "").strip()
HOST = (os.environ.get("GZH_CHOICE_HOST") or "0.0.0.0").strip()

PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
background:#f7f8fa;margin:0;padding:32px 20px;color:#1f2329}}
.card{{max-width:520px;margin:0 auto;background:#fff;border-radius:14px;padding:28px 24px;
box-shadow:0 2px 12px rgba(31,35,41,.08)}}
h1{{font-size:19px;margin:0 0 14px}}
.sel{{font-size:17px;font-weight:600;color:#1f2329;line-height:1.5;margin:12px 0 18px}}
.meta{{font-size:13px;color:#646a73;line-height:1.7}}
.ok{{color:#2ea121;font-size:15px;font-weight:600;margin-bottom:10px}}
.bad{{color:#d83931;font-size:15px;font-weight:600;margin-bottom:10px}}
</style></head><body><div class="card">
<div class="{cls}">{head}</div>
<div class="sel">{body}</div>
<div class="meta">{meta}</div>
</div></body></html>"""


def _page(cls: str, head: str, body: str, meta: str = "") -> bytes:
    return PAGE.format(title=head, cls=cls, head=head, body=body, meta=meta).encode("utf-8")


def _load_candidates() -> list[dict]:
    try:
        record = json.loads(fb.LAST_RUN.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return record.get("candidates") or []


class Handler(BaseHTTPRequestHandler):
    server_version = "gzh-choice/1.0"

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/health":
            self._send(200, b'{"ok":true}', "application/json")
            return
        if parsed.path != "/c":
            self._send(404, _page("bad", "页面不存在", "这个链接不对，回群里重新点一次按钮即可。"))
            return
        got_token = (query.get("t") or [""])[0]
        if not TOKEN or got_token != TOKEN:
            self._send(403, _page("bad", "链接无效", "口令不对或已过期，回群里重新点一次按钮。"))
            return
        try:
            idx = int((query.get("i") or ["0"])[0])
        except ValueError:
            idx = 0
        cands = _load_candidates()
        if not (1 <= idx <= len(cands)):
            self._send(400, _page("bad", "选不了", "候选列表里没有这一项，可能是新一期还没生成，回群里看看最新卡片。"))
            return
        cand = cands[idx - 1]
        payload = fb.record_choice({
            "key": cand.get("key", ""),
            "idx": idx,
            "title": cand.get("title", ""),
        })
        self._send(200, _page(
            "ok", "✅ 已记下你的选择",
            f"第 {idx} 篇 · {cand.get('title', '')}",
            f"今晚 02:00 用这篇出 v0 底稿，08:30 推到群里。<br>记录时间 {payload.get('ts', '')}",
        ))
        print(f"[{datetime.now().isoformat(timespec='seconds')}] choose idx={idx} key={cand.get('key','')}", flush=True)

    def log_message(self, fmt: str, *args) -> None:  # 少噪音
        sys.stderr.write("[choice] " + fmt % args + "\n")


def main() -> int:
    fb.load_env()
    global TOKEN
    TOKEN = (os.environ.get("GZH_CHOICE_TOKEN") or "").strip()
    if not TOKEN:
        print("缺少 GZH_CHOICE_TOKEN：先写进 /etc/gzh/env（否则任何人都能改你的选题）")
        return 2
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"choice_server listening on http://{HOST}:{PORT}  (token {'set' if TOKEN else 'missing'})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
