#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gatekeeper：门禁与发布前验收（对应 SOP v8 §09）。

两件事：
1. 读流水线质检报告，FAIL → 打回 composer；
2. 全过 → qc_passed，再推进到 awaiting_publish（人工闸门，必须停）。

绝不自动发布。没有 --force-publish 这种东西。
"""
from __future__ import annotations

import re
from pathlib import Path

from .base import Agent, Context, Result


def parse_verdict(report: Path) -> str | None:
    if not report.exists():
        return None
    text = report.read_text(encoding="utf-8")
    m = re.search(r"总判定[：:]\s*\*{0,2}\s*(通过|不通过|失败)", text)
    return m.group(1) if m else None


class Gatekeeper(Agent):
    name = "gatekeeper"
    stage = "composed"

    def run(self, ctx: Context) -> Result:
        if ctx.run.stage == "qc_passed":
            return Result.ok("门禁已过，停在人工发布闸门", to="awaiting_publish")

        qc = ctx.run.artifacts.get("qc_report", "")
        report = Path(qc) if qc else None
        if not report or not report.exists():
            return Result.rework("找不到质检报告，不能放行")

        verdict = parse_verdict(report)
        if verdict != "通过":
            fails = re.findall(r"\|[^|]*\|\s*❌\s*\|", report.read_text(encoding="utf-8"))
            detail = f"{len(fails)} 项 FAIL" if fails else str(verdict)
            return Result.rework(f"质检未通过（{detail}），见 {report.name}")

        return Result.ok(f"质检通过：{report.name}", to="qc_passed")
