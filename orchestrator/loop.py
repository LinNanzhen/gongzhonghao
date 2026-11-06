#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多智能体编排器入口（SOP v9）。

子命令：
  tick     推进所有 active run 一步（事件扫描 + 建 run + 跑一个 stage）
  submit   手动建任务
  status   查看 run 列表
  approve  人工闸门放行（topic / publish）

示例：
  python3 orchestrator/loop.py tick --scan --max-runs 3
  python3 orchestrator/loop.py submit --url "https://www.youtube.com/watch?v=xxxx" --title "某人访谈"
  python3 orchestrator/loop.py approve --run-id 1a2b3c4d5e6f --gate topic
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import os

HERE = Path(__file__).resolve().parent          # gongzhonghao/orchestrator
PKG_ROOT = HERE.parent                           # gongzhonghao
# 案例根目录：本地默认是仓库父目录（公众号/）；云端用 GZH_BASE 指到仓库内 cases/
BASE = Path(os.environ.get("GZH_BASE") or PKG_ROOT.parent).resolve()
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from orchestrator import events as ev            # noqa: E402
from orchestrator.agents import AGENTS           # noqa: E402
from orchestrator.agents.base import Context     # noqa: E402
from orchestrator.providers.base import get_provider  # noqa: E402
from orchestrator.state import (                 # noqa: E402
    GATES, Run, RunStore, Source, IllegalTransition, run_id_for, normalize_url,
)


# ---------------------------------------------------------------- 工具


def allocate_case_dir(guest: str, run_id: str) -> Path:
    """在根目录分配下一个 NN_XX_案例。"""
    nums = []
    for p in BASE.iterdir():
        if p.is_dir():
            head = p.name.split("_", 1)[0]
            if head.isdigit() and not p.name.startswith("_"):
                nums.append(int(head))
    nxt = (max(nums) + 1) if nums else 1
    safe = guest.replace("/", "_").strip() or "未命名"
    target = BASE / f"{nxt:02d}_{safe}_案例"
    if target.exists():
        target = BASE / f"{nxt:02d}_{safe}_{run_id}_案例"
    return target


def make_ctx(run: Run, provider, *, dry_run: bool, render: bool) -> Context:
    case = Path(run.case_dir) if run.case_dir else allocate_case_dir("待确认人物", run.run_id)
    return Context(run=run, case_dir=case, orch_dir=HERE,
                   provider=provider, dry_run=dry_run, render=render)


# ---------------------------------------------------------------- 建 run


def ingest(store: RunStore, events, *, dry_run: bool) -> list[Run]:
    existing = {r.run_id for r in store.all()}
    fresh = ev.dedupe(events, existing)
    created: list[Run] = []
    for e in fresh:
        from orchestrator.agents.scout import guess_guest
        guest = guess_guest(e.source)
        case = allocate_case_dir(guest, e.run_id)
        if dry_run:
            print(f"[dry-run] 将新建 run {e.run_id} → {case.name}")
            continue
        run = store.create(e.run_id, e.source, str(case))
        store.save(run, f"case_dir\t{case}")
        created.append(run)
    return created


# ---------------------------------------------------------------- tick


