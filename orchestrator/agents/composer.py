#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""composer：排版（对应 SOP v8 §07）。

硬规则：排版轮只改版式，不改文字。因此本 agent **不生成 HTML**，
只调用流水线 `gzh_pipeline.py`，把正文 md 变成双 HTML + 渲染图。
自己拼 HTML 会绕过门禁，属于违规。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .base import Agent, Context, Result, guest_of


class Composer(Agent):
    name = "composer"
    stage = "critiqued"

    def run(self, ctx: Context) -> Result:
        draft = ctx.run.artifacts.get("draft", "")
        md = Path(draft) if draft else None
        if not md or not md.exists():
            return Result.blocked("找不到正文 md，无法排版")

        pipeline = ctx.orch_dir.parent / "流水线" / "gzh_pipeline.py"
        if not pipeline.exists():
            return Result.blocked(f"流水线不存在：{pipeline}")

        guest = guest_of(ctx.case_dir)
        name = f"{guest}_v1"

        cmd = [sys.executable, str(pipeline),
               "--md", str(md),
               "--name", name,
               "--outdir", str(ctx.case_dir)]
        if not ctx.render:
            cmd.append("--skip-render")

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900, cwd=str(ctx.case_dir))
        except subprocess.TimeoutExpired:
            return Result.rework("流水线超时（900s）")

        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
            return Result.rework("流水线 FAIL：" + " / ".join(tail))

        qc = ctx.case_dir / f"{name}_质检报告.md"
        arts = {
            "preview": str(ctx.case_dir / f"{name}_排版预览.html"),
            "import": str(ctx.case_dir / f"{name}_导入片段.html"),
            "qc_report": str(qc),
        }
        return Result.ok(f"排版完成：{name}（render={'on' if ctx.render else 'off'}）",
                         artifacts={k: v for k, v in arts.items() if Path(v).exists()},
                         to="composed")
