#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nightly · 夜间自动出稿（抓取 → 选片 → 转写 → 出 v0 底稿）

设计见 `研究/ECS夜间自动出稿_设计_2026-11-19.md`。本文件只做「桥接」这一段：
把 podwatch 的待转写队列、我们的 STT、orchestrator.oneshot 串起来，
并把这一次的结果写成 `播客监控/nightly/last_run.json` 供飞书机器人读取。

用法（在 gongzhonghao/ 下）：
    python3 -m nightly run --dry-run          # 只打分选片，不转写不出稿
    python3 -m nightly run --limit 1          # 真跑：转 1 期 + 出 v0 底稿
    python3 -m nightly run --skip-poll        # 不抓 RSS，直接用现有队列
    python3 -m nightly status                 # 看上一次跑成什么样

环境变量：
    GZH_STT_CMD   自有转写器命令（podwatch 的 gzh 后端，例：/path/.tools/stt/stt-remote）
    GZH_LLM_API_KEY / GZH_LLM_BASE_URL / GZH_LLM_MODEL   DeepSeek 等 OpenAI 兼容接口
    GZH_WORKSPACE 工作区根（默认自动推断）

退出码：0=正常（含「没有新节目」），2=配置错误，3=转写或出稿失败。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE = Path(os.environ.get("GZH_WORKSPACE") or HERE.parent)
POD_OUT = WORKSPACE / "播客监控"
QUEUE = POD_OUT / "队列" / "待转写.jsonl"
STATE = POD_OUT / "state.json"
OUT_DIR = POD_OUT / "nightly"
LAST_RUN = OUT_DIR / "last_run.json"
CHOICE = Path(os.environ.get("GZH_STATE_DIR") or (WORKSPACE / ".state")) / "choice.json"

sys.path.insert(0, str(HERE / "podwatch"))
import podwatch as pw  # noqa: E402

# 选题关键词：命中越多分越高（可在 nightly.json 里覆盖）
DEFAULT_KEYWORDS = [
    "AI", "Agent", "大模型", "模型", "OpenAI", "Anthropic", "DeepSeek", "Google",
    "创业", "产品", "增长", "商业化", "算力", "芯片", "英伟达", "推理", "对齐",
    "编程", "开发者", "开源", "工具", "效率", "组织", "招聘", "融资", "IPO",
]

DEFAULT_CFG = {
    "window_days": 7,          # 只看最近 7 天新入库的
    "candidates": 3,           # 推给用户几个候选
    "transcribe_per_night": 1, # 每晚自动转几期（其余候选等用户点选再转）
    "keywords": DEFAULT_KEYWORDS,
    "ban_words": ["replay", "重播", "best of"],
}


# ---------------------------------------------------------------- 基础读写


def log(msg: str) -> None:
    print(msg, flush=True)


def load_cfg() -> dict:
    path = OUT_DIR / "nightly.json"
    cfg = dict(DEFAULT_CFG)
    if path.exists():
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
        except ValueError:
            log(f"[warn] {path} 不是合法 JSON，用默认配置")
    return cfg


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def episode_of(entry: dict) -> dict:
    """把队列条目整理成打分用的统一结构。"""
    art = entry.get("art") or ""
    stem = Path(art).stem if art else ""
    d = (entry.get("date") or "")[:10]
    return {
        "pid": entry.get("pid", ""),
        "key": entry.get("key", ""),
        "title": entry.get("title", ""),
        "link": entry.get("link", ""),
        "audio": entry.get("audio", ""),
        "date": d,
        "art": art,
        "stem": stem,
    }


# ---------------------------------------------------------------- 抓取与挑选


def poll() -> int:
    """跑一轮 podwatch poll，刷新队列。失败不致命，用现有队列继续。"""
    script = HERE / "podwatch" / "podwatch.py"
    try:
        proc = subprocess.run([sys.executable, str(script), "poll",
                               "--timeout", "10", "--workers", "12"],
                              capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"[warn] poll 失败：{exc}，继续用现有队列")
        return 1
    tail = (proc.stdout or "").strip().splitlines()[-3:]
    for line in tail:
        log(f"  {line}")
    if proc.returncode != 0:
        log(f"[warn] poll 退出码 {proc.returncode}：{(proc.stderr or '')[-200:]}")
    return proc.returncode