def step(store: RunStore, run: Run, provider, *, dry_run: bool, render: bool) -> str:
    if run.status != "active":
        return f"skip（status={run.status}）"
    if run.waiting_gate:
        return f"wait（人工闸门：{run.waiting_gate}）"

    agent_name = run.next_agent
    agent = AGENTS.get(agent_name or "")
    if agent is None:
        return "skip（该阶段无 agent）"

    ctx = make_ctx(run, provider, dry_run=dry_run, render=render)
    try:
        result = agent.run(ctx)
    except Exception as exc:  # noqa: BLE001 - 单阶段失败不应中断整轮
        run.record_error(agent.name, exc)
        if not dry_run:
            store.save(run, f"error\t{agent.name}\t{exc}")
        return f"error（{agent.name}：{type(exc).__name__}）"

    if dry_run:
        return f"[dry-run] {agent.name} → {result.status}（{result.note}）"

    if result.status == "ok":
        target = result.to or run.stage
        run.advance(target, agent.name, result.note, result.artifacts)
        store.save(run, f"advance\t{agent.name}\t{run.stage}")
        return f"ok（{agent.name} → {run.stage}）"

    if result.status == "rework":
        if run.stage == "composed":
            run.rewind("critiqued", agent.name, result.reason)
        elif run.stage in ("drafted", "critiqued"):
            run.rewind("researched", agent.name, result.reason)
        else:
            run.record_rework(agent.name, result.reason)
        store.save(run, f"rework\t{agent.name}\t{result.reason}")
        return f"rework（{agent.name} → {run.stage}）"

    if result.status == "blocked":
        run.status = "blocked"
        run.error = result.reason
        store.save(run, f"blocked\t{agent.name}\t{result.reason}")
        return f"blocked（{agent.name}）"

    return f"skip（{agent.name}：{result.note}）"


def cmd_tick(args) -> int:
    store = RunStore(HERE / "runs")
    pkw = {k: getattr(args, k) for k in ("model", "base_url") if getattr(args, k, "")}
    provider = get_provider(args.provider, **pkw)

    # 真实 provider 缺 key 时提前失败，避免每个 run 各报一次错
    if args.provider not in ("stub",) and not getattr(provider, "api_key", ""):
        print("缺少 API key：请设置 GZH_LLM_API_KEY（或改用 --provider stub）", file=sys.stderr)
        return 2

    collected = list(ev.scan_inbox(HERE / "inbox"))
    warnings: list[str] = []
    if args.scan:
        found, warnings = ev.scan_sources(HERE / "sources.json", days=args.days)
        collected += found

    for w in warnings:
        print(f"[warn] {w}")

    created = ingest(store, collected, dry_run=args.dry_run)
    if created:
        print(f"新建 {len(created)} 个 run")

    runs = store.all()
    todo = [r for r in runs if r.status == "active" and not r.waiting_gate]
    if args.max_runs:
        todo = todo[:args.max_runs]

    for run in todo:
        print(f"→ {run.run_id} [{run.case_dir or '?'}] : {step(store, run, provider, dry_run=args.dry_run, render=args.render)}")

    waiting = [r for r in runs if r.waiting_gate and r.status == "active"]
    if waiting:
        print(f"\n等待人工闸门：{len(waiting)} 个")
        for r in waiting:
            print(f"  {r.run_id}  {GATES[r.stage]:8s}  {Path(r.case_dir).name}")

    blocked = [r for r in runs if r.status == "blocked"]
    if blocked:
        print(f"\n需要介入：{len(blocked)} 个 blocked")
        for r in blocked:
            print(f"  {r.run_id}  {r.error}")

    print(f"\n总计 run：{len(runs)}（active={sum(r.status=='active' for r in runs)}，"
          f"blocked={len(blocked)}，done={sum(r.status=='done' for r in runs)}）")
    return 0


# ---------------------------------------------------------------- submit / status / approve


def cmd_submit(args) -> int:
    store = RunStore(HERE / "runs")
    url = normalize_url(args.url)
    rid = run_id_for(url)
    if store.exists(rid):
        print(f"已存在：{rid}（幂等，不新建）")
        return 0
    from orchestrator.agents.scout import guess_guest
    src = Source(kind="cli", url=url, title=args.title, notes=args.notes)
    case = allocate_case_dir(guess_guest(src), rid)
    run = store.create(rid, src, str(case))
    store.save(run, f"case_dir\t{case}")
    print(f"已建：{rid}\n案例目录：{case}")
    return 0


