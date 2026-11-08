#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""composer：排版（对应 SOP v8 §07）。

硬规则：排版轮只改版式，不改文字。因此本 agent **不生成 HTML**，
只调用流水线 `gzh_pipeline.py`，把正文 md 变成双 HTML + 渲染图。
自己拼 HTML 会绕过门禁，属于违规。

版本化（T3a）：产物名扫描 `{guest}_v*_排版预览.html` 取 max+1，重跑得 v2/v3…，
绝不覆盖上一版。artifacts 存**相对 case_dir** 的 posix 路径（T3c），
云端产物拉回本地还能继续跑。

退出码分层：流水线 3 = 产物已生成但渲染未完成 → 这里返回 ok（事实层），
放不放行由 gatekeeper 按 `判定码：INCOMPLETE` 判（门禁层）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from ..versions import next_version
from .base import Agent, Context, Result, guest_of

# 单个产物在 case_dir 下的相对路径模板
ARTIFACT_SUFFIX = {
    "preview": "_排版预览.html",
    "import": "_导入片段.html",
    "qc_report": "_质检报告.md",
}


def _rel_posix(case_dir: Path, path: Path) -> str:
    """artifacts 契约：相对 case_dir 的 posix 路径；不在目录内才退回绝对路径。"""
    try:
        return path.relative_to(case_dir).as_posix()
    except ValueError:  # pragma: no cover - 正常流程不会走到
        return path.as_posix()


class Composer(Agent):
    name = "composer"
    stage = "critiqued"

    def run(self, ctx: Context) -> Result:
        draft = ctx.artifact_path("draft")
        if not draft or not draft.exists():
            return Result.blocked("找不到正文 md，无法排版")

        pipeline = ctx.orch_dir.parent / "流水线" / "gzh_pipeline.py"
        if not pipeline.exists():
            return Result.blocked(f"流水线不存在：{pipeline}")

        guest = guest_of(ctx.case_dir)
        # 扫已存在的 {guest}_vN_排版预览.html，取 max+1（v1 起）
        version = next_version(ctx.case_dir, rf"{re.escape(guest)}_v(\d+)_排版预览\.html")
        name = f"{guest}_v{version}"

        cmd = [sys.executable, str(pipeline),
               "--md", str(draft),
               "--name", name,
               "--outdir", str(ctx.case_dir)]
        if not ctx.render:
            cmd.append("--skip-render")

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900, cwd=str(ctx.case_dir))
        except subprocess.TimeoutExpired:
            return Result.rework("流水线超时（900s）")

        arts = self._artifacts(ctx, name)

        # 3 = 产物已生成但渲染验收未完成。本层只报事实，拦截留给 gatekeeper。
        if proc.returncode == 3:
            return Result.ok(f"排版完成：{name}（渲染未完成，待 gatekeeper 判定）",
                             artifacts=arts, to="composed")

        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
            if proc.returncode == 1:
                return Result.rework("流水线 FAIL：" + " / ".join(tail))
            return Result.rework(f"流水线异常退出（{proc.returncode}）：" + " / ".join(tail))

        return Result.ok(f"排版完成：{name}（render={'on' if ctx.render else 'off'}）",
                         artifacts=arts, to="composed")

    @staticmethod
    def _artifacts(ctx: Context, name: str) -> dict[str, str]:
        """只登记真实存在的产物；值一律是相对 case_dir 的 posix 路径。"""
        arts: dict[str, str] = {}
        for key, suffix in ARTIFACT_SUFFIX.items():
            path = ctx.case_dir / f"{name}{suffix}"
            if path.exists():
                arts[key] = _rel_posix(ctx.case_dir, path)
        return arts
