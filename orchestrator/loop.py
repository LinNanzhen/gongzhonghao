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
import time
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
from orchestrator.providers.base import ProviderError, get_provider  # noqa: E402
from orchestrator.state import (                 # noqa: E402
    GATES, Run, RunStore, Source, IllegalTransition, run_id_for, normalize_url,
)


# ---------------------------------------------------------------- 策略

POLICY_PATH = HERE / "policy.json"
DEFAULT_POLICY: dict = {"auto_approve": {"topic": False, "publish": False},
                        "auto_transcribe": {"enabled": False}}


def load_policy() -> dict:
    if POLICY_PATH.exists():
        try:
            return json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return DEFAULT_POLICY


def try_auto_gate(store: RunStore, run: Run, policy: dict) -> bool:
    """若策略允许，放行当前闸门并返回 True。"""
    gate = run.waiting_gate
    if not gate or is_foreign(run):
        return False
    if not (policy.get("auto_approve") or {}).get(gate):
        return False
    if gate == "publish":
        run.gates["publish"] = True
        run.advance("published", "auto", "策略自动放行发布")
    else:
        run.gates[gate] = True
    store.save(run, f"auto_approve\t{gate}")
    return True


def apply_auto_approve(store: RunStore, runs: list[Run], policy: dict) -> list[Run]:
    """按策略自动放行闸门；放行记录写进 events.log，可审计。"""
    auto = policy.get("auto_approve") or {}
    changed = []
    for run in runs:
        if run.status != "active" or is_foreign(run):
            continue
        gate = run.waiting_gate
        if not gate or not auto.get(gate):
            continue
        if gate == "publish":
            run.gates["publish"] = True
            run.advance("published", "auto", "策略自动放行发布")
        else:
            run.gates[gate] = True
        store.save(run, f"auto_approve\t{gate}")
        changed.append(run)
    return changed


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


def case_path(run: Run) -> Path:
    """把 state.json 里的 case_dir 解析成本机路径。

    存的是相对名（`01_某某_案例`），这样云端 `cases/` 与本地根目录都能用同一份状态。
    兼容早期写入的绝对路径：存在就用，不存在退回按名字找。
    """
    if not run.case_dir:
        return allocate_case_dir("待确认人物", run.run_id)
    raw = Path(run.case_dir)
    if raw.is_absolute():
        return raw if raw.exists() else BASE / raw.name
    return BASE / raw


def is_foreign(run: Run) -> bool:
    """判断 run 是否属于别的运行环境（如云端 run 被拉到本地）。

    判据：case_dir 是绝对路径、在本地不存在、且不在当前 BASE 之下。
    """
    if not run.case_dir:
        return False
    raw = Path(run.case_dir)
    if not raw.is_absolute() or raw.exists():
        return False
    return not str(raw).startswith(str(BASE))


def make_ctx(run: Run, provider, *, dry_run: bool, render: bool) -> Context:
    case = case_path(run)
    return Context(run=run, case_dir=case, orch_dir=HERE,
                   provider=provider, dry_run=dry_run, render=render)


# ---------------------------------------------------------------- 建 run


def ingest(store: RunStore, events, *, dry_run: bool) -> list[Run]:
    existing = {r.run_id for r in store.all()}
    fresh = ev.dedupe(events, existing)
    created: list[Run] = []
    for e in fresh:
        from orchestrator.fetcher import fetch_oembed
        if not e.source.title:
            meta = fetch_oembed(e.source.url)
            if meta.get("title"):
                e.source.title = meta["title"]
            if meta.get("channel"):
                e.source.channel = meta["channel"]
        from orchestrator.agents.scout import guess_guest
        guest = guess_guest(e.source)
        case = allocate_case_dir(guest, e.run_id)
        if dry_run:
            print(f"[dry-run] 将新建 run {e.run_id} → {case.name}")
            continue
        run = store.create(e.run_id, e.source, case.name)
        store.save(run, f"case_dir\t{case.name}")
        created.append(run)
    return created


# ---------------------------------------------------------------- tick