def score_of(ep: dict, cfg: dict, state: dict) -> tuple[float, list[str]]:
    """启发式基础分：越新越高、命中关键词加分、S/A 级加分、超长扣分。"""
    reasons: list[str] = []
    score = 0.0

    days = 99
    if ep["date"]:
        try:
            days = (date.today() - datetime.strptime(ep["date"], "%Y-%m-%d").date()).days
        except ValueError:
            days = 99
    score += max(0.0, 10.0 - days) * 0.8
    reasons.append(f"{days} 天前" if days < 90 else "日期未知")

    title = ep["title"]
    hits = [kw for kw in cfg["keywords"] if kw and kw.lower() in title.lower()]
    if hits:
        score += 3.0 * min(len(hits), 4)
        reasons.append("命中 " + "/".join(hits[:4]))

    tier = str(((state.get(ep["pid"], {}) or {}).get("seen", {})
                or {}).get(ep["key"], {}).get("tier", "B")).upper()
    if tier in ("S", "A"):
        score += {"S": 6.0, "A": 3.0}[tier]
        reasons.append(f"{tier} 级源")

    low = title.lower()
    if any(w.lower() in low for w in cfg["ban_words"]):
        score -= 20.0
        reasons.append("重播/合辑：降权")

    return round(score, 2), reasons


def pick(entries: list[dict], cfg: dict) -> list[dict]:
    """按窗口过滤后打分排序，返回前 N 个候选。"""
    start = date.today() - timedelta(days=int(cfg["window_days"]) - 1)
    state = (read_json(STATE, {}) or {}).get("podcasts", {})
    eps = []
    for entry in entries:
        ep = episode_of(entry)
        if transcript_path(ep).exists():      # 转写过就不再重复转
            continue
        if ep["date"]:
            try:
                if datetime.strptime(ep["date"], "%Y-%m-%d").date() < start:
                    continue
            except ValueError:
                pass
        score, reasons = score_of(ep, cfg, state)
        eps.append({**ep, "score": score, "reasons": reasons})
    eps.sort(key=lambda x: (-x["score"], x["date"]))
    return eps[: int(cfg["candidates"])]


# ---------------------------------------------------------------- 转写


def transcript_path(ep: dict) -> Path:
    """podwatch 转写产物：播客监控/转写/<pid>/<文章stem>.transcript.json"""
    return POD_OUT / "转写" / ep["pid"] / f"{ep['stem']}.transcript.json"


def transcribe(ep: dict) -> Path:
    """下载音频 → 自有 STT → 回填文章与 segments 文件（已有产物则复用）。"""
    cached = transcript_path(ep)
    if cached.exists() and (read_json(cached, {}) or {}).get("segments"):
        log(f"   复用已有转写：{cached.name}")
        return cached
    if not os.environ.get("GZH_STT_CMD"):
        raise RuntimeError("缺少 GZH_STT_CMD：podwatch 不知道该调哪个转写器")
    work = POD_OUT / "转写" / ep["pid"] / f".work_{int(time.time())}"
    cfg = read_json(HERE / "podwatch" / "podcasts.json", {}) or {}
    podcast = next((p for p in cfg.get("podcasts", []) if p.get("id") == ep["pid"]), {})
    langs = str(podcast.get("lang", "") or "")
    got = pw.transcribe_audio(ep["audio"], work, cfg=cfg.get("defaults", {}) or {},
                              language=langs)
    if not got.get("text"):
        raise RuntimeError("转写结果为空")
    entry = {"pid": ep["pid"], "key": ep["key"], "art": ep["art"],
             "title": ep["title"]}
    pw.update_article_transcript(POD_OUT, entry, podcast, got)
    path = transcript_path(ep)
    if not path.exists():
        raise RuntimeError(f"转写成功但未找到 segments 文件：{path}")
    return path


# ---------------------------------------------------------------- 出稿


def draft(ep: dict, transcript: Path, *, provider: str, max_ticks: int,
          dry_run: bool) -> dict:
    """调 orchestrator.oneshot 跑到 awaiting_publish（纯音频：不给 --video）。"""
    title = re.sub(r"^[#\s]+", "", ep["title"]).strip()
    if dry_run:
        return {"ok": True, "dry_run": True, "title": title}
    cmd = [sys.executable, "-m", "orchestrator.oneshot",
           "--transcript", str(transcript),
           "--title", title,
           "--provider", provider,
           "--max-ticks", str(max_ticks)]
    # 不传 --no-render：ECS 上装了 Chrome + Noto CJK，渲染验收要真跑，
    # 否则 gatekeeper 判 INCOMPLETE（qc_report 永远是"未完成"）。
    env = dict(os.environ)
    proc = subprocess.run(cmd, cwd=str(HERE), env=env,
                          capture_output=True, text=True, timeout=5400)
    tail = (proc.stdout or "").strip().splitlines()[-12:]
    for line in tail:
        log(f"  {line}")
    return {"ok": proc.returncode == 0, "exit": proc.returncode,
            "reason": (proc.stderr or "").strip()[-400:], "tail": tail}


