#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0 独立验收脚本（verifier 专用，不修改任何产品代码）。

在**临时目录**里跑对抗场景，覆盖 task-3 要求的 10 类检查，输出 PASS/FAIL/SKIP
表与退出码。所有产物（sandbox 仓库副本、案例目录、runs/、报告）都写在临时目录，
**不写真实 `cases/` 与 `runs/`**，不联网（通知检查用本地 mock HTTP server）。

用法：
    python3 orchestrator/verify_p0.py              # 跑全部，自动清理现场
    python3 orchestrator/verify_p0.py --keep       # 保留临时目录便于排查
    python3 orchestrator/verify_p0.py --only 1,2   # 只跑编号前缀匹配的检查（开发用）
    python3 orchestrator/verify_p0.py --list       # 只列检查项

退出码：0 = 没有 FAIL（允许 SKIP）／1 = 至少一项 FAIL／2 = 脚本环境错误。

检查项与工单对应（权威规格：研究/agent-autoloop_启示与改造方案_v2.md §6）：
    C1 门禁三态            T1a/T1c   gatekeeper 判定分派
    C2 流水线判定码/退出码 T1a       --skip-render=3/INCOMPLETE、无 Chrome=3、渲染失败=1
    C3 critic 体检表       T1b       恒真列是否真的被修掉
    C4 一 tick 一步        T2a       默认 --steps 1
    C5 发布不可自动        T4a       policy.publish=true 也不放行
    C6 门禁不可信          T1a/T1c   读 composer/gatekeeper + 真实报告串联
    C7 版本化              T3a/T3b   composer v1/v2、writer _vN 递增不覆盖
    C8 artifacts 相对路径  T3c       相对可读、失效绝对路径按文件名回退
    C9 坏状态与并发锁      T2c/T2d   坏 json 告警、第二个 tick 拿不到锁
    C10 闸门通知           T4b       未配置静默、mock webhook 一条、12h 去重
    C11 附加               T2b/T5b/T5c  UTC now、status usage、死配置清理

纪律：只读产品代码；本脚本是 verifier 唯一可写的代码文件。发现 FAIL 只报告，
不改别人的文件（由 Lead 派回原 owner 修）。
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import importlib.util
import io
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent          # gongzhonghao/orchestrator
REPO = HERE.parent                               # gongzhonghao
PIPELINE = REPO / "流水线" / "gzh_pipeline.py"

TMPROOT: Path = Path(tempfile.mkdtemp(prefix="verify_p0_", dir="/tmp"))
VERBOSE = False
RESULTS: list[tuple[str, str, str, str]] = []    # (cid, title, status, detail)
CHECKS: list[tuple[str, str, object]] = []


# ================================================================ 断言与登记

class CheckFail(AssertionError):
    """检查未通过（产品行为与验收标准不符）。"""


class SkipCheck(Exception):
    """本机条件不满足，无法实测（不判 FAIL，但必须出现在表里）。"""


def expect(cond: bool, msg: str) -> None:
    if not cond:
        raise CheckFail(msg)


def check(cid: str, title: str):
    def deco(fn):
        CHECKS.append((cid, title, fn))
        return fn
    return deco


def short(text: str, limit: int = 220) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


# ================================================================ 通用工具

def live_import(name: str):
    """导入工作区（真实仓库）里的模块——验收对象就是这份代码。"""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    return importlib.import_module(name)


def new_case_dir(sbx: "Sandbox", name: str) -> Path:
    p = sbx.cases / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# 最小合规稿：能过流水线质检 + critic 机器项。
MD_OK = """# 验收样本：变化先落在入门岗位

## 01｜入门岗位先被重新定价

**变化最先落在入门岗位。**

> 「这是一句可以直接引用的原话。」（00:12）

## 02｜能力曲线与价格曲线之间出现裂缝

**裂缝来自采用速度，不是模型能力。**

> 第二处原话，带时间戳 01:03。

## 03｜变贵的是需要人来决定做什么的任务

**变贵的是那些需要人来决定要做什么的任务。**

> 第三处原话，带时间戳 02:20。

## 来源与说明

除署名缩写的对话原话外，本文的叙述、归纳与判断均为作者观点。
"""

# 反例稿：无加粗判断、无引语/时间戳、标题是话题词（critic 机器项必须打低分）。
MD_BAD = """# 验收样本：结构不合格的稿子

## 01｜背景

这篇稿子只做背景陈述，没有任何加粗判断，也没有任何引语和证据。

## 02｜现状

这一段描述现状，同样没有加粗判断句，也没有具体证据支撑。

## 03｜挑战

这一段谈挑战，还是没有加粗判断句，也没有任何可定位的引用。
"""


def write_qc_report(path: Path, *, total_line: str | None = None, code: str | None = None,
                    render_line: str = "", fail_rows: list[tuple[str, str]] | None = None,
                    extra: str = "") -> Path:
    """合成一份质检报告（对抗场景用）。"""
    parts = ["# 质检报告：验收合成", "", "- 生成时间：验收脚本"]
    if total_line is not None:
        parts.append(f"- 总判定：{total_line}")
    if code is not None:
        parts.append(f"- 判定码：{code}")
    parts += ["", "## 质检门禁", "", "| 检查项 | 结果 | 详情 |", "|---|---|---|"]
    if fail_rows:
        for name, detail in fail_rows:
            parts.append(f"| {name} | ❌ | {detail} |")
    else:
        parts.append("| HTML 硬规则门禁 | ✅ | 8+ 项门禁全过 |")
    parts += ["", "## 渲染验收", "", render_line or "| 375px | 100px | 无 | 正常 | ✓ |"]
    if extra:
        parts += ["", extra]
    return write_text(path, "\n".join(parts) + "\n")


def parse_critic_table(text: str) -> list[dict[str, str]]:
    """解析 critic 体检报告里的「四关逐节打分」表，返回 [{列名: 值}]。"""
    rows: list[dict[str, str]] = []
    header: list[str] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if "可复述" in cells:
            header = cells
            continue
        if header is None:
            continue
        if all(c and set(c) <= {"-", ":"} for c in cells):
            continue
        if len(cells) != len(header):
            continue
        rows.append(dict(zip(header, cells)))
    return rows


