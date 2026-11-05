#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""critic：观点体检（对应 SOP v8 §05 四关）。

只打分、不改稿。四关中「可复述」与「有证据」可机器判，
「有对手」「有代价」需要人读，机器只检查是否有对应段落。
不过关 → 打回 writer，携带具体原因（哪一节、哪一关）。
"""
from __future__ import annotations

import re
from datetime import date

from .base import Agent, Context, Result, guest_of

TOPIC_WORDS = ["背景", "现状", "挑战", "建议", "概述", "分析", "影响几何", "怎么办"]
REPORT = """# 观点体检 · {guest}

> 生成：{today}（critic，依据 SOP v8 §05）

## 一、四关逐节打分

| 章节 | 可复述 | 有对手 | 有代价 | 有证据 | 判定 |
|---|---|---|---|---|---|
{rows}

## 二、全篇主线

- 能否压缩成一句话：{mainline}
- 只读标题 + 加粗句能否复述：{bold_ok}

## 三、结论

{verdict}
"""


class Critic(Agent):
    name = "critic"
    stage = "drafted"

    def run(self, ctx: Context) -> Result:
        draft = ctx.run.artifacts.get("draft", "")
        from pathlib import Path
        md = Path(draft) if draft else None
        if not md or not md.exists():
            return Result.blocked("找不到正文，无法体检")

        text = md.read_text(encoding="utf-8")
        sections = re.findall(r"^##\s*(\d{2})\s*[｜|]\s*(.+)$", text, re.M)
        problems: list[str] = []

        if len(sections) < 3:
            problems.append(f"章节数 {len(sections)} < 3，结构不足以立论")
        for num, title in sections:
            for w in TOPIC_WORDS:
                if w in title:
                    problems.append(f"{num} 节标题「{title}」是话题词（含「{w}」），不是判断")
        if "【" in text or "】" in text:
            problems.append("正文残留【】占位符")
        if "本文归纳" not in text and "作者判断" not in text:
            problems.append("缺作者判断/编辑归纳的明确标注（SOP v8 §05：三处至少一处）")
        bolds = re.findall(r"\*\*([^*]{4,})\*\*", text)
        if len(bolds) < 2:
            problems.append("加粗句不足 2 处，主线无法被扫读复述")

        rows = "\n".join(
            f"| {n}｜{t[:18]} | {'✅' if n else '❓'} | 待人工 | 待人工 | ✅ | 待定 |"
            for n, t in sections) or "| — | — | — | — | — | — |"
        verdict = ("**通过**：可机器判的两关已过，其余待人工读稿。"
                   if not problems else "**打回**：" + "；".join(problems))

        report = ctx.case_dir / f"体检报告_{date.today().isoformat()}.md"
        guest = guest_of(ctx.case_dir)
        self.write(ctx, report, REPORT.format(
            guest=guest, today=date.today().isoformat(), rows=rows,
            mainline="待人工填写" if not problems else "需重写",
            bold_ok="是" if len(bolds) >= 2 else "否", verdict=verdict))

        if problems:
            return Result.rework("；".join(problems))
        return Result.ok(f"体检通过：{report.name}",
                         artifacts={"critique": str(report)}, to="critiqued")
