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


# 中文稿件会混用「」与“”，统计时都要认
QUOTE_RE = re.compile(r"[“\"「]([^”\"」]{6,})[”\"」]")

# 「可复述」= 标题是判断 + 本节有加粗判断句（加粗是扫读入口，SOP v8 §05）
BOLD_SENTENCE_RE = re.compile(r"\*\*([^*\n]{4,})\*\*")
# 「有证据」= 可定位的 mm:ss 时间戳 / [n] 引用编号 / 带引号原话
TIMESTAMP_RE = re.compile(r"\b\d{1,2}:\d{2}\b")
CITE_RE = re.compile(r"\[\d{1,2}\]")


def split_sections(text: str) -> list[tuple[str, str, str]]:
    """按 `## NN｜标题` 切节，返回 [(章节号, 标题, 本节正文)]。"""
    heads = list(re.finditer(r"^##\s*(\d{2})\s*[｜|]\s*(.+)$", text, re.M))
    out: list[tuple[str, str, str]] = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out.append((m.group(1), m.group(2).strip(), text[m.end():end]))
    return out


def judge_section(title: str, body: str) -> dict:
    """机器判两关（可复述/有证据），其余两关只能人工。

    注意：之前表格里 `'✅' if n else '❓'` 的 `n` 是章节号字符串，恒真 →
    「可复述」列永远 ✅；「有证据」也是硬编码 ✅。这里改成逐节真算。
    """
    body_no_quote = re.sub(r"^>.*$", "", body, flags=re.M)   # 引文块不算作者笔法
    topic = next((w for w in TOPIC_WORDS if w in title), "")
    bolds = BOLD_SENTENCE_RE.findall(body_no_quote)
    has_evidence = bool(
        TIMESTAMP_RE.search(body) or CITE_RE.search(body) or QUOTE_RE.search(body)
    )
    return {
        "topic_word": topic,
        "bold_count": len(bolds),
        # 可复述：标题本身是判断句（不含话题词）且本节有加粗判断句
        "retell": (not topic) and bool(bolds),
        "evidence": has_evidence,
    }


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
        md = ctx.artifact_path("draft")
        if not md or not md.exists():
            return Result.blocked("找不到正文，无法体检")

        text = md.read_text(encoding="utf-8")
        secs = split_sections(text)
        sections = [(num, title) for num, title, _ in secs]
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
        quotes = QUOTE_RE.findall(text)
        per_k = len(quotes) / max(1.0, len(text) / 1000)
        if per_k < 2:
            human_warns.append(f"直接引语偏少（{len(quotes)} 处 / {len(text)} 字，建议每千字 2 处以上）")
        long_paras = [p for p in text.split("\n")
                      if len(p.strip()) > 150 and not p.lstrip().startswith(("#", ">", "|", "-"))]
        if long_paras:
            human_warns.append(f"{len(long_paras)} 段超过 150 字（建议 60—90 字一段）")

        # —— 体检表：逐节机器判定（T1b）。此前「可复述」是 `'✅' if n else '❓'`
        # （n 为章节号字符串恒真）、「有证据」硬编码 ✅，两列都不可信。
        rows: list[str] = []
        need_fix: list[str] = []
        judgment_titles = 0
        for num, title, body in secs:
            j = judge_section(title, body)
            retell = "✅" if j["retell"] else "❌"
            evidence = "✅" if j["evidence"] else "❓"
            if not j["topic_word"]:
                judgment_titles += 1
            if not j["retell"]:
                need_fix.append(num)
            rows.append(
                f"| {num}｜{title[:18]} | {retell} | 待人工 | 待人工 | {evidence} | "
                f"{'需改' if retell == '❌' else '待人工'} |"
            )
        rows_text = "\n".join(rows) or "| — | — | — | — | — | — |"

        # 全篇主线：机器只判"有没有压缩成一句话的基础"，那句话本身仍要人读
        if secs:
            basis = ("具备一句话主线的基础"
                     if (judgment_titles == len(secs) and len(bolds) >= 2)
                     else "主线基础不足（有标题不是判断句，或加粗句不足 2 处）")
            mainline = (f"机器初判：{basis}——{len(secs)} 节中 {judgment_titles} 节标题是判断句，"
                        f"全篇加粗句 {len(bolds)} 处；一句话主线仍需人工确认")
        else:
            mainline = "无编号章节（## NN｜标题），机器无法评估主线"

        if problems:
            verdict = "**打回**：" + "；".join(problems)
        elif need_fix:
            verdict = ("**通过（机器项有未过，需人工确认）**："
                       + "、".join(f"{n} 节" for n in need_fix)
                       + "「可复述」未见加粗判断句；按现行口径加粗属编辑预算，不单独打回。")
        else:
            verdict = "**通过**：可机器判的两关已过，其余待人工读稿。"
        if human_warns:
            verdict += "\n\n> 人味提示（按语境判断，不必全改）：" + "；".join(human_warns)

        report = ctx.case_dir / f"体检报告_{date.today().isoformat()}.md"
        guest = guest_of(ctx.case_dir)
        self.write(ctx, report, REPORT.format(
            guest=guest, today=date.today().isoformat(), rows=rows_text,
            mainline=mainline,
            bold_ok="是" if len(bolds) >= 2 else "否", verdict=verdict))

        # artifacts 存相对 case_dir 的 posix 路径（T3c），云端产物拉回本地仍可解析
        try:
            critique = report.relative_to(ctx.case_dir).as_posix()
        except ValueError:  # pragma: no cover - report 一定在 case_dir 下
            critique = str(report)

        if problems:
            return Result.rework("；".join(problems))
        return Result.ok(f"体检通过：{report.name}",
                         artifacts={"critique": critique}, to="critiqued")