# ================================================================ 沙箱（仓库副本 + 临时案例根）

class Sandbox:
    """把 orchestrator/ 与 流水线/ 复制到临时目录，BASE 指到临时 cases/。

    这样 tick / composer / 通知去重文件等所有写操作都落在临时目录。
    """

    def __init__(self, name: str):
        self.name = name
        self.root = TMPROOT / name / "gzh"
        (self.root).mkdir(parents=True)
        shutil.copytree(REPO / "orchestrator", self.root / "orchestrator",
                        ignore=shutil.ignore_patterns("runs", "__pycache__", "inbox"))
        (self.root / "orchestrator" / "inbox").mkdir(parents=True, exist_ok=True)
        shutil.copytree(REPO / "流水线", self.root / "流水线",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.cases = self.root / "cases"
        self.cases.mkdir(parents=True, exist_ok=True)
        self.write_policy()

    # -------------------------------------------------- 路径
    @property
    def orch(self) -> Path:
        return self.root / "orchestrator"

    @property
    def runs(self) -> Path:
        return self.orch / "runs"

    # -------------------------------------------------- 环境 / 子进程
    def write_policy(self, data: dict | None = None) -> None:
        payload = data if data is not None else {
            "auto_approve": {"topic": False},
            "auto_transcribe": {"enabled": False},
        }
        write_text(self.orch / "policy.json", json.dumps(payload, ensure_ascii=False, indent=2))

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items()
               if k not in ("FEISHU_WEBHOOK", "GZH_FEISHU_WEBHOOK", "GZH_LLM_API_KEY")}
        env["GZH_BASE"] = str(self.cases)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if extra:
            env.update(extra)
        return env

    def run_loop(self, *args: str, extra_env: dict[str, str] | None = None,
                 timeout: int = 180) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "orchestrator/loop.py", *args],
            cwd=str(self.root), env=self.env(extra_env),
            capture_output=True, text=True, timeout=timeout)

    def run_pipeline_cli(self, md: Path, name: str, outdir: Path, *args: str,
                         timeout: int = 300) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "流水线/gzh_pipeline.py", "--md", str(md),
             "--name", name, "--outdir", str(outdir), *args],
            cwd=str(self.root), env=self.env(),
            capture_output=True, text=True, timeout=timeout)

    # -------------------------------------------------- 状态构造
    def write_run(self, run_id: str, *, stage: str = "new", status: str = "active",
                  case_dir: str = "99_验收_案例", artifacts: dict | None = None,
                  gates: dict | None = None, usage: dict | None = None,
                  source_title: str = "验收样本", history: list | None = None) -> dict:
        """直接写一份 state.json（绕过 ingest，便于构造任意阶段）。"""
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        state = {
            "run_id": run_id,
            "stage": stage,
            "status": status,
            "source": {"kind": "cli", "url": f"https://example.com/{run_id}",
                       "title": source_title, "channel": "", "notes": "", "published_at": ""},
            "case_dir": case_dir,
            "artifacts": artifacts or {},
            "history": history or [],
            "rework": {"count": 0, "last_reason": ""},
            "gates": {"topic": False, "publish": False, **(gates or {})},
            "errors": 0,
            "error": None,
            "created_at": stamp,
            "updated_at": stamp,
        }
        if usage is not None:
            state["usage"] = usage
        d = self.runs / run_id
        d.mkdir(parents=True, exist_ok=True)
        write_text(d / "state.json", json.dumps(state, ensure_ascii=False, indent=2))
        return state

    def read_run(self, run_id: str) -> dict:
        return json.loads((self.runs / run_id / "state.json").read_text(encoding="utf-8"))


# ================================================================ agent 直调

def _context(run, case_dir: Path, sbx: Sandbox, provider=None, *, render: bool = False):
    Base = live_import("orchestrator.agents.base")
    return Base.Context(run=run, case_dir=Path(case_dir), orch_dir=sbx.orch,
                        provider=provider, dry_run=False, render=render)


def run_gatekeeper(report: Path, *, stage: str = "composed", case_dir: Path | None = None,
                   sbx: Sandbox | None = None):
    """直接调 gatekeeper：给一份报告路径，看它判 ok/rework/blocked。"""
    State = live_import("orchestrator.state")
    Gate = live_import("orchestrator.agents.gatekeeper").Gatekeeper
    sbx = sbx or Sandbox("_gate_ctx")
    case = Path(case_dir) if case_dir else Path(report).parent
    run = State.Run(run_id="verify-gatekeeper", stage=stage,
                    artifacts={"qc_report": str(report)})
    return Gate().run(_context(run, case, sbx))


def run_composer(sbx: Sandbox, case: Path, *, draft_value: str | None = None,
                 draft_rel: str = "正文.md", render: bool = False):
    State = live_import("orchestrator.state")
    Comp = live_import("orchestrator.agents.composer").Composer
    run = State.Run(run_id="verify-composer", stage="critiqued",
                    artifacts={"draft": draft_value if draft_value is not None else draft_rel})
    return Comp().run(_context(run, case, sbx, render=render))


def run_critic(sbx: Sandbox, case: Path, *, draft_value: str | None = None,
               draft_rel: str = "正文.md"):
    State = live_import("orchestrator.state")
    Critic = live_import("orchestrator.agents.critic").Critic
    run = State.Run(run_id="verify-critic", stage="drafted",
                    artifacts={"draft": draft_value if draft_value is not None else draft_rel})
    return Critic().run(_context(run, case, sbx))


def run_writer(sbx: Sandbox, case: Path, provider):
    State = live_import("orchestrator.state")
    Writer = live_import("orchestrator.agents.writer").Writer
    run = State.Run(run_id="verify-writer", stage="researched", artifacts={})
    return Writer().run(_context(run, case, sbx, provider=provider))


