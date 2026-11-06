#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""状态机与持久化：orchestrator 的唯一真相。

设计约束（见 SOP v9 §04）：
- 状态文件是唯一真相，不存在的 run 视为 new。
- 非法状态转移一律抛错，不允许"顺手往前推一步"。
- 每次转移追加 history 与 events.log，作为审计日志。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 状态定义

# 正常推进顺序
STAGES = [
    "new",
    "triaged",
    "transcribed",
    "ledgered",
    "researched",
    "drafted",
    "critiqued",
    "composed",
    "qc_passed",
    "awaiting_publish",
    "published",
    "archived",
]

# 允许的转移：正向 + 打回
ALLOWED: dict[str, set[str]] = {
    "new": {"triaged"},
    "triaged": {"transcribed"},
    "transcribed": {"ledgered"},
    "ledgered": {"researched"},
    "researched": {"drafted"},
    "drafted": {"critiqued", "drafted"},          # 打回：critic 不过 → 重写
    "critiqued": {"composed", "drafted"},         # 打回：内容不过 → 回 writer
    "composed": {"qc_passed", "composed"},        # 打回：门禁 FAIL → 重排
    "qc_passed": {"awaiting_publish", "composed"},
    "awaiting_publish": {"published"},            # 只能人工放行
    "published": {"archived"},
    "archived": set(),
}

# 需要人工点头的闸门
GATES: dict[str, str] = {
    "triaged": "topic",           # 选题定调
    "awaiting_publish": "publish",  # 发布拍板
}

# 该阶段由哪个 agent 执行
STAGE_AGENT: dict[str, str] = {
    "new": "scout",
    "triaged": "transcriber",
    "transcribed": "ledger",
    "ledgered": "researcher",
    "researched": "writer",
    "drafted": "critic",
    "critiqued": "composer",
    "composed": "gatekeeper",
    "qc_passed": "gatekeeper",
    "awaiting_publish": "archivist",
    "published": "archivist",
}

# 打回：允许回退到的目标（与正向推进分开，避免把"回退"混进"前进"）
REWIND: dict[str, set[str]] = {
    "drafted": {"researched"},     # critic 不过 → 回 writer 重写
    "critiqued": {"researched"},   # 内容层面不过 → 回 writer
    "composed": {"critiqued"},     # 门禁 FAIL → 回 composer 重排
    "qc_passed": {"critiqued"},
}

MAX_REWORK = 3
MAX_ERRORS = 3


class IllegalTransition(RuntimeError):
    """非法状态转移。"""


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 数据模型


