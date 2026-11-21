#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""闸门通知（T4b）：把「等人处理」的 run 推到飞书群机器人。

为什么要有它：两道人工闸门（选题 / 发布）再严，人不知道它到了，
"无人值守"就退化成"人肉巡检"（最坏等 2 小时 cron 才发现）。

设计约束（tick 绝不能因为通知失败而失败）：
- 未配置 webhook（`FEISHU_WEBHOOK` / `GZH_FEISHU_WEBHOOK`）→ 静默返回 False，不报错；
- 网络 / HTTP / 飞书返回 code≠0 → 只 `print("[warn] ...")`，不抛、不阻塞 tick；
- 12 小时去重：`runs/.notified.json` 记 `{run_id}|{stage}|{gate}|{status}` 签名与时间戳，
  同一签名 12 小时内不重复推，防 2 小时 cron 刷屏；
- `dry_run` 只打印计划，不推送也不写去重文件。

    >>> from orchestrator.notify import notify_gates
    >>> notify_gates(store, runs)      # tick 末尾调用一次
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 仅类型提示，运行时不 import，避免环依赖
    from .state import Run, RunStore

WEBHOOK_ENVS = ("FEISHU_WEBHOOK", "GZH_FEISHU_WEBHOOK")
TIMEOUT = 5.0                 # 秒；飞书 webhook 正常 <1s，5s 已足够
DEDUP_SECONDS = 12 * 3600     # 同签名 12 小时内只推一次（cron 是 2 小时一次）
STATE_NAME = ".notified.json"

# 闸门名的中文标签（给人看的消息里别写 topic/publish）
GATE_LABELS = {"topic": "选题", "publish": "发布"}


# ---------------------------------------------------------------- webhook


def webhook_url() -> str:
    """读第一个配置了的 webhook；都没有返回空串。"""
    for env in WEBHOOK_ENVS:
        url = (os.environ.get(env) or "").strip()
        if url:
            return url
    return ""


def _post(url: str, text: str) -> None:
    """POST 一条文本消息到飞书 webhook；失败抛异常，由调用方降级成 warn。

    飞书机器人约定的 body：{"msg_type": "text", "content": {"text": ...}}。
    注意飞书部分错误（如关键词不匹配）仍返回 HTTP 200 + `code != 0`，也要判失败。
    """
    payload = {"msg_type": "text", "content": {"text": text}}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        status = getattr(resp, "status", 200)
        raw = resp.read()
    if isinstance(status, int) and not 200 <= status < 300:
        raise RuntimeError(f"HTTP {status}")
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        data = None
    if isinstance(data, dict):
        code = data.get("code", data.get("StatusCode"))
        if code not in (None, 0):
            msg = data.get("msg") or data.get("StatusMessage") or ""
            raise RuntimeError(f"飞书返回 code={code} {msg}".strip())


# ---------------------------------------------------------------- 收集


def _label(run: "Run") -> str:
    """案例名：优先 source.title，退化到 case_dir 名。"""
    title = (run.source.title or "").strip()
    if title:
        return title
    case = (run.case_dir or "").strip()
    return Path(case).name if case else "?"


def _collect(runs: list["Run"]) -> list[dict[str, Any]]:
    """把等待人工闸门 / blocked 的 run 收集成待通知项。"""
    items: list[dict[str, Any]] = []
    for run in runs:
        gate = run.waiting_gate
        if run.status == "blocked":
            items.append({
                "run": run,
                "status": "blocked",
                "gate": "-",
                "signature": _signature(run, "blocked", "-"),
                "line": (f"· [blocked] {run.run_id}  {_label(run)}  "
                         f"stage={run.stage}  reason={(run.error or '')[:80]}"),
            })
        elif gate and run.status == "active":
            note = GATE_LABELS.get(gate, gate)
            items.append({
                "run": run,
                "status": "waiting",
                "gate": gate,
                "signature": _signature(run, "waiting", gate),
                "line": (f"· [待确认·{note}] {run.run_id}  {_label(run)}  "
                         f"stage={run.stage}  gate={gate}"),
            })
    return items


def _signature(run: "Run", status: str, gate: str) -> str:
    return f"{run.run_id}|{run.stage}|{gate}|{status}"


def _compose(items: list[dict[str, Any]]) -> str:
    waiting = sum(1 for it in items if it["status"] == "waiting")
    blocked = len(items) - waiting
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    head = [f"【公众号编排器】需要人工处理 {len(items)} 项"
            f"（待确认 {waiting} / blocked {blocked}）", f"时间：{now}", ""]
    lines = [it["line"] for it in items]
    tail = ["", "处理：",
            "  选题：python3 orchestrator/loop.py approve --run-id <id> --gate topic",
            "  发布：python3 orchestrator/loop.py approve --run-id <id> --gate publish",
            "  解封：python3 orchestrator/loop.py resume --run-id <id>"]
    return "\n".join(head + lines + tail)


# ---------------------------------------------------------------- 去重记录


def _load_seen(path: Path) -> dict[str, float]:
    """读去重文件（坏文件按空处理，通知不值得因为它失败）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    seen: dict[str, float] = {}
    for key, value in data.items():
        try:
            seen[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return seen


# ---------------------------------------------------------------- 对外


def notify_gates(store: "RunStore", runs: list["Run"], *, dry_run: bool = False) -> bool:
    """把 waiting/blocked 的 run 汇总成一条消息推到飞书；推了返回 True。

    返回 False 的情况都算正常：没有待通知项 / 没配 webhook / dry_run /
    全部被 12h 去重掉 / 推送失败（失败已降级为 warn）。
    """
    items = _collect(runs)
    if not items:
        return False
    url = webhook_url()
    if not url:
        return False
    if dry_run:
        print(f"[dry-run] 有 {len(items)} 项待通知（未发送、未写去重记录）")
        return False

    root = Path(store.root)
    path = root / STATE_NAME
    now_ts = time.time()
    seen = {k: v for k, v in _load_seen(path).items() if now_ts - v < DEDUP_SECONDS}
    fresh = [it for it in items if now_ts - seen.get(it["signature"], 0) >= DEDUP_SECONDS]
    if not fresh:
        return False

    try:
        _post(url, _compose(fresh))
    except Exception as exc:  # noqa: BLE001 - 通知失败绝不拖垮 tick
        print(f"[warn] 闸门通知发送失败：{type(exc).__name__}: {exc}")
        return False

    for it in fresh:
        seen[it["signature"]] = now_ts
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:  # 已发出去了，写不进去只影响下一轮去重
        print(f"[warn] 通知去重记录写入失败：{exc}")
    print(f"已推送闸门通知：{len(fresh)} 项")
    return True
