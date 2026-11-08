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
import shutil
import sys
import time
from datetime import date, datetime, timedelta, timezone
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

try:                       # POSIX（本地 macOS / GitHub Actions Linux）
    import fcntl
except ImportError:        # pragma: no cover - 非 POSIX 平台降级为 no-op
    fcntl = None


class TickLock:
    """tick 单实例锁（T2d）：同一 `runs/` 目录同时只允许一个 tick。

    用 `fcntl.flock(LOCK_EX|LOCK_NB)` 锁 `runs/.lock`：拿不到锁说明另一个 tick
    正在跑，调用方打印提示后本轮直接跳过（退出码 0，不拖垮定时任务）。
    非 POSIX（import fcntl 失败）降级为 no-op——宁可无锁，也不要跑不起来。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def __enter__(self) -> bool:
        if fcntl is None:      # 非 POSIX：降级 no-op，永远"拿到"
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def __exit__(self, *exc_info) -> None:
        if self._fh is None:
            return
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:        # pragma: no cover - 释放失败没有补救手段
            pass
        finally:
            self._fh.close()
            self._fh = None


# ---------------------------------------------------------------- 策略

POLICY_PATH = HERE / "policy.json"
DEFAULT_POLICY: dict = {"auto_approve": {"topic": False},
                        "auto_transcribe": {"enabled": False}}


def load_policy() -> dict:
    if POLICY_PATH.exists():
        try:
            return json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return DEFAULT_POLICY


def try_auto_gate(store: RunStore, run: Run, policy: dict) -> bool:
    """若策略允许，放行当前闸门并返回 True。

    T4a：**发布永不自动**。即使 policy 里手写了 `auto_approve.publish=true`，
    这里也不读它——发布只能由人 `approve --gate publish` 放行。
    """
    gate = run.waiting_gate
    if not gate or is_foreign(run):
        return False
    if gate == "publish":       # 不读 policy，永远不自动发布
        return False
    if not (policy.get("auto_approve") or {}).get(gate):
        return False
    run.gates[gate] = True
    store.save(run, f"auto_approve\t{gate}")
    return True


def apply_auto_approve(store: RunStore, runs: list[Run], policy: dict) -> list[Run]:
    """按策略自动放行闸门；放行记录写进 events.log，可审计。

    T4a：publish 分支已删除——发布动作永远留给人工，policy 写了 true 也不生效。
    """
    auto = policy.get("auto_approve") or {}
    changed = []
    for run in runs:
        if run.status != "active" or is_foreign(run):
            continue
        gate = run.waiting_gate
        if not gate or gate == "publish":   # 发布永不自动
            continue
        if not auto.get(gate):
            continue
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


# ---------------------------------------------------------------- 成本记账 / 产物归一化


_USAGE_KEYS = ("input", "output", "total", "calls")


def _usage_snapshot(provider) -> dict[str, int] | None:
    """取 provider.usage_total 的当前快照；provider 不支持成本记账时返回 None。"""
    total = getattr(provider, "usage_total", None)
    if not isinstance(total, dict):
        return None
    return {k: int(total.get(k, 0) or 0) for k in _USAGE_KEYS}


def _usage_delta(before: dict | None, after: dict | None) -> dict[str, int]:
    """两次快照的差 = 这一步真正花掉的 token（T5b）。"""
    if before is None or after is None:
        return {}
    return {k: after.get(k, 0) - before.get(k, 0)
            for k in _USAGE_KEYS if after.get(k, 0) - before.get(k, 0)}


def _absorb_usage(run: Run, before: dict | None, provider) -> dict[str, int]:
    """把 provider 本步新增的用量累加进 run.usage，返回 delta（供事件日志）。"""
    delta = _usage_delta(before, _usage_snapshot(provider))
    if delta:
        run.add_usage(delta)
    return delta


def _usage_note(delta: dict[str, int]) -> str:
    """把 delta 压成 events.log 里的一行注解；没有花费就返回空串。"""
    if not delta:
        return ""
    return (f"\tusage\tinput={delta.get('input', 0)}"
            f"\toutput={delta.get('output', 0)}\ttotal={delta.get('total', 0)}"
            f"\tcalls={delta.get('calls', 0)}")


def normalize_artifacts(artifacts: dict[str, str] | None, case_dir: Path) -> dict[str, str]:
    """把 artifacts 归一化为相对 `case_dir` 的 posix 路径（T3c 写侧）。

    九个 agent 各自写 `str(path)`，云端会把 `/home/runner/...` 存进 state.json，
    拉回本地后读取侧找不到文件。在 loop 写状态这一处统一转换：
    - 在 case_dir 之下 → 相对路径（posix，Windows 反斜杠也归一）；
    - 不在其下 / 无法解析 → 原样保留（外部引用不该被改写）。
    """
    out: dict[str, str] = {}
    try:
        base = Path(case_dir).resolve()
    except OSError:            # pragma: no cover - resolve 极少失败
        base = Path(case_dir)
    for key, value in (artifacts or {}).items():
        raw = "" if value is None else str(value)
        if not raw:
            out[key] = raw
            continue
        p = Path(raw)
        if not p.is_absolute():
            out[key] = p.as_posix()
            continue
        try:
            out[key] = p.resolve().relative_to(base).as_posix()
        except (OSError, ValueError):
            out[key] = str(p)
    return out


def _last_activity(run: Run) -> datetime | None:
    """run 最近一次 history 的时间（本地时区）；读不到返回 None。"""
    candidates = [h.get("at") for h in reversed(run.history or [])
                  if isinstance(h, dict) and h.get("at")]
    candidates += [run.updated_at, run.created_at]
    for raw in candidates:
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if dt.tzinfo is None:          # 兼容手写/历史无时区记录：按 UTC 理解
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone()
    return None


def _usage_bucket() -> dict[str, int]:
    return {"input": 0, "output": 0, "total": 0,
            "runs": 0, "reported": 0, "unreported": 0}


def usage_summary(runs: list[Run], *, today: date | None = None) -> dict[str, dict[str, int]]:
    """按 run 最近一次 history 时间归日，汇总今日/本周 tokens（T5b）。

    记账是近似的：usage 是 run 级累计，只能按"最后活动日"归集（规格如此）。
    供应商没回 usage 的 run 计入 unreported，如实标"未上报"，不伪装成 0。
    """
    today = today or date.today()
    week_start = today - timedelta(days=today.weekday())
    buckets = {"today": _usage_bucket(), "week": _usage_bucket()}
    for run in runs:
        at = _last_activity(run)
        if at is None:
            continue
        day = at.date()
        usage = run.usage or {}
        reported = any(int(usage.get(k, 0) or 0) > 0 for k in ("input", "output", "total"))
        for key, in_scope in (("today", day == today),
                              ("week", week_start <= day <= today)):
            if not in_scope:
                continue
            bucket = buckets[key]
            bucket["runs"] += 1
            if not reported:
                bucket["unreported"] += 1
                continue
            bucket["reported"] += 1
            for field in ("input", "output", "total"):
                bucket[field] += int(usage.get(field, 0) or 0)
    return buckets


def _usage_line(label: str, bucket: dict[str, int]) -> str:
    if bucket["runs"] == 0:
        return f"{label} tokens：无记录"
    if bucket["reported"] == 0:
        return f"{label} tokens：未上报（{bucket['unreported']} 个 run 无 usage 数据）"
    extra = f"，{bucket['unreported']} 个 run 未上报" if bucket["unreported"] else ""
    return (f"{label} tokens：{bucket['total']}"
            f"（input={bucket['input']}，output={bucket['output']}，"
            f"{bucket['reported']} 个 run 上报{extra}）")


def _tokens_text(run: Run) -> str:
    usage = run.usage or {}
    total = int(usage.get("total", 0) or 0)
    if total <= 0:
        return "未上报"
    return f"{total:,}"


def print_broken(store: RunStore) -> int:
    """打印坏 state.json 告警，返回坏文件数（T2c：不静默、不中断）。"""
    for item in store.broken:
        print(f"[warn] state.json 解析失败：{item['path']}（{item['error']}）")
    return len(store.broken)


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
        if e.attachments:
            tr = case / "转写"
            tr.mkdir(parents=True, exist_ok=True)
            for att in e.attachments:
                shutil.copy2(att, tr / att.name)
            store.save(run, f"copy_transcript\t{','.join(a.name for a in e.attachments)}")
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
    before = _usage_snapshot(provider)
    try:
        result = agent.run(ctx)
    except Exception as exc:  # noqa: BLE001 - 单阶段失败不应中断整轮
        delta = _absorb_usage(run, before, provider)
        run.record_error(agent.name, exc)
        if not dry_run:
            store.save(run, f"error\t{agent.name}\t{exc}{_usage_note(delta)}")
        return f"error（{agent.name}：{type(exc).__name__}）"

    # T5b：agent 执行前后 provider.usage_total 的差值 = 这一步真实花费
    delta = _absorb_usage(run, before, provider)
    usage_note = _usage_note(delta)

    if dry_run:
        return f"[dry-run] {agent.name} → {result.status}（{result.note}）"

    if result.status == "ok":
        target = result.to or run.stage
        # T3c：写侧在 loop 统一归一化，九个 agent 不用逐个改
        artifacts = normalize_artifacts(result.artifacts, ctx.case_dir)
        run.advance(target, agent.name, result.note, artifacts)
        store.save(run, f"advance\t{agent.name}\t{run.stage}{usage_note}")
        return f"ok（{agent.name} → {run.stage}）"

    if result.status == "rework":
        reason = result.reason or result.note
        # T3c 缺陷修复：按 agent 名分流，不再按 stage 一刀切。
        # 原逻辑 composer 失败（stage=critiqued）会 rewind researched，白烧 writer+critic 两轮。
        rewind_to = {"critic": "researched", "gatekeeper": "critiqued"}.get(agent.name)
        if rewind_to is not None:
            try:
                run.rewind(rewind_to, agent.name, reason)
            except IllegalTransition:      # 防脏状态：回退不合法时降级为原地重试
                run.record_rework(agent.name, reason)
        else:
            # composer 等：原地重试（记录打回原因，但不改 stage）
            run.record_rework(agent.name, reason)
        store.save(run, f"rework\t{agent.name}\t{reason}{usage_note}")
        return f"rework（{agent.name} → {run.stage}）"

    if result.status == "blocked":
        run.status = "blocked"
        run.error = result.reason or result.note
        store.save(run, f"blocked\t{agent.name}\t{run.error}{usage_note}")
        return f"blocked（{agent.name}）"

    # skip（幂等/缺前置不推进）：不写状态转移，但如果花了 token 仍要落账
    if delta:
        store.save(run, f"skip\t{agent.name}{usage_note}")
    return f"skip（{agent.name}：{result.note}）"


def cmd_tick(args) -> int:
    store = RunStore(HERE / "runs")
    # T2d：本地与云端/两个进程同时 tick 时，第二个直接跳过（退出码 0）
    with TickLock(store.root / ".lock") as locked:
        if not locked:
            print("另一个 tick 正在运行，本轮跳过（runs/.lock 被占用）")
            return 0
        return _tick(args, store)


def _tick(args, store: RunStore) -> int:
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
    # T2c：坏 state.json 要可见；tick 只告警、不因此失败（不拖垮定时任务）
    broken_count = print_broken(store)
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

    # T2a：一次 tick 默认只推一步（失败可定位的根基）；多步必须显式 --steps
    steps = max(1, int(args.steps))
    for run in todo:
        label = f"→ {run.run_id} [{run.case_dir or '?'}]"
        for i in range(steps):
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

    # 重新读：上面的推进改变了 stage/status，汇总与通知必须用最新状态
    runs = store.all()
    if store.broken:
        broken_count = len(store.broken)

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
          f"blocked={len(blocked)}，done={sum(r.status=='done' for r in runs)}，"
          f"broken={broken_count}）")

    # T4b：tick 末尾推一次闸门通知（--no-notify 可关；失败只 warn）
    if not args.no_notify:
        try:
            from orchestrator.notify import notify_gates
            notify_gates(store, runs, dry_run=args.dry_run)
        except Exception as exc:  # noqa: BLE001 - 通知异常绝不能拖垮定时任务
            print(f"[warn] 闸门通知异常：{type(exc).__name__}: {exc}")
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
    # T2c：坏 state.json 不静默；status 对坏文件返回退出码 1（可被 CI 感知）
    broken_count = print_broken(store)
    summary = usage_summary(runs)
    if args.json:
        payload = {
            "runs": [r.to_dict() for r in runs],
            "usage": summary,
            "broken": store.broken,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1 if broken_count else 0
    if not runs:
        print("（没有 run）")
        return 1 if broken_count else 0
    print(f"{'run_id':14s} {'stage':18s} {'status':8s} {'rework':6s} {'tokens':>10s} 案例")
    for r in runs:
        gate = f" [gate:{r.waiting_gate}]" if r.waiting_gate else ""
        print(f"{r.run_id:14s} {r.stage:18s} {r.status:8s} {r.rework.get('count', 0):<6} "
              f"{_tokens_text(r):>10s} "
              f"{Path(r.case_dir).name if r.case_dir else '-'}{gate}")
        if r.status == "blocked" and r.error:
            print(f"    ! {r.error}")
    print()
    print(_usage_line("今日", summary["today"]))
    print(_usage_line("本周", summary["week"]))
    return 1 if broken_count else 0


def cmd_rewind(args) -> int:
    """人工回退到某个阶段重跑（改完提示词后重试）。"""
    store = RunStore(HERE / "runs")
    run = store.get(args.run_id)
    if run is None:
        print(f"找不到 run：{args.run_id}")
        return 2
    try:
        run.rewind(args.to, "human", f"人工回退：{args.note or ''}")
    except IllegalTransition as exc:
        print(str(exc))
        return 2
    run.status = "active"
    run.error = None
    store.save(run, f"rewind\t{args.to}\t{args.note or ''}")
    print(f"已回退：{run.run_id} → {run.stage}（下次 tick 重跑）")
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
    t.add_argument("--steps", type=int, default=1,
                   help="每个 run 本轮最多推进几个阶段（默认 1：一次 tick 只推一步）")
    t.add_argument("--render", action="store_true", help="排版时跑三档 Chrome 渲染")
    t.add_argument("--dry-run", action="store_true", help="只打印计划，不写文件")
    t.add_argument("--run-id", default="", help="只推进指定 run（自检/排障用）")
    t.add_argument("--skip-ingest", action="store_true", help="不扫描 inbox/sources（自检用）")
    t.add_argument("--no-notify", action="store_true", dest="no_notify",
                   help="本轮不推送闸门通知（默认推；webhook 未配置时自动静默）")
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

    rw = sub.add_parser("rewind", help="人工回退到某个阶段")
    rw.add_argument("--run-id", required=True)
    rw.add_argument("--to", required=True)
    rw.add_argument("--note", default="")
    rw.set_defaults(func=cmd_rewind)

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
