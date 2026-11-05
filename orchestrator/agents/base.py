#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 协议：一个阶段 = 一个 agent + 一份产物 + 一组判据。

返回约定：
  ok      通过，可进入下一状态
  rework  打回，携带具体原因（不自动改稿）
  blocked 需要人工介入
  skip    本轮无事可做（幂等/缺前置条件），不推进状态
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..providers.base import Provider
from ..state import Run


@dataclass
class Context:
    run: Run
    case_dir: Path
    orch_dir: Path
    provider: Provider
    dry_run: bool = False
    render: bool = False

    def artifact_dir(self) -> Path:
        """LLM 原始输出留档目录（不进案例交付物）。"""
        d = self.orch_dir / "runs" / self.run.run_id / "llm"
        if not self.dry_run:
            d.mkdir(parents=True, exist_ok=True)
        return d

    def save_llm(self, stage: str, text: str) -> None:
        if self.dry_run:
            return
        (self.artifact_dir() / f"{stage}.txt").write_text(text, encoding="utf-8")


@dataclass
class Result:
    status: str = "ok"                 # ok | rework | blocked | skip
    note: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)
    to: str | None = None              # 显式目标状态，None 表示由 loop 取默认下一步
    reason: str = ""                   # rework/blocked 的具体原因

    @classmethod
    def ok(cls, note: str = "", artifacts: dict[str, str] | None = None, to: str | None = None):
        return cls(status="ok", note=note, artifacts=artifacts or {}, to=to)

    @classmethod
    def rework(cls, reason: str):
        return cls(status="rework", reason=reason, note=reason)

    @classmethod
    def blocked(cls, reason: str):
        return cls(status="blocked", reason=reason, note=reason)

    @classmethod
    def skip(cls, note: str):
        return cls(status="skip", note=note)


def guest_of(case_dir: Path) -> str:
    """从 `NN_嘉宾_案例` 里取出嘉宾名。"""
    m = re.match(r"^\d{2,3}_(.+?)(?:_案例)?$", case_dir.name)
    return (m.group(1) if m else case_dir.name) or "未命名"


class Agent:
    name: str = "agent"
    stage: str = "new"        # 处理哪个 stage 的 run

    def run(self, ctx: Context) -> Result:  # pragma: no cover - 接口
        raise NotImplementedError

    # 小工具：安全写文件（dry-run 时不写）
    @staticmethod
    def write(ctx: Context, path: Path, text: str) -> str:
        if not ctx.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return str(path)