# ================================================================ 流水线（in-process，便于 patch）

def load_pipeline_module(sbx: Sandbox):
    """按文件路径加载沙箱里的流水线模块（不污染当前仓库模块名）。"""
    path = sbx.root / "流水线" / "gzh_pipeline.py"
    mod_name = f"verify_gzh_pipeline_{re.sub(r'[^0-9a-zA-Z_]', '_', sbx.name)}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def run_pipeline_inproc(sbx: Sandbox, md_text: str, *, name: str,
                        patches: dict | None = None,
                        extra_args: tuple[str, ...] = ()) -> tuple[int, str, str]:
    """在进程内跑流水线 main()，返回 (退出码, 报告全文, 控制台输出)。

    patches 用于注入环境/故障（如 find_chrome→None、render_measure→报错），
    这是验收脚本自身的测试脚手架，不改产品代码。
    """
    pipe = load_pipeline_module(sbx)
    outdir = TMPROOT / sbx.name / "inproc" / name
    outdir.mkdir(parents=True, exist_ok=True)
    md = write_text(outdir / "正文.md", md_text)
    saved: dict[str, object] = {}
    buf = io.StringIO()
    argv_backup = sys.argv
    try:
        for attr, repl in (patches or {}).items():
            saved[attr] = getattr(pipe, attr)
            setattr(pipe, attr, repl)
        sys.argv = ["gzh_pipeline.py", "--md", str(md), "--name", name,
                    "--outdir", str(outdir), *extra_args]
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                code = pipe.main()
            except SystemExit as exc:      # 参数/环境错误
                code = int(exc.code or 0)
        code = int(code)
    finally:
        sys.argv = argv_backup
        for attr, value in saved.items():
            setattr(pipe, attr, value)
    report = outdir / f"{name}_质检报告.md"
    text = report.read_text(encoding="utf-8") if report.exists() else ""
    return code, text, buf.getvalue()


def find_report_verdict(report_text: str) -> str | None:
    m = re.search(r"判定码[：:]\s*\**\s*(PASS|FAIL|INCOMPLETE)\b", report_text, re.I)
    return m.group(1).upper() if m else None


# ================================================================ C1 门禁三态

@check("1.1", "门禁：判定码 PASS → ok/qc_passed")
def c11() -> str:
    sbx = Sandbox("c1_1")
    report = write_qc_report(TMPROOT / "c1_1" / "qc.md",
                             total_line="**通过**", code="PASS")
    res = run_gatekeeper(report, sbx=sbx)
    expect(res.status == "ok", f"返回 {res.status}（{res.reason or res.note}），期望 ok")
    expect(res.to == "qc_passed", f"to={res.to}，期望 qc_passed")
    return "PASS → ok(to=qc_passed)"


@check("1.2", "门禁：判定码 FAIL → rework")
def c12() -> str:
    sbx = Sandbox("c1_2")
    report = write_qc_report(TMPROOT / "c1_2" / "qc.md",
                             total_line="**未通过（需修正）**", code="FAIL",
                             fail_rows=[("HTML 硬规则门禁", "模拟：残留 div")])
    res = run_gatekeeper(report, sbx=sbx)
    expect(res.status == "rework", f"返回 {res.status}（{res.reason or res.note}），期望 rework")
    return f"FAIL → rework（{short(res.reason or res.note, 80)}）"


@check("1.3", "门禁：判定码 INCOMPLETE → blocked（不得放行）")
def c13() -> str:
    sbx = Sandbox("c1_3")
    report = write_qc_report(TMPROOT / "c1_3" / "qc.md",
                             total_line="**未完成（渲染验收未执行）**", code="INCOMPLETE",
                             render_line="已跳过（--skip-render）。")
    res = run_gatekeeper(report, sbx=sbx)
    expect(res.status == "blocked", f"返回 {res.status}（{res.reason or res.note}），期望 blocked")
    return f"INCOMPLETE → blocked（{short(res.reason or res.note, 80)}）"


@check("1.4", "门禁：旧报告（无判定码，仅总判定未通过）→ rework")
def c14() -> str:
    sbx = Sandbox("c1_4")
    report = write_qc_report(TMPROOT / "c1_4" / "qc.md",
                             total_line="**未通过（需修正）**")
    res = run_gatekeeper(report, sbx=sbx)
    expect(res.status == "rework", f"返回 {res.status}，期望 rework（旧格式需兼容）")
    return "旧格式未通过 → rework"


@check("1.5", "门禁：旧「假通过」报告（总判定通过+已跳过--skip-render）→ blocked")
def c15() -> str:
    sbx = Sandbox("c1_5")
    report = write_qc_report(TMPROOT / "c1_5" / "qc.md",
                             total_line="**通过**",
                             render_line="已跳过（--skip-render）。")
    res = run_gatekeeper(report, sbx=sbx)
    expect(res.status != "ok",
           "把「总判定通过 + 已跳过（--skip-render）」判为 ok——T1a②「跳过渲染不得判通过」未落实")
    expect(res.status == "blocked",
           f"返回 {res.status}（{res.reason or res.note}），期望 blocked（未做渲染验收）")
    return "历史假通过报告 → blocked"


@check("1.6", "门禁：报告无任何判定 → blocked（不可信）")
def c16() -> str:
    sbx = Sandbox("c1_6")
    report = write_qc_report(TMPROOT / "c1_6" / "qc.md")   # 无总判定、无判定码
    res = run_gatekeeper(report, sbx=sbx)
    expect(res.status == "blocked", f"返回 {res.status}（{res.reason or res.note}），期望 blocked")
    return "无判定报告 → blocked"


@check("1.7", "门禁：报告不存在 → rework")
def c17() -> str:
    sbx = Sandbox("c1_7")
    missing = TMPROOT / "c1_7" / "不存在.md"
    res = run_gatekeeper(missing, sbx=sbx)
    expect(res.status == "rework", f"返回 {res.status}，期望 rework")
    return "缺报告 → rework"


