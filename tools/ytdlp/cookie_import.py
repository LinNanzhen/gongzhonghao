#!/usr/bin/env python3
"""把浏览器里拷出来的 B站 Cookie 转成 yt-dlp 能吃的 Netscape cookies.txt。

Chrome 157 起 cookie 是 v20 应用绑定加密，第三方工具解不开（yt-dlp 会报
"cannot decrypt v10 cookies: no key found"），所以这里走「你从浏览器拷出来 → 我转换」：

  # 1) 从 DevTools 复制整行 Cookie（推荐，含 HttpOnly 的 SESSDATA）
  python3 cookie_import.py --paste "SESSDATA=xxx; bili_jct=yyy; DedeUserID=123; ..." --check

  # 2) 从「Get cookies.txt LOCALLY」等扩展导出的文件
  python3 cookie_import.py --file ~/Downloads/bilibili_cookies.txt --check

  # 3) 从扩展导出的 JSON（[{name,value,domain,...}, ...]）
  python3 cookie_import.py --json ~/Downloads/cookies.json --check

--check 会调 api.bilibili.com/x/web-interface/nav 验证登录态并打印昵称/UID。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_OUT = Path(__file__).with_name("bili-cookies.txt")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
IMPORTANT = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "buvid3", "b_nut")


def parse_cookie_header(raw: str) -> list[tuple[str, str]]:
    """解析 'a=1; b=2' 或 'Cookie: a=1; b=2' 或 DevTools 里复制出来的多行。"""
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1]
    pairs = []
    for line in raw.splitlines():
        for chunk in line.strip().split(";"):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            k, v = chunk.split("=", 1)
            k, v = k.strip(), v.strip()
            if k:
                pairs.append((k, v))
    return pairs


def parse_netscape(path: Path) -> list[tuple[str, str]]:
    pairs = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            pairs.append((parts[5], parts[6]))
    return pairs


def parse_json(path: Path) -> list[tuple[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("cookies", [])
    pairs = []
    for c in data:
        if isinstance(c, dict) and c.get("name"):
            pairs.append((c["name"], str(c.get("value", ""))))
    return pairs


def to_netscape(pairs: list[tuple[str, str]], domain: str, expiry: int) -> str:
    seen, lines = set(), ["# Netscape HTTP Cookie File", "# 由 .tools/ytdlp/cookie_import.py 生成", ""]
    for name, value in pairs:
        if name in seen or not value:
            continue
        seen.add(name)
        lines.append(f"{domain}\tTRUE\t/\tFALSE\t{expiry}\t{name}\t{value}")
    return "\n".join(lines) + "\n"


def check(pairs: list[tuple[str, str]]) -> int:
    cookie = "; ".join(f"{k}={v}" for k, v in pairs)
    try:
        out = subprocess.run(
            ["curl", "-k", "-sS", "-m", "15", "-A", UA,
             "-H", f"Cookie: {cookie}",
             "https://api.bilibili.com/x/web-interface/nav"],
            capture_output=True, text=True, timeout=25,
        ).stdout
        data = json.loads(out)
    except Exception as exc:  # noqa: BLE001
        print(f"[check] 请求失败：{exc}", file=sys.stderr)
        return 2
    block = data.get("data") or {}
    if block.get("isLogin"):
        print(f"[check] ✓ 登录态有效：{block.get('uname')}（UID {block.get('mid')}，"
              f"等级 {block.get('level')}，大会员 {bool(block.get('vipStatus'))}）")
        return 0
    msg = data.get("message") or "isLogin=false"
    print(f"[check] ✗ 登录态无效：{msg}（code={data.get('code')}）——Cookie 可能已过期或漏了 SESSDATA",
          file=sys.stderr)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="B站 Cookie → Netscape cookies.txt")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--paste", help="直接粘 Cookie 字符串")
    src.add_argument("--file", type=Path, help="Cookie 文本文件（Cookie 头或 Netscape）")
    src.add_argument("--json", type=Path, help="扩展导出的 JSON")
    ap.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--domain", default=".bilibili.com")
    ap.add_argument("--days", type=int, default=180, help="写进 cookies.txt 的过期天数（默认 180）")
    ap.add_argument("--check", action="store_true", help="调 nav 接口验证登录态")
    args = ap.parse_args()

    if args.paste:
        pairs = parse_cookie_header(args.paste)
    elif args.json:
        pairs = parse_json(args.json)
    else:
        text = args.file.read_text(encoding="utf-8", errors="replace")
        pairs = parse_netscape(args.file) if "\t" in text else parse_cookie_header(text)

    if not pairs:
        print("没有解析出任何 cookie", file=sys.stderr)
        return 2

    got = [k for k, _ in pairs]
    missing = [k for k in ("SESSDATA", "bili_jct", "DedeUserID") if k not in got]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(to_netscape(pairs, args.domain, int(time.time()) + args.days * 86400), encoding="utf-8")
    args.out.chmod(0o600)
    print(f"[ok] {len([k for k in got if k in IMPORTANT]) or len(got)} 个关键 cookie → {args.out}")
    if missing:
        print(f"[warn] 缺 {', '.join(missing)}（未登录的 Cookie 也会 412）", file=sys.stderr)
    if args.check:
        return check(pairs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