def step(store: RunStore, run: Run, provider, *, dry_run: bool, render: bool) -> str:
    if run.status != "active":
        return f"skip（status={run.status}）"
    if run.waiting_gate:
        return f"wait（人工闸门：{run.waiting_gate}）"
    if is_foreign(run):
        return "skip（属于其他环境的 run：设 GZH_BASE 或换到该环境执行）"

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
    try:
        provider = get_provider(args.provider, **pkw)
    except ProviderError as exc:
        print(f"provider 配置错误：{exc}", file=sys.stderr)
        print("提示：provider 填供应商名（stub/deepseek/kimi/qwen/openai），key 请放 GZH_LLM_API_KEY",
              file=sys.stderr)
        return 2

    # 真实 provider 缺 key 时提前失败，避免每个 run 各报一次错
    if args.provider not in ("stub",) and not getattr(provider, "api_key", ""):
        print("缺少 API key：请设置 GZH_LLM_API_KEY（或改用 --provider stub）", file=sys.stderr)
        return 2

    policy = load_policy()
    collected = [] if args.skip_ingest else list(ev.scan_inbox(HERE / "inbox"))
    warnings: list[str] = []
    if args.scan and not args.skip_ingest:
        found, warnings = ev.scan_sources(HERE / "sources.json", days=args.days)
        collected += found

    for w in warnings:
        print(f"[warn] {w}")

    created = ingest(store, collected, dry_run=args.dry_run)
    if created:
        print(f"新建 {len(created)} 个 run")

    runs = store.all()
    # 老 run 补标题（建 run 时网络可能不通）
    from orchestrator.fetcher import fetch_oembed
    for r in runs:
        if r.status == "active" and not r.source.title:
            meta = fetch_oembed(r.source.url)
            if meta.get("title"):
                r.source.title = meta["title"]
                r.source.channel = meta.get("channel", "")
                store.save(r, f"enrich\ttitle={meta['title'][:40]}")
    for r in apply_auto_approve(store, runs, policy):
        print(f"auto 放行 {r.run_id}（{r.stage}）")
    runs = store.all()
    todo = [r for r in runs if r.status == "active" and not r.waiting_gate]
    if args.run_id:
        todo = [r for r in todo if r.run_id == args.run_id]
    if args.max_runs:
        todo = todo[:args.max_runs]

    for run in todo:
        label = f"→ {run.run_id} [{run.case_dir or '?'}]"
        for i in range(6):
            run = store.get(run.run_id)
            if run is None or run.status != "active":
                break
            if run.waiting_gate:
                if try_auto_gate(store, run, policy):
                    print(f"{label} : auto 放行 {run.gates}")
                    continue
                print(f"{label} : wait（人工闸门：{run.waiting_gate}）")
                break
            msg = step(store, run, provider, dry_run=args.dry_run, render=args.render)
            print(f"{label} : {msg}")
            if msg.startswith(("skip", "wait", "error", "rework", "blocked")):
                break

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
    run = store.create(rid, src, case.name)
    store.save(run, f"case_dir\t{case.name}")
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


def cmd_ping(args) -> int:
    """连通性自检：调一次模型，确认 key / 端点 / 模型名都对。"""
    pkw = {k: getattr(args, k) for k in ("model", "base_url") if getattr(args, k, "")}
    try:
        provider = get_provider(args.provider, **pkw)
    except ProviderError as exc:
        print(f"provider 配置错误：{exc}", file=sys.stderr)
        return 2
    if provider.name != "stub" and not getattr(provider, "api_key", ""):
        print("缺少 API key：请设置 GZH_LLM_API_KEY（或 DEEPSEEK_API_KEY）", file=sys.stderr)
        return 2

    t0 = time.time()
    try:
        out = provider.complete(task="ping", system="你是连通性探针，只回复 ok。",
                                prompt="只回复 ok", max_tokens=64, temperature=0)
    except ProviderError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 2
    print(f"ok  provider={provider.name}  model={getattr(provider, 'model', '-')}  "
          f"{time.time() - t0:.1f}s  -> {out.strip()[:40]}")
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
    t.add_argument("--run-id", default="", help="只推进指定 run（自检/排障用）")
    t.add_argument("--skip-ingest", action="store_true", help="不扫描 inbox/sources（自检用）")
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

    pg = sub.add_parser("ping", help="模型连通性自检")
    pg.add_argument("--provider", default="stub")
    pg.add_argument("--model", default="")
    pg.add_argument("--base-url", default="", dest="base_url")
    pg.set_defaults(func=cmd_ping)

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