@check("1.8", "门禁：qc_passed 分支仍推进到 awaiting_publish")
def c18() -> str:
    sbx = Sandbox("c1_8")
    report = write_qc_report(TMPROOT / "c1_8" / "qc.md", total_line="**通过**", code="PASS")
    res = run_gatekeeper(report, stage="qc_passed", sbx=sbx)
    expect(res.status == "ok" and res.to == "awaiting_publish",
           f"返回 {res.status}/to={res.to}，期望 ok/to=awaiting_publish")
    return "qc_passed → awaiting_publish"


# ================================================================ C2 流水线判定码与退出码

@check("2.1", "流水线：--skip-render → 退出码 3 + 判定码 INCOMPLETE")
def c21() -> str:
    sbx = Sandbox("c2_1")
    outdir = TMPROOT / "c2_1" / "out"
    outdir.mkdir(parents=True)
    md = write_text(TMPROOT / "c2_1" / "正文.md", MD_OK)
    proc = sbx.run_pipeline_cli(md, "验收跳过渲染", outdir, "--skip-render")
    report = (outdir / "验收跳过渲染_质检报告.md")
    expect(report.exists(), f"没有生成质检报告；stderr={short(proc.stderr, 120)}")
    text = report.read_text(encoding="utf-8")
    expect(proc.returncode == 3, f"退出码 {proc.returncode}，期望 3（未完成）")
    expect(find_report_verdict(text) == "INCOMPLETE",
           f"报告判定码 {find_report_verdict(text)}，期望 INCOMPLETE")
    return "exit=3，报告含「判定码：INCOMPLETE」"


@check("2.2", "流水线：找不到 Chrome（非 --skip-render）→ 退出码 3 + INCOMPLETE")
def c22() -> str:
    sbx = Sandbox("c2_2")
    code, text, out = run_pipeline_inproc(
        sbx, MD_OK, name="验收无Chrome", patches={"find_chrome": lambda: None})
    expect(code == 3, f"退出码 {code}，期望 3（无 Chrome 不得判通过）")
    expect(find_report_verdict(text) == "INCOMPLETE",
           f"报告判定码 {find_report_verdict(text)}，期望 INCOMPLETE")
    return "无 Chrome → exit=3/INCOMPLETE"


@check("2.3", "流水线：渲染测量报错 → 退出码 1 + FAIL（不得判通过/未完成）")
def c23() -> str:
    sbx = Sandbox("c2_3")
    code, text, out = run_pipeline_inproc(
        sbx, MD_OK, name="验收渲染故障",
        patches={"render_measure": lambda chrome, body, viewport, timeout_s=60:
                 {"error": "模拟渲染测量失败"}})
    expect(code == 1, f"退出码 {code}，期望 1（渲染失败属于未通过）")
    expect(find_report_verdict(text) == "FAIL",
           f"报告判定码 {find_report_verdict(text)}，期望 FAIL")
    return "渲染报错 → exit=1/FAIL"


@check("2.4", "流水线：真实 Chrome 三档渲染全过 → 退出码 0 + PASS")
def c24() -> str:
    sbx = Sandbox("c2_4")
    pipe = load_pipeline_module(sbx)
    chrome = pipe.find_chrome()
    if not chrome:
        raise SkipCheck("本机未找到 Chrome，无法实测真实渲染")
    code, text, out = run_pipeline_inproc(sbx, MD_OK, name="验收真实渲染")
    expect(code == 0, f"退出码 {code}，期望 0；输出尾部={short(out[-300:], 160)}")
    expect(find_report_verdict(text) == "PASS",
           f"报告判定码 {find_report_verdict(text)}，期望 PASS")
    expect("渲染验收" in text, "报告缺少渲染验收小节")
    return f"真实 Chrome（{Path(chrome).name}）三档渲染 → exit=0/PASS"


# ================================================================ C3 critic 体检表

@check("3.1", "critic：无加粗/无引语的稿 →「可复述」「有证据」不得全是 ✅")
def c31() -> str:
    sbx = Sandbox("c3_1")
    case = new_case_dir(sbx, "99_体检反例_案例")
    write_text(case / "正文.md", MD_BAD)
    res = run_critic(sbx, case)
    reports = sorted(case.glob("体检报告_*.md"))
    expect(reports, "未生成体检报告")
    rows = parse_critic_table(reports[0].read_text(encoding="utf-8"))
    expect(len(rows) >= 3, f"体检表数据行 {len(rows)}，期望 ≥3")
    retell = [r.get("可复述", "") for r in rows]
    evidence = [r.get("有证据", "") for r in rows]
    expect(any(v != "✅" for v in retell),
           f"「可复述」列全是 ✅（反例稿无加粗判断）——恒真列未修：{retell}")
    expect(any(v != "✅" for v in evidence),
           f"「有证据」列全是 ✅（反例稿无引语/时间戳）——恒真列未修：{evidence}")
    return f"可复述={retell} 有证据={evidence}（res={res.status}）"


@check("3.2", "critic：合规稿 →「可复述」「有证据」机器项全 ✅")
def c32() -> str:
    sbx = Sandbox("c3_2")
    case = new_case_dir(sbx, "99_体检正例_案例")
    write_text(case / "正文.md", MD_OK)
    res = run_critic(sbx, case)
    reports = sorted(case.glob("体检报告_*.md"))
    expect(reports, "未生成体检报告")
    rows = parse_critic_table(reports[0].read_text(encoding="utf-8"))
    expect(len(rows) >= 3, f"体检表数据行 {len(rows)}，期望 ≥3")
    retell = [r.get("可复述", "") for r in rows]
    evidence = [r.get("有证据", "") for r in rows]
    expect(all(v == "✅" for v in retell), f"合规稿「可复述」出现非 ✅：{retell}")
    expect(all(v == "✅" for v in evidence), f"合规稿「有证据」出现非 ✅：{evidence}")
    return f"两列全 ✅（{len(rows)} 节；res={res.status}）"


# ================================================================ C4 一 tick 一步

