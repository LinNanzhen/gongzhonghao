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

    def media_path(self) -> Path | None:
        """定位本期原视频（配图/封面抽帧用）。

        解析顺序：
        1. `case_dir/媒体路径.txt` 第一行（oneshot 建 run 时写入，绝对或相对路径）；
        2. case_dir 下最大的 .mp4/.mov/.mkv/.m4a/.wav；
        3. 环境变量 `GZH_MEDIA_DIR` 里与案例名/嘉宾名匹配的媒体文件。
        找不到返回 None——配图环节应降级跳过，而不是 blocked。
        """
        import os

        marker = self.case_dir / "媒体路径.txt"
        if marker.exists():
            first = marker.read_text(encoding="utf-8", errors="replace").splitlines()
            if first and first[0].strip():
                p = Path(first[0].strip()).expanduser()
                if not p.is_absolute():
                    p = self.case_dir / p
                if p.exists() and p.is_file():
                    return p

        media_exts = {".mp4", ".mov", ".mkv", ".m4a", ".wav", ".flac", ".webm"}
        cands = [p for p in self.case_dir.rglob("*") if p.is_file() and p.suffix.lower() in media_exts]
        if cands:
            return max(cands, key=lambda p: p.stat().st_size)

        media_dir = os.environ.get("GZH_MEDIA_DIR")
        if media_dir:
            mdir = Path(media_dir)
            if mdir.is_dir():
                cands = [p for p in mdir.iterdir() if p.is_file() and p.suffix.lower() in media_exts]
                if cands:
                    return max(cands, key=lambda p: p.stat().st_size)
        return None

    def artifact_path(self, key: str) -> Path | None:
        """把 `run.artifacts[key]` 解析成本机路径。

        新契约（T3c）：artifacts 存**相对 case_dir** 的路径，云端产物拉回本地
        也能继续跑；同时兼容早期写入的绝对路径，以及云端 `/home/runner/...`
        这类在本机不存在的绝对路径（按文件名落到当前案例目录）。
        """
        raw = self.run.artifacts.get(key)
        if not raw:
            return None
        p = Path(raw)
        if not p.is_absolute():
            return self.case_dir / p
        return p if p.exists() else self.case_dir / p.name


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


def gather_context(case_dir: Path, prefixes: tuple[str, ...] = (),
                   per_file: int = 4000, total: int = 12000) -> str:
    """把案例里的 md 读成文本塞进 prompt。

    模型通过 API 调用，**没有文件系统**——只写"文件在 X 目录"它读不到。
    """
    parts: list[str] = []
    for prefix in prefixes:
        for path in sorted(case_dir.glob(f"{prefix}*.md")):
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if len(body) > per_file:
                body = body[:per_file] + f"\n…（已截断，原文 {len(body)} 字）"
            parts.append(f"### {path.name}\n{body}")
    text = "\n\n".join(parts)
    if len(text) > total:
        text = text[:total] + f"\n…（上下文已截断，共 {len(text)} 字）"
    return text


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
