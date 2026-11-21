#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""writer：写稿（对应 SOP v10 §05，对话解说式）。

桩模式输出的是一份**能过流水线门禁的最小骨架**：
章节 01/02 编号、判断句标题、缩写对话行、文末归属声明、无【】占位符、无高危禁用词。
正文与简介卡一次产出：模型在正文后写「===简介卡===」分隔段，本 agent 拆出
`{嘉宾}_简介卡_vN.html` 登记为 card 产物，composer 排版时经 `--card` 注入。
接真实模型后，把 task="writer.draft" 换成真正的提示词即可，门禁不变。
"""
from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path

from .base import Agent, Context, Result, gather_context, guest_of
from ..versions import next_version


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


# 上下文预算（字）。qwen-plus 131k tokens；中文 60k 字 + 24k 素材约 70k tokens，
# 留出输出与 reasoning 空间。可用环境变量按模型上下文调小/调大。
TRANSCRIPT_CHARS = _env_int("GZH_WRITER_TRANSCRIPT_CHARS", 60000)
CONTEXT_CHARS = _env_int("GZH_WRITER_CONTEXT_CHARS", 24000)
CONTEXT_PER_FILE = _env_int("GZH_WRITER_CONTEXT_PER_FILE", 8000)


def transcript_excerpt(case_dir: Path, limit: int | None = None) -> str:
    """转写原文（默认最多 6 万字），带时间戳，供模型引用。"""
    limit = TRANSCRIPT_CHARS if limit is None else limit
    tr_dir = case_dir / "转写"
    text = ""
    js = tr_dir / "transcript.json"
    if js.exists():
        try:
            data = json.loads(js.read_text(encoding="utf-8"))
            lines = []
            for seg in (data.get("segments") or []):
                total = int(seg.get("start", 0))
                mm, ss = divmod(total, 60)
                spk = seg.get("speaker") or ""
                lines.append(f"[{mm:02d}:{ss:02d}]{(' ' + spk) if spk else ''} {seg.get('text', '')}")
            text = "\n".join(lines)
        except (ValueError, OSError):
            text = ""
    if not text and tr_dir.exists():
        for cand in sorted(tr_dir.glob("*")):
            if cand.suffix.lower() in (".md", ".srt", ".txt"):
                try:
                    text = cand.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = ""
                break
    if len(text) > limit:
        text = text[:limit] + f"\n…（转写共 {len(text)} 字，已截断，后面没看到的部分不要编）"
    return text


SKELETON = """# 待定标题：把主线压成一句可复述的判断

## 导语

这段访谈最值得注意的不是结论，而是结论成立的条件。我们顺着对话往下看。

<!-- 人物卡 -->

## 01｜先被重新定价的是入门岗位

（这一节只推进一个问题：变化最先落在谁身上。）

> IS：可定位的原话放在这里。
> EN|Locatable original quote goes here.
> —— 原视频 00:00:00 [1]

**这一句是本节结论。**

## 02｜能力在飙升，价格还没动

（解释能力曲线与经济影响之间的裂缝。）

**裂缝来自采用速度，不是模型能力。**

## 03｜什么在涨价：意图、判断、品味

（这一节回答：如果按任务定价，哪些任务变贵了。）

> DP：第二处原话。
> EN|A second locatable quote.
> —— 原视频 00:00:00 [1]

**变贵的是那些需要人来决定要做什么的任务。**

## 结尾

回到开头的问题：变化已经发生，但兑现方式取决于谁先改变工作方式。三节合起来说明的是一件事——重新定价发生在任务层，不在岗位层。

## 来源与说明

[1] 节目名称，发布日期，链接；引文对应原视频时间戳。除署名缩写的对话原话外，本文的叙述、归纳与判断均为作者观点。