@dataclass
class Source:
    kind: str = "inbox"          # inbox | rss | cli
    url: str = ""
    title: str = ""
    channel: str = ""
    notes: str = ""
    published_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Run:
    run_id: str
    stage: str = "new"
    status: str = "active"        # active | blocked | stale | done
    source: Source = field(default_factory=Source)
    case_dir: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    rework: dict[str, Any] = field(default_factory=lambda: {"count": 0, "last_reason": ""})
    gates: dict[str, bool] = field(default_factory=lambda: {"topic": False, "publish": False})
    errors: int = 0
    error: str | None = None
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)

    # -------------------------------------------------- 序列化
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["source"] = self.source.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Run":
        src = d.get("source") or {}
        d = {k: v for k, v in d.items() if k != "source"}
        d.setdefault("gates", {"topic": False, "publish": False})
        return cls(source=Source(**{k: src.get(k, "") for k in
                                    ("kind", "url", "title", "channel", "notes", "published_at")}), **d)

    # -------------------------------------------------- 转移
    def can(self, to: str) -> bool:
        return to in ALLOWED.get(self.stage, set())

    def advance(self, to: str, agent: str, note: str = "", artifacts: dict[str, str] | None = None) -> None:
        if not self.can(to):
            raise IllegalTransition(
                f"{self.run_id}: 不允许 {self.stage} → {to}（允许：{sorted(ALLOWED.get(self.stage, []))}）"
            )
        frm = self.stage
        self.stage = to
        self.updated_at = now()
        self.error = None
        self.errors = 0
        if artifacts:
            self.artifacts.update(artifacts)
        self.history.append({"at": self.updated_at, "from": frm, "to": to,
                             "agent": agent, "note": note})
        if to == "archived":
            self.status = "done"

    def rewind(self, to: str, agent: str, reason: str) -> None:
        """打回：记录原因并回退状态。超过上限则转 blocked。"""
        if to not in REWIND.get(self.stage, set()):
            raise IllegalTransition(
                f"{self.run_id}: {self.stage} 不允许打回 {to}（允许：{sorted(REWIND.get(self.stage, []))}）"
            )
        frm = self.stage
        self.record_rework(agent, reason)
        if self.status == "blocked":
            return
        self.stage = to
        self.history[-1]["to"] = to
        self.history[-1]["from"] = frm

    def record_rework(self, agent: str, reason: str) -> None:
        self.rework["count"] = int(self.rework.get("count", 0)) + 1
        self.rework["last_reason"] = reason
        self.updated_at = now()
        self.history.append({"at": self.updated_at, "from": self.stage, "to": self.stage,
                             "agent": agent, "note": f"rework: {reason}"})
        if self.rework["count"] > MAX_REWORK:
            self.status = "blocked"
            self.error = f"打回超过 {MAX_REWORK} 次：{reason}"

    def record_error(self, agent: str, exc: BaseException) -> None:
        self.errors += 1
        self.error = f"{type(exc).__name__}: {exc}"
        self.updated_at = now()
        self.history.append({"at": self.updated_at, "from": self.stage, "to": self.stage,
                             "agent": agent, "note": f"error: {self.error}"})
        if self.errors >= MAX_ERRORS:
            self.status = "blocked"

    @property
    def waiting_gate(self) -> str | None:
        """未通过的人工闸门名；已通过返回 None（允许 agent 继续）。"""
        gate = GATES.get(self.stage)
        if gate and not self.gates.get(gate, False):
            return gate
        return None

    @property
    def next_agent(self) -> str | None:
        return STAGE_AGENT.get(self.stage)


# ---------------------------------------------------------------- 仓库


class RunStore:
    """runs/ 目录的读写。一个 run 一个子目录。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def dir(self, run_id: str) -> Path:
        return self.root / run_id

    def state_path(self, run_id: str) -> Path:
        return self.dir(run_id) / "state.json"

    def log_path(self, run_id: str) -> Path:
        return self.dir(run_id) / "events.log"

    def exists(self, run_id: str) -> bool:
        return self.state_path(run_id).exists()

    def get(self, run_id: str) -> Run | None:
        p = self.state_path(run_id)
        if not p.exists():
            return None
        return Run.from_dict(json.loads(p.read_text(encoding="utf-8")))

    def all(self) -> list[Run]:
        runs = []
        for p in sorted(self.root.glob("*/state.json")):
            try:
                runs.append(Run.from_dict(json.loads(p.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, TypeError):
                continue
        return runs

    def save(self, run: Run, line: str = "") -> None:
        d = self.dir(run.run_id)
        d.mkdir(parents=True, exist_ok=True)
        self.state_path(run.run_id).write_text(
            json.dumps(run.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        if line:
            with self.log_path(run.run_id).open("a", encoding="utf-8") as fh:
                fh.write(f"{now()}\t{line}\n")

    def create(self, run_id: str, source: Source, case_dir: str) -> Run:
        run = Run(run_id=run_id, source=source, case_dir=case_dir)
        self.save(run, f"create\t{source.kind}\t{source.url}")
        return run


# ---------------------------------------------------------------- id


def normalize_url(url: str) -> str:
    """去掉追踪参数与尾斜杠；youtu.be 归一为 watch?v=。"""
    u = (url or "").strip()
    # 任意 YouTube 形态统一成 watch?v=ID（youtu.be、/live/、/shorts/、/embed/、?v=）
    if "youtube.com" in u or "youtu.be" in u:
        m = re.search(r"(?:youtu\.be/|[?&]v=|/(?:live|shorts|embed|v)/)([\w-]{6,})", u)
        if m:
            return f"https://www.youtube.com/watch?v={m.group(1)}"
    u = re.sub(r"[?&](utm_[^=&]*|si|feature|t)=[^&#]*", "", u)
    u = re.sub(r"[?&]+$", "", u)
    if u.endswith("/"):
        u = u[:-1]
    return u


def run_id_for(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()[:12]