@check("4.1", "tick：提供 --steps 且默认 1")
def c41() -> str:
    sbx = Sandbox("c4_1")
    proc = sbx.run_loop("tick", "--help")
    out = proc.stdout + proc.stderr
    expect(proc.returncode == 0, f"tick --help 退出码 {proc.returncode}")
    expect("--steps" in out, "tick --help 里没有 --steps 参数（T2a 未落实）")
    expect(re.search(r"--steps[^\n]*default[^\n]*1|默认 1|默认1", out) is not None,
           "--steps 帮助文本未标明默认 1")
    return "tick --help 含 --steps（默认 1）"


@check("4.2", "tick：同一 run 连续两次 tick 默认各只前进 1 个阶段")
def c42() -> str:
    sbx = Sandbox("c4_2")
    case = new_case_dir(sbx, "99_一步_案例")
    write_qc_report(case / "质检报告.md", total_line="**通过**", code="PASS")
    sbx.write_run("steprun", stage="composed", case_dir="99_一步_案例",
                  artifacts={"qc_report": "质检报告.md"})
    p1 = sbx.run_loop("tick", "--skip-ingest", "--provider", "stub", "--run-id", "steprun")
    st1 = sbx.read_run("steprun")["stage"]
    expect(p1.returncode == 0, f"第一次 tick 退出码 {p1.returncode}；{short(p1.stderr, 120)}")
    expect(st1 == "qc_passed", f"第一次 tick 后 stage={st1}，期望 qc_passed（只推进 1 步）")
    p2 = sbx.run_loop("tick", "--skip-ingest", "--provider", "stub", "--run-id", "steprun")
    st2 = sbx.read_run("steprun")["stage"]
    expect(p2.returncode == 0, f"第二次 tick 退出码 {p2.returncode}；{short(p2.stderr, 120)}")
    expect(st2 == "awaiting_publish", f"第二次 tick 后 stage={st2}，期望 awaiting_publish")
    return f"composed → {st1} → {st2}（每次 +1）"


# ================================================================ C5 发布不可自动

@check("5.1", "发布：policy 写 publish=true → tick 后仍停在 awaiting_publish")
def c51() -> str:
    sbx = Sandbox("c5_1")
    sbx.write_policy({"auto_approve": {"topic": False, "publish": True},
                      "auto_transcribe": {"enabled": False}})
    sbx.write_run("pubrun", stage="awaiting_publish", case_dir="99_发布_案例",
                  gates={"topic": True, "publish": False})
    proc = sbx.run_loop("tick", "--skip-ingest", "--provider", "stub")
    state = sbx.read_run("pubrun")
    expect(proc.returncode == 0, f"tick 退出码 {proc.returncode}")
    expect(state["stage"] == "awaiting_publish",
           f"tick 后 stage={state['stage']}，期望 awaiting_publish（发布永不自动）")
    expect(state["gates"].get("publish") is False,
           f"tick 后 gates.publish={state['gates'].get('publish')}，期望 False")
    return "policy.publish=true 仍停在 awaiting_publish，gates.publish=false"


@check("5.2", "发布：policy.json 不含可放行发布的死键")
def c52() -> str:
    data = json.loads((REPO / "orchestrator" / "policy.json").read_text(encoding="utf-8"))
    auto = data.get("auto_approve") or {}
    expect("publish" not in auto or auto.get("publish") is False,
           f"policy.auto_approve.publish={auto.get('publish')!r}，应删除或恒 false")
    notes = json.dumps(data.get("notes") or {}, ensure_ascii=False)
    expect(("永不自动" in notes) or ("publish" not in auto),
           "policy notes 未写明发布永不自动")
    return "auto_approve 无 publish 开启键；notes 已说明"


# ================================================================ C6 门禁不可信（读代码 + 行为）

@check("6.1", "门禁代码审读：composer 只调流水线、不自造判定")
def c61() -> str:
    text = (REPO / "orchestrator" / "agents" / "composer.py").read_text(encoding="utf-8")
    expect("gzh_pipeline" in text, "composer 未调用流水线脚本（可能自己拼 HTML/判定）")
    expect("skip-render" in text.lower(),
           "composer 未体现 --skip-render 开关（render=False 时应跳过渲染）")
    expect("总判定" not in text,
           "composer 源码出现「总判定」——疑似自己写判定（应只信流水线报告）")
    return "composer 调 gzh_pipeline + 传 --skip-render，无自造判定"


@check("6.2", "门禁代码审读：gatekeeper 使用统一判定 + INCOMPLETE→blocked")
def c62() -> str:
    text = (REPO / "orchestrator" / "agents" / "gatekeeper.py").read_text(encoding="utf-8")
    expect(("parse_report" in text) or ("from ..verdict" in text) or ("from orchestrator.verdict" in text),
           "gatekeeper 未复用 orchestrator/verdict.py（判定字符串仍各自手写）")
    expect("INCOMPLETE" in text, "gatekeeper 未处理 INCOMPLETE 分支")
    sbx = Sandbox("c6_2")
    report = write_qc_report(TMPROOT / "c6_2" / "qc.md",
                             total_line="**未完成（渲染验收未执行）**", code="INCOMPLETE")
    res = run_gatekeeper(report, sbx=sbx)
    expect(res.status == "blocked", f"INCOMPLETE 报告返回 {res.status}，期望 blocked")
    return "复用 verdict.parse_report；INCOMPLETE → blocked"


@check("6.3", "门禁串联：真实 --skip-render 报告 → gatekeeper 判 blocked")
def c63() -> str:
    sbx = Sandbox("c6_3")
    outdir = TMPROOT / "c6_3" / "out"
    outdir.mkdir(parents=True)
    md = write_text(TMPROOT / "c6_3" / "正文.md", MD_OK)
    proc = sbx.run_pipeline_cli(md, "验收串联", outdir, "--skip-render")
    report = outdir / "验收串联_质检报告.md"
    expect(report.exists(), "流水线未生成报告")
    expect(proc.returncode == 3, f"流水线退出码 {proc.returncode}，期望 3")
    res = run_gatekeeper(report, sbx=sbx)
    expect(res.status == "blocked",
           f"gatekeeper 对真实 --skip-render 报告返回 {res.status}（{res.reason or res.note}），期望 blocked")
    return "skip-render 报告 → blocked（不会假通过）"