# ---------------------------------------------------------------- 主流程


def cmd_run(args) -> int:
    cfg = load_cfg()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    started = time.time()
    record = {"started": datetime.now().isoformat(timespec="seconds"),
              "dry_run": bool(args.dry_run), "candidates": [], "picked": "",
              "transcript": "", "draft": {}, "errors": []}

    if not args.skip_poll:
        log("① 抓取新节目")
        poll()
    else:
        log("① 跳过抓取（--skip-poll）")

    entries = pw.read_queue(POD_OUT)
    log(f"② 队列 {len(entries)} 期，按最近 {cfg['window_days']} 天打分")
    if not entries:
        record["note"] = "队列为空：最近没有新的纯音频节目"
        write_json(LAST_RUN, record)
        log("   队列为空，今晚不转写")
        return 0

    picks = pick(entries, cfg)
    if not picks:
        record["note"] = "窗口内没有候选（可能都已转写/出稿过）"
        write_json(LAST_RUN, record)
        log("   最近 7 天没有候选")
        return 0
    record["candidates"] = [{k: p[k] for k in
                             ("pid", "key", "title", "date", "score", "reasons", "link")}
                            for p in picks]
    for i, p in enumerate(picks, 1):
        log(f"   {i}. [{p['score']:5.1f}] {p['title'][:50]}（{p['date']}）"
            f" — {'；'.join(p['reasons'])}")

    # 你在飞书卡片上点的「选 N」优先于自动打分。
    # 判新规则（2026-11-21 修）：24 小时内点的都认。早先版用「比上次运行新」，
    # 一旦夜里手动验过一轮就会刷新 last_run，把你当晚点的选择误判成过期——
    # 19:04 点的「选 2」差点被 19:15 的测试运行吞掉。同一期已跑过则不重复。
    if not args.pick:
        want = read_json(CHOICE, None)
        last = read_json(LAST_RUN, {}) or {}
        if want and want.get("key"):
            try:
                age_h = (time.time()
                         - datetime.fromisoformat(str(want.get("ts"))).timestamp()) / 3600.0
            except (TypeError, ValueError):
                age_h = 1e9
            if age_h <= 24 and want["key"] != last.get("pick_key"):
                args.pick = want["key"]
                log(f"   采用飞书选择：{want.get('title', '')[:40]}"
                    f"（{want.get('ts')}，{age_h:.1f}h 前点的）")

    if args.pick:
        forced = next((p for p in picks if p["key"] == args.pick), None)
        if forced is None:
            for entry in entries:            # 不在候选窗口里也允许显式指定
                if entry.get("key") == args.pick:
                    ep = episode_of(entry)
                    score, reasons = score_of(ep, cfg, {})
                    forced = {**ep, "score": score, "reasons": reasons}
                    break
        if forced is None:
            log(f"   ✗ --pick {args.pick} 没在队列里找到")
            return 2
        picks = [forced, *[p for p in picks if p["key"] != args.pick]]
    chosen = picks[0]
    record["picked"] = chosen["title"]
    record["pick_key"] = chosen["key"]
    log(f"③ 转写第 1 候选：{chosen['title'][:50]}")
    if args.dry_run:
        log("   [dry-run] 不转写")
        record["draft"] = {"ok": True, "dry_run": True}
        record["elapsed"] = round(time.time() - started, 1)
        write_json(LAST_RUN, record)
        return 0

    tp = None
    for attempt in range(1, 4):              # 失败重试 2 次（Q12），CDN 抖动很常见
        try:
            tp = transcribe(chosen)
            break
        except Exception as exc:  # noqa: BLE001 - 单期失败要能继续给人看
            msg = f"{type(exc).__name__}: {exc}"
            record["errors"].append(f"转写尝试 {attempt} 失败：{msg}")
            log(f"   ✗ 转写尝试 {attempt} 失败：{msg}")
            if attempt < 3:
                time.sleep(20)
    if tp is None:
        record["elapsed"] = round(time.time() - started, 1)
        write_json(LAST_RUN, record)
        return 3
    record["transcript"] = str(tp)
    segs = read_json(tp, {}).get("segments") or []
    log(f"   转写完成：{len(segs)} 段 → {tp.name}")

    log(f"④ 出 v0 底稿（provider={args.provider}）")
    result = draft(chosen, tp, provider=args.provider,
                   max_ticks=args.max_ticks, dry_run=False)
    record["draft"] = result
    if not result.get("ok"):
        record["errors"].append(f"出稿失败：{result.get('reason', '')[:200]}")
        log(f"   ✗ 出稿失败：exit={result.get('exit')}")
    else:
        log("   ✓ 已到 awaiting_publish")

    record["elapsed"] = round(time.time() - started, 1)
    write_json(LAST_RUN, record)
    log(f"⑤ 记录写入 {LAST_RUN}（{record['elapsed']}s）")
    return 0 if result.get("ok") else 3


