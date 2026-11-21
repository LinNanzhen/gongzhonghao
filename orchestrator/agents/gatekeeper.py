#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gatekeeper：门禁与发布前验收（对应 SOP v8 §09）。

两件事：
1. 读流水线质检报告，按判定码分派：
   PASS       → qc_passed（可以往人工发布闸门走）
   FAIL       → 打回 composer（附 ❌ 项数/明细）
   INCOMPLETE → blocked：渲染验收没跑，停下来本地补跑后 resume（**绝不能放行**）
   报告存在但解析不出判定 → blocked：报告不可信，不放行
2. qc_passed → awaiting_publish（人工闸门，必须停）。

判定字符串统一用 `orchestrator/verdict.py`（T1c），本文件不再手写正则。
绝不自动发布。没有 --force-publish 这种东西。
"""
from __future__ import annotations

import re
from pathlib import Path

from ..verdict import FAIL, INCOMPLETE, PASS, parse_report
from .base import Agent, Context, Result


def parse_verdict(report: Path) -> str | None:
    """兼容旧调用点：报告路径 → 判定值；解析不出返回 None。

    真正的解析规则在 `orchestrator.verdict.parse_report`，这里只负责读文件。
    """
    if not report.exists():
        return None
    return parse_report(report.read_text(encoding="utf-8"))


def fail_items(text: str) -> list[str]:
    """从质检报告表格里抓 ❌ 行，返回检查项名（去重保序）。"""
    items = [m.strip() for m in re.findall(r"^\|\s*([^|\n]+?)\s*\|\s*❌\s*\|", text, re.M)]
    return list(dict.fromkeys(items))


class Gatekeeper(Agent):
    name = "gatekeeper"
    stage = "composed"

    def run(self, ctx: Context) -> Result:
        if ctx.run.stage == "qc_passed":
            return Result.ok("门禁已过，停在人工发布闸门", to="awaiting_publish")

        # artifacts 存相对 case_dir 的路径（T3c）；旧 run 的绝对路径也兼容
        report = ctx.artifact_path("qc_report")
        if not report or not report.exists():
            return Result.rework("找不到质检报告，不能放行")

        text = report.read_text(encoding="utf-8")
        verdict = parse_report(text)

        if verdict == PASS:
            return Result.ok(f"质检通过：{report.name}", to="qc_passed")

        if verdict == FAIL:
            fails = fail_items(text)
            if fails:
                detail = "；".join(fails[:6]) + ("…" if len(fails) > 6 else "")
                return Result.rework(f"质检未通过（{len(fails)} 项 FAIL：{detail}），见 {report.name}")
            return Result.rework(f"质检未通过（渲染验收或外链等检查项 FAIL，明细见 {report.name}）")

        if verdict == INCOMPLETE:
            return Result.blocked("渲染验收未完成，需本地补跑渲染后 resume")

        # 报告存在但没有判定码/总判定：不可信，宁可停下也不放行
        return Result.blocked("质检报告无判定码，不可信")