# ================================================================ C7 版本化

@check("7.1", "版本化：composer 连跑两次 → v1、v2 排版产物都在")
def c71() -> str:
    sbx = Sandbox("c7_1")
    case = new_case_dir(sbx, "99_排版_案例")
    write_text(case / "正文.md", MD_OK)
    guest = live_import("orchestrator.agents.base").guest_of(case)
    r1 = run_composer(sbx, case, draft_rel="正文.md")
    expect(r1.status == "ok", f"第一次 composer 返回 {r1.status}（{r1.reason or r1.note}）")
    r2 = run_composer(sbx, case, draft_rel="正文.md")
    expect(r2.status == "ok", f"第二次 composer 返回 {r2.status}（{r2.reason or r2.note}）")
    v1 = case / f"{guest}_v1_排版预览.html"
    v2 = case / f"{guest}_v2_排版预览.html"
    expect(v1.exists() and v2.exists(),
           f"期望 v1/v2 都在；实际={sorted(p.name for p in case.iterdir())}")
    expect((case / f"{guest}_v1_质检报告.md").exists(),
           "v1 质检报告被覆盖/丢失")
    return f"{guest}_v1/v2 排版预览+报告均在"


@check("7.2", "版本化：writer 重跑 → _vN 递增且不覆盖旧稿")
def c72() -> str:
    sbx = Sandbox("c7_2")
    case = new_case_dir(sbx, "99_写稿_案例")
    provider = live_import("orchestrator.providers.base").get_provider("stub")
    guest = live_import("orchestrator.agents.base").guest_of(case)
    r1 = run_writer(sbx, case, provider)
    v1 = case / f"{guest}_人味版_v1.md"
    expect(r1.status == "ok", f"第一次 writer 返回 {r1.status}")
    expect(v1.exists(), f"未生成 {v1.name}；实际={sorted(p.name for p in case.iterdir())}")
    before = v1.read_text(encoding="utf-8")
    r2 = run_writer(sbx, case, provider)
    v2 = case / f"{guest}_人味版_v2.md"
    expect(r2.status == "ok", f"第二次 writer 返回 {r2.status}")
    expect(v2.exists(), f"未生成 {v2.name}；实际={sorted(p.name for p in case.iterdir())}")
    expect(v1.read_text(encoding="utf-8") == before, "第二次 writer 覆盖了 v1 旧稿")
    draft = r2.artifacts.get("draft", "")
    expect(draft.endswith("_v2.md"), f"artifacts.draft={draft!r}，期望指向 _v2.md")
    return f"{guest}_人味版_v1/v2 均在，v1 未被覆盖"


# ================================================================ C8 artifacts 相对路径

@check("8.1", "相对路径：composer 读 artifacts.draft（相对 case_dir）")
def c81() -> str:
    sbx = Sandbox("c8_1")
    case = new_case_dir(sbx, "99_相对_案例")
    write_text(case / "正文.md", MD_OK)
    guest = live_import("orchestrator.agents.base").guest_of(case)
    res = run_composer(sbx, case, draft_rel="正文.md")
    expect(res.status == "ok", f"composer 返回 {res.status}（{res.reason or res.note}）——相对 draft 读不到")
    expect((case / f"{guest}_v1_排版预览.html").exists(), "未生成排版预览")
    return "artifacts.draft=相对路径 → composer ok"


@check("8.2", "相对路径：critic 读 artifacts.draft（相对 case_dir）")
def c82() -> str:
    sbx = Sandbox("c8_2")
    case = new_case_dir(sbx, "99_相对体检_案例")
    write_text(case / "正文.md", MD_OK)
    res = run_critic(sbx, case)
    expect(res.status != "blocked", f"critic 返回 blocked（{res.reason or res.note}）——相对 draft 读不到")
    expect(sorted(case.glob("体检报告_*.md")), "未生成体检报告")
    return f"artifacts.draft=相对路径 → critic {res.status}"


@check("8.3", "相对路径：失效的 /home/runner 绝对路径按文件名回退（composer+critic）")
def c83() -> str:
    sbx = Sandbox("c8_3")
    case = new_case_dir(sbx, "99_回退_案例")
    write_text(case / "正文.md", MD_OK)
    foreign = f"/home/runner/work/gongzhonghao/cases/{case.name}/正文.md"
    res_c = run_critic(sbx, case, draft_value=foreign)
    expect(res_c.status != "blocked",
           f"critic 对失效绝对路径返回 blocked（{res_c.reason or res_c.note}）——未按文件名回退")
    res_p = run_composer(sbx, case, draft_value=foreign)
    expect(res_p.status == "ok",
           f"composer 对失效绝对路径返回 {res_p.status}（{res_p.reason or res_p.note}）——未按文件名回退")
    return f"critic={res_c.status}，composer={res_p.status}（均按文件名回退）"


@check("8.4", "相对路径：composer 写回的 artifacts 一律相对 case_dir")
def c84() -> str:
    sbx = Sandbox("c8_4")
    case = new_case_dir(sbx, "99_相对产物_案例")
    write_text(case / "正文.md", MD_OK)
    res = run_composer(sbx, case, draft_rel="正文.md")
    expect(res.status == "ok", f"composer 返回 {res.status}")
    arts = dict(res.artifacts or {})
    expect(arts, "composer 未登记任何 artifacts")
    absolute = {k: v for k, v in arts.items() if Path(str(v)).is_absolute()}
    expect(not absolute, f"artifacts 仍是绝对路径（T3c 未落实）：{absolute}")
    missing = {k: v for k, v in arts.items() if not (case / v).exists()}
    expect(not missing, f"artifacts 相对路径在本机解析不到：{missing}")
    return f"artifacts={ {k: str(v) for k, v in arts.items()} }"


# ================================================================ C9 坏状态与并发锁