def cmd_review(args) -> int:
    """周复盘：列出这一周出过什么、卡在哪；清掉上一周之前的导入片段 html。"""
    base = Path(os.environ.get("GZH_BASE") or WORKSPACE)
    cutoff = datetime.now() - timedelta(days=int(args.days))
    rows = []
    for case in sorted(base.glob("*_案例")):
        if not case.is_dir():
            continue
        htmls = sorted(case.glob("*_导入片段.html"), key=lambda p: p.stat().st_mtime)
        if not htmls:
            continue
        newest = htmls[-1]
        mtime = datetime.fromtimestamp(newest.stat().st_mtime)
        if mtime < cutoff:
            continue
        qc = next(iter(case.glob("*_质检报告.md")), None)
        verdict = ""
        if qc:
            text = qc.read_text(encoding="utf-8", errors="replace")
            for token in ("PASS", "FAIL", "INCOMPLETE"):
                if token in text:
                    verdict = token
                    break
        rows.append({"case": case.name, "html": newest.name,
                     "at": mtime.strftime("%m-%d %H:%M"), "qc": verdict})
    rows.sort(key=lambda r: r["at"], reverse=True)
    md = [f"# 周复盘 {date.today().isoformat()}", "",
          f"窗口：最近 {args.days} 天 · 产出 {len(rows)} 期", ""]
    for r in rows:
        md.append(f"- **{r['case']}** · {r['at']} · 质检 {r['qc'] or '无报告'}")
    path = OUT_DIR / f"review_{date.today().isoformat()}.md"
    path.write_text("\n".join(md) + "\n", encoding="utf-8")
    log(f"复盘写入 {path}（{len(rows)} 期）")

    if args.clean:
        deleted = 0
        keep_after = datetime.now() - timedelta(days=int(args.keep_days))
        for case in base.glob("*_案例"):
            for html in case.glob("*_导入片段.html"):
                if datetime.fromtimestamp(html.stat().st_mtime) < keep_after:
                    html.unlink(missing_ok=True)
                    deleted += 1
        log(f"清理旧导入 html：{deleted} 个（保留 {args.keep_days} 天）")
    return 0


def cmd_status(args) -> int:
    data = read_json(LAST_RUN, None)
    if not data:
        log("还没有跑过：python3 -m nightly run --dry-run")
        return 0
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m nightly",
                                 description="夜间自动出稿：抓取 → 选片 → 转写 → v0 底稿")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="跑一次夜间流程")
    r.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="只打分选片，不转写不出稿")
    r.add_argument("--pick", default="", help="指定某期（队列里的 key），跳过打分排第一")
    r.add_argument("--skip-poll", action="store_true", dest="skip_poll",
                   help="不抓 RSS，直接用现有队列")
    r.add_argument("--provider", default=os.environ.get("GZH_LLM_PROVIDER") or "stub",
                   help="LLM provider（深写用 openai-compat；联调可 stub）")
    r.add_argument("--max-ticks", type=int, default=40, dest="max_ticks")
    s = sub.add_parser("status", help="看上一次运行结果")
    v = sub.add_parser("review", help="周复盘：列一周产出，可选清旧导入 html")
    v.add_argument("--days", type=int, default=7)
    v.add_argument("--clean", action="store_true", help="同时删除超过保留期的 *_导入片段.html")
    v.add_argument("--keep-days", type=int, default=14, dest="keep_days",
                   help="导入 html 保留天数（默认 14：留一周缓冲再删）")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "review":
        return cmd_review(args)
    return cmd_status(args)


if __name__ == "__main__":
    sys.exit(main())