def cmd_status(args) -> int:
    store = RunStore(HERE / "runs")
    runs = store.all()
    if args.json:
        print(json.dumps([r.to_dict() for r in runs], ensure_ascii=False, indent=2))
        return 0
    if not runs:
        print("（没有 run）")
        return 0
    print(f"{'run_id':14s} {'stage':18s} {'status':8s} {'rework':6s} 案例")
    for r in runs:
        gate = f" [gate:{r.waiting_gate}]" if r.waiting_gate else ""
        print(f"{r.run_id:14s} {r.stage:18s} {r.status:8s} {r.rework.get('count', 0):<6} "
              f"{Path(r.case_dir).name if r.case_dir else '-'}{gate}")
        if r.status == "blocked" and r.error:
            print(f"    ! {r.error}")
    return 0


def cmd_resume(args) -> int:
    """人工介入后解封 blocked/stale 的 run，重置重试计数。"""
    store = RunStore(HERE / "runs")
    run = store.get(args.run_id)
    if run is None:
        print(f"找不到 run：{args.run_id}")
        return 2
    if run.status == "done":
        print("已归档（done），无需解封")
        return 0
    prev = run.status
    run.status = "active"
    run.error = None
    run.errors = 0
    run.rework = {"count": 0, "last_reason": ""}
    store.save(run, f"resume\t{prev}\t{args.note or 'manual'}")
    print(f"已解封：{run.run_id}（{prev} → active，stage={run.stage}）")
    return 0


def cmd_approve(args) -> int:
    store = RunStore(HERE / "runs")
    run = store.get(args.run_id)
    if run is None:
        print(f"找不到 run：{args.run_id}")
        return 2
    if args.gate == "topic":
        if run.stage != "triaged":
            print(f"当前 stage={run.stage}，不是 triaged，不能确认选题")
            return 2
        # 闸门只解锁，不代替阶段执行：transcriber 仍须自己跑
        run.gates["topic"] = True
        store.save(run, f"approve\ttopic\t{args.note or 'ok'}")
        print(f"已解锁：{run.run_id}（下次 tick 才会进入转写阶段）")
        return 0
    if args.gate == "publish":
        if run.stage != "awaiting_publish":
            print(f"当前 stage={run.stage}，不是 awaiting_publish，不能确认发布")
            return 2
        run.gates["publish"] = True
        run.advance("published", "human", f"发布确认：{args.note or 'ok'}")
        store.save(run, f"approve\tpublish\t{args.note or 'ok'}")
        print(f"已放行：{run.run_id} → {run.stage}（下次 tick 归档）")
        return 0
    print(f"未知闸门：{args.gate}")
    return 2


# ---------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="公众号多智能体编排器（SOP v9）")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("tick", help="推进一轮")
    t.add_argument("--scan", action="store_true", help="扫描 sources.json（RSS）")
    t.add_argument("--days", type=int, default=7, help="RSS 回看天数")
    t.add_argument("--max-runs", type=int, default=5, help="本轮最多推进几个 run")
    t.add_argument("--render", action="store_true", help="排版时跑三档 Chrome 渲染")
    t.add_argument("--dry-run", action="store_true", help="只打印计划，不写文件")
    t.add_argument("--provider", default="stub",
                   help="LLM provider：stub / openai-compat（别名 deepseek、kimi、qwen）")
    t.add_argument("--model", default="", help="模型名；默认取 GZH_LLM_MODEL")
    t.add_argument("--base-url", default="", dest="base_url",
                   help="OpenAI 兼容端点；默认取 GZH_LLM_BASE_URL")
    t.set_defaults(func=cmd_tick)

    s = sub.add_parser("submit", help="手动建任务")
    s.add_argument("--url", required=True)
    s.add_argument("--title", default="")
    s.add_argument("--notes", default="")
    s.set_defaults(func=cmd_submit)

    st = sub.add_parser("status", help="查看 run")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_status)

    r = sub.add_parser("resume", help="解封 blocked run")
    r.add_argument("--run-id", required=True)
    r.add_argument("--note", default="")
    r.set_defaults(func=cmd_resume)

    a = sub.add_parser("approve", help="人工闸门放行")
    a.add_argument("--run-id", required=True)
    a.add_argument("--gate", required=True, choices=["topic", "publish"])
    a.add_argument("--note", default="")
    a.set_defaults(func=cmd_approve)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
