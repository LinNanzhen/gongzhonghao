#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ledger：证据台账（对应 SOP v8 §03）。

台账是闸门：查不到的字段留空，不猜。本 agent 复制模板并预填
「类型 / 核验状态」两列的合法取值说明，避免手写时自由发挥。
"""
from __future__ import annotations

from datetime import date

from .base import Agent, Context, Result, guest_of

HEADER = """# 事实台账 · {guest}

> 生成：{today}（ledger，基于 `模板/事实台账.md`）
> 规则：**台账里查不到的人物字段，正文不能出现**（SOP v8 §03）。

## 一、主张台账（九列）

| ID | 正文主张/人物字段 | 类型 | 原话与定位 | 来源链接/文件 | 发生时间 | 来源发布日期 | 核验状态 | 对应段落 |
|---|---|---|---|---|---|---|---|---|
| F01 |  | 事实/自述/推论/编辑归纳 | 时间戳、页码或段落 | 原始来源优先 |  |  | 已核实/仅自述/有冲突/待核实/编辑判断 |  |
| F02 |  |  |  |  |  |  |  |  |

## 二、人物字段

| 字段 | 内容 | 核验状态 | 信源 |
|---|---|---|---|
| 姓名（中文） |  |  |  |
| 姓名（英文） |  |  |  |
| 现任职位 |  |  |  |
| 所属机构 |  |  |  |

> 官方 bio 查不到就整行删掉，不留空、不靠印象补。

## 三、编辑归纳（必须单列，不能写成「他说」）

| ID | 归纳内容 | 依据的原话/数据 | 出现在正文哪一处 |
|---|---|---|---|
| E01 |  |  |  |

## 四、最小值检查

- [ ] 每行只有**一个**可判断的主张
- [ ] 交易/金额/规模/价格/动机分别核验，未因一项通过就给整段打勾
- [ ] 预测记录了发布日、相对时间、前提与估算身份
"""


class Ledger(Agent):
    name = "ledger"
    stage = "transcribed"

    def run(self, ctx: Context) -> Result:
        guest = guest_of(ctx.case_dir)
        path = ctx.case_dir / f"事实台账_{date.today().isoformat()}_{guest}.md"
        if path.exists():
            return Result.skip(f"台账已存在：{path.name}")
        self.write(ctx, path, HEADER.format(guest=guest, today=date.today().isoformat()))
        return Result.ok(f"台账：{path.name}", artifacts={"ledger": str(path)}, to="ledgered")