===简介卡===
<section style="border:0.5px solid #e8e4dc;border-radius:12px;margin:28px 0;padding:18px 20px;">
<p style="margin:0 0 2px;font-size:18px;font-weight:600;color:#1a1a1a;line-height:1.4;">受访者全名 <span style="font-size:13px;font-weight:400;color:#a9a49a;">English Name · IS</span></p>
<p style="margin:0 0 10px;font-size:13px;color:#a3762a;line-height:1.6;">受访者 · 职务与机构（台账可核实字段）</p>
<p style="margin:0 0 4px;font-size:15px;line-height:1.75;color:#333333;">· 代表经历一行（查不到就不写）</p>
<p style="margin:0;font-size:15px;line-height:1.75;color:#333333;">· 教育经历一行（查不到就不写）</p>
</section>
<section style="border:0.5px solid #e8e4dc;border-radius:12px;margin:28px 0;padding:18px 20px;">
<p style="margin:0 0 2px;font-size:18px;font-weight:600;color:#1a1a1a;line-height:1.4;">采访者全名 <span style="font-size:13px;font-weight:400;color:#a9a49a;">English Name · DP</span></p>
<p style="margin:0 0 10px;font-size:13px;color:#a3762a;line-height:1.6;">采访者 · 节目名称（台账可核实字段）</p>
<p style="margin:0;font-size:15px;line-height:1.75;color:#333333;">· 代表经历一行（查不到就不写）</p>
</section>
"""

CARD_SEP = "===简介卡==="


def split_card(raw: str) -> tuple[str, str]:
    """把模型输出拆成（正文 md, 简介卡 HTML）。无分隔符时卡片为空串。"""
    text, sep, card = raw.partition(CARD_SEP)
    if not sep:
        return text.strip() + "\n", ""
    card = card.strip()
    # 容忍模型把卡片包进 ```html 围栏
    if card.startswith("```"):
        card = re.sub(r"^```[a-zA-Z]*\s*\n?", "", card)
        card = re.sub(r"\n?```\s*$", "", card)
    return text.strip() + "\n", card.strip()


class Writer(Agent):
    name = "writer"
    stage = "researched"

    def run(self, ctx: Context) -> Result:
        guest = guest_of(ctx.case_dir)
        # 版本只增不删（T3b）：重跑另存 `_vN`，旧稿保留供 diff
        n = next_version(ctx.case_dir, rf"{re.escape(guest)}_人味版_v(\d+)\.md")
        legacy = ctx.case_dir / f"{guest}_人味版.md"
        if n == 1 and legacy.exists():
            n = 2   # 历史命名视为已存在的 v1，不覆盖
        md = ctx.case_dir / f"{guest}_人味版_v{n}.md"
        llm = ctx.provider.complete(
            task="writer.draft",
            system=(
                "你是深度报道作者，为一篇人物访谈报道写正文。风格参照电影解说脚本："
                "你是带着读者看材料的叙述者，思考直接写进叙述，原话以对话形式嵌入。遵守以下硬性要求：\n"
                "0. 第一行必须是「# 主标题」（一句判断，≤20 字）；第二行「## 导语」，80—150 字；然后才是「## 01｜标题」。\n"
                "1. 章节写成「## 01｜标题」，标题是判断句，不是「背景/现状/挑战/建议」这类话题词。\n"
                "2. 对话放在引用块，说话人用姓名缩写直标（英文姓名取首字母，如 DP/IS；中文姓名取拼音首字母）："
                "「> IS：中文译文」一行、「> EN|英文原文」一行、「> —— 原视频 mm:ss」一行；"
                "同一组问答可放同一个引用块，逐行交替。每节至少 1—2 处对话，不要全部改写成转述。\n"
                "3. 英文原话必须中英对照，不允许只给译文；译文忠实，保留限定词（也许/我不确定），"
                "不把条件句改成断言。受访者只是推测的，叙述中写「据其自述」「可能」，不写成已证实的事实。\n"
                "4. 每节至少一处 **加粗判断句**（只加粗判断/结论，不加粗整段或受访者原话）。\n"
                "4b. 作者的归纳与判断直接写进叙述（「这里要看的是……」「把两段放在一起……」），可用「我们」；"
                "禁止「本文归纳：」「作者判断：」逐段标签，禁止「采访者问/他回答」式机械视角切换。\n"
                "5. 文末「## 来源与说明」列出引文时间戳与来源，并必须包含固定声明："
                "「除署名缩写的对话原话外，本文的叙述、归纳与判断均为作者观点。」\n"
                "6. 禁止拔高：不写「标志着」「彰显了」「开启了新时代」「让我们拭目以待」。\n"
                "7. 禁止假对比：「不仅……更是」、没有纠正误解的「不是……而是」、「这背后是」式的伪深度。\n"
                "8. 引号只用于可定位的原话；改写和拼接不加引号。材料里没有的事实、数字、原话一律不编。\n"
                "9. 不用赋能、抓手、闭环、底层逻辑、颗粒度、势能、破局这类词；不用【】占位符；"
                "不写「首先/其次/最后」的编号式行文；不写「值得注意的是」「不可忽视的是」「总而言之」。\n"
                "10. 段落控制在 3 行以内（60—90 字）；长句拆开，一句一个意思。\n"
                "11. 全文 1800—3000 字（不含英文原文），**必须写完整**：最后一节收束主线，不得在半句处停止。\n"
                "11b. 材料是长访谈时，按时间顺序覆盖主要段落，不要只写开头几分钟；结尾要能看出你读到了后半段。\n"
                "12. 正文写完后另起一行写「===简介卡===」，随后输出人物简介卡 HTML：素材里有几个说话人就几张卡，"
                "采访者也要有。每张卡一个 <section>（禁用 div/class/id/flex/position/渐变/图片），只写台账与研究笔记里"
                "可核实的字段：全名（中英对照）＋缩写、职业身份、代表机构/作品、教育经历，共 3—5 行；"
                "查不到的字段不写，不靠印象补，不堆头衔奖项。字号：姓名≥18px、身份≥13px、条目≥15px。"
            ),
            prompt=(
                "以下是一篇人物访谈报道的全部素材（选题卡、素材卡、事实台账、研究笔记的节选）。\n"
                "请只使用这些材料写正文；材料里没有的事实、数字、原话一律不要编。\n"
                "如果某处确实缺材料，就写得概括一些，不要标注「待核」之类的占位。\n\n"
                + (gather_context(ctx.case_dir,
                                  ("说话人对照", "选题卡", "素材卡", "事实台账", "研究笔记"),
                                  per_file=CONTEXT_PER_FILE, total=CONTEXT_CHARS)
                   or "（素材目录为空）")
                + "\n\n以下是转写原文（带时间戳；引用原话时标注 [mm:ss]）：\n"
                + (transcript_excerpt(ctx.case_dir) or "（没有转写稿）")
                + "\n\n现在写出完整正文。"
            ),
            max_tokens=16000,  # 推理模型含 reasoning；8000 会在半句处截断（2026-11-09 实测）
        )
        ctx.save_llm("writer", llm)

        raw = SKELETON if llm.lstrip().startswith("[STUB]") else llm
        text, card_html = split_card(raw)
        self.write(ctx, md, text)
        artifacts = {"draft": md.relative_to(ctx.case_dir).as_posix()}
        if card_html:
            card = ctx.case_dir / f"{guest}_简介卡_v{n}.html"
            self.write(ctx, card, card_html + "\n")
            artifacts["card"] = card.relative_to(ctx.case_dir).as_posix()
        return Result.ok(f"正文：{md.name}（v{n}）",
                         artifacts=artifacts,
                         to="drafted")
