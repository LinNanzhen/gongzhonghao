#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""researcher：研究补证据（对应 SOP v8 §04）。

只输出短笔记，覆盖五件事。资料只服务读者问题，不做行业综述。
"""
from __future__ import annotations

from datetime import date

from .base import Agent, Context, Result, guest_of

NOTES = """# 研究笔记 · {guest}

> 生成：{today}（researcher）

## 一、行业所处阶段

（这个领域现在到哪一步了？用一句话说，不铺历史。）

## 二、关键证据

| # | 证据 | 来源 | 口径/时间 | 能支持的主张 |
|---|---|---|---|---|
| 1 |  |  |  |  |

## 三、一个有解释力的对照

（只写一个，说明可比性；不要罗列公司名单。）

## 四、主要不确定性

- 

## 五、作者暂定判断

> 一句可被证伪的话。若只能写成「变化很快」，说明还没有判断。
"""


class Researcher(Agent):
    name = "researcher"
    stage = "ledgered"

    def run(self, ctx: Context) -> Result:
        guest = guest_of(ctx.case_dir)
        path = ctx.case_dir / f"研究笔记_{date.today().isoformat()}_{guest}.md"
        if path.exists():
            return Result.skip(f"研究笔记已存在：{path.name}")

        llm = ctx.provider.complete(
            task="researcher.notes",
            system="你是研究助理。只补证据与对照，不给观点文章。",
            prompt=f"选题卡与台账在 {ctx.case_dir}；请列出还需要核实的三件事。",
            max_tokens=800,
        )
        ctx.save_llm("researcher", llm)

        self.write(ctx, path, NOTES.format(guest=guest, today=date.today().isoformat()))
        return Result.ok(f"研究笔记：{path.name}", artifacts={"research": str(path)}, to="researched")