@check("9.1", "坏 state.json：status 告警 + 退出码 1 + 不拖累其他 run")
def c91() -> str:
    sbx = Sandbox("c9_1")
    sbx.write_run("goodrun", stage="archived", status="done", case_dir="99_好_案例")
    bad = sbx.runs / "badrun"
    bad.mkdir(parents=True)
    write_text(bad / "state.json", '{"run_id": "badrun", ')   # 坏 JSON
    proc = sbx.run_loop("status")
    out = proc.stdout + proc.stderr
    expect(proc.returncode == 1,
           f"status 退出码 {proc.returncode}，期望 1（坏 state 应让 CI 可感知）")
    expect("[warn]" in out and "state.json" in out,
           f"status 未打印坏 state.json 告警；输出={short(out, 160)}")
    expect("goodrun" in out, "可读 run 未列出（坏文件拖累了其他 run）")
    return "退出码=1，告警与正常 run 同时可见"


@check("9.2", "坏 state.json：tick 告警但不阻塞（退出码 0，summary 标 broken）")
def c92() -> str:
    sbx = Sandbox("c9_2")
    sbx.write_run("goodrun", stage="archived", status="done", case_dir="99_好_案例")
    bad = sbx.runs / "badrun"
    bad.mkdir(parents=True)
    write_text(bad / "state.json", "not-a-json")
    proc = sbx.run_loop("tick", "--skip-ingest", "--provider", "stub")
    out = proc.stdout + proc.stderr
    expect(proc.returncode == 0, f"tick 退出码 {proc.returncode}，期望 0（坏文件不能拖垮定时任务）")
    expect("[warn]" in out and "state.json" in out,
           f"tick 未打印坏 state 告警；输出={short(out, 160)}")
    expect("broken=1" in out, f"tick summary 未标注 broken 数；输出尾部={short(out[-200:], 160)}")
    return "tick 退出码 0，告警 + broken=1 可见"


