#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""writer：写稿（对应 SOP v8 §05）。

桩模式输出的是一份**能过流水线门禁的最小骨架**：
章节 01/02 编号、判断句标题、作者判断单独标注、无【】占位符、无高危禁用词。
接真实模型后，把 task="writer.draft" 换成真正的提示词即可，门禁不变。
"""
from __future__ import annotations

from datetime import date

from .base import Agent, Context, Result, guest_of

SKELETON = """# 待定标题：把主线压成一句可复述的判断

## 导语

这段访谈最值得注意的不是结论，而是结论成立的条件。**作者判断已单独标注，与受访者原话分开。**

本文回答一个问题：当变化已经发生，普通读者的选择会怎样被改变。

## 01｜先被重新定价的是入门岗位

（这一节只推进一个问题：变化最先落在谁身上。）

> 可定位的原话放在这里。
> —— 受访者，原视频 00:00:00 [1]

**这一句是本节结论。**

## 02｜能力在飙升，价格还没动

（解释能力曲线与经济影响之间的裂缝。）

**裂缝来自采用速度，不是模型能力。**

## 03｜什么在涨价：意图、判断、品味

（这一节回答：如果按任务定价，哪些任务变贵了。）

> 第二处原话。
> —— 受访者，原视频 00:00:00 [1]

**变贵的是那些需要人来决定要做什么的任务。**

## 结尾

回到开头的问题：变化已经发生，但兑现方式取决于谁先改变工作方式。

> 本文归纳：三节合起来说明的是一件事——重新定价发生在任务层，不在岗位层。

---

## 来源与说明

[1] 节目名称，发布日期，链接；引文对应原视频时间戳。
"""


class Writer(Agent):
    name = "writer"
    stage = "researched"

    def run(self, ctx: Context) -> Result:
        md = ctx.case_dir / f"{guest_of(ctx.case_dir)}_人味版.md"
        llm = ctx.provider.complete(
            task="writer.draft",
            system="你是深度报道作者。按 SOP v8 §05 写正文：章节 01/02 编号，标题是判断不是话题，"
                   "作者判断单独标注，不用【】占位符。",
            prompt=f"选题卡、素材卡、台账、研究笔记都在 {ctx.case_dir}。请写出正文骨架。",
            max_tokens=8000,   # 推理模型含 reasoning，正文需要余量
        )
        ctx.save_llm("writer", llm)

        text = SKELETON if llm.lstrip().startswith("[STUB]") else llm.strip() + "\n"
        self.write(ctx, md, text)
        return Result.ok(f"正文：{md.name}", artifacts={"draft": str(md)}, to="drafted")
