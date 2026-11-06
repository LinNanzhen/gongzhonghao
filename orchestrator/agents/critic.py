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

# 人味检查（依据 模板/人味检查表.md）。
# 硬伤：命中即打回——这几条都是"没有新增信息还抬高语气"，不是风格偏好。
HUMANIZE_FAIL = [
    ("意义拔高", r"标志着|彰显了|预示着一个?新|开启了.{0,6}(时代|篇章)"),
    ("套话收尾", r"拭目以待|未来可期|让我们共同|期待更多可能|值得每个人深思"),
    ("AI 高频词", r"赋能|抓手|底层逻辑|颗粒度|势能|破局"),
]
# 提示：只写进报告，不阻断——上下文中可能成立（如"闭环反馈"是术语）。
HUMANIZE_WARN = [
    ("假对比", r"不仅.{0,12}更是"),
    ("伪深度", r"这背后是|本质上，"),
    ("起跑式铺垫", r"接下来[，,]?让我们|下面我们|让我们深入"),
    ("借笼统权威", r"(专家|业内人士|有研究)(认为|指出|表示)"),
    ("破折号密集", r"——[^。\n]{0,20}——"),
]


def scan_humanize(text: str) -> tuple[list[str], list[str]]:
    """返回 (硬伤, 提示)。"""
    body = re.sub(r"^>.*$", "", text, flags=re.M)   # 引文块不算作者笔法
    fails, warns = [], []
    for name, pattern in HUMANIZE_FAIL:
        hits = [m.group(0) for m in re.finditer(pattern, body)]
        if hits:
            fails.append(f"人味硬伤·{name}（{len(hits)} 处，如「{hits[0][:12]}」）")
    for name, pattern in HUMANIZE_WARN:
        hits = [m.group(0) for m in re.finditer(pattern, body)]
        if hits:
            warns.append(f"人味提示·{name}（{len(hits)} 处，如「{hits[0][:14]}」）")
    return fails, warns
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

        human_fails, human_warns = scan_humanize(text)
        problems.extend(human_fails)
        # 加粗是编辑预算（SOP v8 §05），不是主线成立的证据，只提示
        if len(bolds) < 2:
            human_warns.append(f"加粗句只有 {len(bolds)} 处（建议 2 处以上，便于扫读）")
        # 直接引语密度：全是转述时，正文会变成作者在替受访者说话
        quotes = re.findall(r"[“\"]([^”\"]{6,})[”\"]", text)
        per_k = len(quotes) / max(1.0, len(text) / 1000)
        if per_k < 2:
            human_warns.append(f"直接引语偏少（{len(quotes)} 处 / {len(text)} 字，建议每千字 2 处以上）")
        long_paras = [p for p in text.split("\n")
                      if len(p.strip()) > 150 and not p.lstrip().startswith(("#", ">", "|", "-"))]
        if long_paras:
            human_warns.append(f"{len(long_paras)} 段超过 150 字（建议 60—90 字一段）")

        rows = "\n".join(
            f"| {n}｜{t[:18]} | {'✅' if n else '❓'} | 待人工 | 待人工 | ✅ | 待定 |"
            for n, t in sections) or "| — | — | — | — | — | — |"
        verdict = ("**通过**：可机器判的两关已过，其余待人工读稿。"
                   if not problems else "**打回**：" + "；".join(problems))
        if human_warns:
            verdict += "\n\n> 人味提示（按语境判断，不必全改）：" + "；".join(human_warns)

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