@check("9.3", "并发锁：另一个进程占锁时第二个 tick 直接退出（提示 + 退出码 0）")
def c93() -> str:
    sbx = Sandbox("c9_3")
    sbx.runs.mkdir(parents=True, exist_ok=True)
    lock = sbx.runs / ".lock"
    holder = (
        "import fcntl,sys,time;"
        "f=open(sys.argv[1],'a+');"
        "fcntl.flock(f.fileno(), fcntl.LOCK_EX);"
        "print('locked', flush=True);"
        "time.sleep(20)"
    )
    helper = subprocess.Popen([sys.executable, "-c", holder, str(lock)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready, _, _ = select.select([helper.stdout], [], [], 15)
        if not ready:
            raise SkipCheck("辅助进程未在 15s 内拿到 flock（本机 fcntl 行为异常）")
        line = (helper.stdout.readline() or "").strip()
        if line != "locked":
            raise SkipCheck(f"辅助进程未拿到锁：{line!r}")
        proc = sbx.run_loop("tick", "--skip-ingest", "--provider", "stub", timeout=60)
        out = proc.stdout + proc.stderr
        expect(proc.returncode == 0, f"第二个 tick 退出码 {proc.returncode}，期望 0（跳过而非失败）")
        expect(("另一个 tick" in out) or ("正在运行" in out),
               f"第二个 tick 未提示锁被占用；输出={short(out, 160)}")
        return "占锁时第二个 tick 提示「另一个 tick 正在运行」且退出码 0"
    finally:
        helper.terminate()
        try:
            helper.wait(timeout=5)
        except Exception:  # noqa: BLE001
            helper.kill()


# ================================================================ C10 闸门通知

class _CaptureHandler(BaseHTTPRequestHandler):
    """本地 mock webhook：记录收到的 POST body。"""
    bodies: list[str] = []

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        type(self).bodies.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"code": 0, "msg": "ok"}')

    def log_message(self, *args):  # noqa: ANN002 - 静默
        pass


def _mock_webhook():
    _CaptureHandler.bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/hook"


@check("10.1", "通知：未配置 FEISHU_WEBHOOK 时 tick 正常（退出码 0、不报错）")
def c101() -> str:
    sbx = Sandbox("c10_1")
    sbx.write_run("notifyrun", stage="awaiting_publish", case_dir="99_通知_案例",
                  gates={"topic": True, "publish": False})
    proc = sbx.run_loop("tick", "--skip-ingest", "--provider", "stub")
    out = proc.stdout + proc.stderr
    expect(proc.returncode == 0, f"tick 退出码 {proc.returncode}，期望 0")
    expect("Traceback" not in out, "未配 webhook 时 tick 抛异常")
    return "无 webhook → tick 退出码 0，无异常"


@check("10.2", "通知：mock webhook 收到一条，且正文含 run_id")
def c102() -> str:
    sbx = Sandbox("c10_2")
    sbx.write_run("notifyrun1", stage="awaiting_publish", case_dir="99_通知_案例",
                  gates={"topic": True, "publish": False})
    server, url = _mock_webhook()
    try:
        proc = sbx.run_loop("tick", "--skip-ingest", "--provider", "stub",
                            extra_env={"FEISHU_WEBHOOK": url, "GZH_FEISHU_WEBHOOK": url})
        expect(proc.returncode == 0, f"tick 退出码 {proc.returncode}；输出={short(proc.stdout + proc.stderr, 140)}")
        expect(len(_CaptureHandler.bodies) == 1,
               f"webhook 收到 {len(_CaptureHandler.bodies)} 条，期望 1 条")
        body = _CaptureHandler.bodies[0]
        expect("notifyrun1" in body, "通知正文不含 run_id")
        expect("msg_type" in body, f"通知 body 不是飞书文本消息格式：{short(body, 160)}")
        return "mock webhook 收到 1 条，含 run_id + msg_type"
    finally:
        server.shutdown()
        server.server_close()


@check("10.3", "通知：12h 内同一签名不重复推送")
def c103() -> str:
    sbx = Sandbox("c10_3")
    sbx.write_run("notifyrun2", stage="awaiting_publish", case_dir="99_通知_案例",
                  gates={"topic": True, "publish": False})
    server, url = _mock_webhook()
    try:
        env = {"FEISHU_WEBHOOK": url, "GZH_FEISHU_WEBHOOK": url}
        p1 = sbx.run_loop("tick", "--skip-ingest", "--provider", "stub", extra_env=env)
        p2 = sbx.run_loop("tick", "--skip-ingest", "--provider", "stub", extra_env=env)
        expect(p1.returncode == 0 and p2.returncode == 0,
               f"tick 退出码 {p1.returncode}/{p2.returncode}")
        expect(len(_CaptureHandler.bodies) == 1,
               f"两次 tick 收到 {len(_CaptureHandler.bodies)} 条通知，期望 1 条（12h 去重）")
        return "连续两次 tick 只推 1 条（去重生效）"
    finally:
        server.shutdown()
        server.server_close()


# ================================================================ C11 附加（T2b/T5b/T5c）

@check("11.1", "UTC：state.now() 返回 +00:00（T2b）")
def c111() -> str:
    state = live_import("orchestrator.state")
    stamp = state.now()
    dt = datetime.fromisoformat(stamp)
    expect(dt.utcoffset() is not None and dt.utcoffset().total_seconds() == 0,
           f"now()={stamp!r} 不是 UTC（offset={dt.utcoffset()}）")
    return f"now()={stamp}"


@check("11.2", "成本：status --json 含 usage 汇总（T5b）")
def c112() -> str:
    sbx = Sandbox("c11_2")
    sbx.write_run("usagerun", stage="archived", status="done", case_dir="99_成本_案例",
                  usage={"input": 100, "output": 20, "total": 120, "calls": 1})
    proc = sbx.run_loop("status", "--json")
    expect(proc.returncode == 0, f"status --json 退出码 {proc.returncode}；{short(proc.stderr, 120)}")
    payload = json.loads(proc.stdout)
    expect("usage" in payload, f"status --json 无 usage 字段；顶层键={list(payload)}")
    today = (payload.get("usage") or {}).get("today") or {}
    expect(int(today.get("total", 0) or 0) == 120,
           f"今日 tokens={today.get('total')!r}，期望 120；payload={short(json.dumps(payload, ensure_ascii=False), 200)}")
    return "status --json 含 usage，今日 total=120"


@check("11.3", "配置：policy.json 死配置已清理（T5c）")
def c113() -> str:
    data = json.loads((REPO / "orchestrator" / "policy.json").read_text(encoding="utf-8"))
    auto = data.get("auto_transcribe") or {}
    dead = {k: auto[k] for k in ("source", "max_minutes") if k in auto}
    expect(not dead, f"auto_transcribe 仍有死配置：{dead}")
    return "auto_transcribe 只保留 enabled"


# ================================================================ 入口

def main(argv: list[str] | None = None) -> int:
    global VERBOSE, TMPROOT
    parser = argparse.ArgumentParser(description="P0 独立验收脚本（临时目录内跑对抗场景）")
    parser.add_argument("--keep", action="store_true", help="保留临时目录（默认跑完清理）")
    parser.add_argument("--only", default="", help="只跑编号前缀匹配的检查，如 1,2,4")
    parser.add_argument("--list", action="store_true", help="只列检查项")
    parser.add_argument("--verbose", action="store_true", help="异常时打印 traceback")
    args = parser.parse_args(argv)
    VERBOSE = args.verbose

    if args.list:
        for cid, title, _ in CHECKS:
            print(f"{cid:5s} {title}")
        shutil.rmtree(TMPROOT, ignore_errors=True)
        return 0

    prefixes = [p.strip() for p in args.only.split(",") if p.strip()]

    def selected(cid: str) -> bool:
        return (not prefixes) or any(cid == p or cid.startswith(p + ".") for p in prefixes)

    print(f"P0 独立验收 · 仓库={REPO}")
    print(f"临时目录={TMPROOT}（检查项 {len(CHECKS)}，本机 Python {sys.version.split()[0]}）")
    print("-" * 100)
    started = time.time()
    for cid, title, fn in CHECKS:
        if not selected(cid):
            continue
        t0 = time.time()
        try:
            detail = fn() or ""
            status = "PASS"
        except SkipCheck as exc:
            status, detail = "SKIP", str(exc)
        except CheckFail as exc:
            status, detail = "FAIL", str(exc)
        except Exception as exc:  # noqa: BLE001 - 半成品代码不能把验收脚本打死
            status, detail = "FAIL", f"异常 {type(exc).__name__}: {exc}"
            if VERBOSE:
                traceback.print_exc()
        elapsed = time.time() - t0
        RESULTS.append((cid, title, status, detail))
        print(f"[{status:4s}] {cid:4s} {title}  ({elapsed:.1f}s)")
        if status != "PASS":
            print(f"         └─ {short(detail, 300)}")
    elapsed = time.time() - started

    print("-" * 100)
    n_pass = sum(1 for r in RESULTS if r[2] == "PASS")
    n_fail = sum(1 for r in RESULTS if r[2] == "FAIL")
    n_skip = sum(1 for r in RESULTS if r[2] == "SKIP")
    print(f"合计 {len(RESULTS)} 项：PASS {n_pass} / FAIL {n_fail} / SKIP {n_skip}，用时 {elapsed:.1f}s")
    if n_fail:
        print("\n未通过项（需报告 Lead，由原 owner 修）：")
        for cid, title, status, detail in RESULTS:
            if status == "FAIL":
                print(f"  - {cid} {title}\n      {short(detail, 400)}")
    if n_skip:
        print("\n未实测项（本机条件不满足，报告中须诚实标注）：")
        for cid, title, status, detail in RESULTS:
            if status == "SKIP":
                print(f"  - {cid} {title}：{short(detail, 200)}")

    if args.keep:
        print(f"\n现场保留：{TMPROOT}")
    else:
        shutil.rmtree(TMPROOT, ignore_errors=True)
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
