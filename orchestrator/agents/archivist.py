#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""archivist：复盘（对应 SOP v8 §10）。

建立「生成稿 → 用户定稿 → 发布稿」的差异记录。只记录，不把本地稿
改名成「发布存档」（v8 §09 明确禁止）。
"""
from __future__ import annotations

from datetime import date

from .base import Agent, Context, Result, guest_of

TEMPLATE = """# 发布差异记录 · {guest}

> 生成：{today}（archivist）

## 一、采用 / 删除 / 换图

| 项目 | 本地稿 | 发布稿 | 处理 | 原因 |
|---|---|---|---|---|
| 主标题 |  |  |  |  |
| 首屏横幅 |  |  |  |  |
| 人物卡头像 |  |  |  |  |
| 正文配图 |  |  |  |  |
| 章节结构 |  |  |  |  |

## 二、待核实项

| 项 | 状态 | 说明 |
|---|---|---|
|  |  |  |

## 三、规则回写建议

- 本篇暴露的问题：
- 应补进 SOP 的判据：

## 四、数据与反馈（可获得时）

- 标题吸引来的读者是否匹配：
- 哪里难懂或中断：
- 哪张图有效：
"""


class Archivist(Agent):
    name = "archivist"
    stage = "published"

    def run(self, ctx: Context) -> Result:
        guest = guest_of(ctx.case_dir)
        path = ctx.case_dir / f"发布差异_{date.today().isoformat()}.md"
        self.write(ctx, path, TEMPLATE.format(guest=guest, today=date.today().isoformat()))
        return Result.ok(f"差异记录：{path.name}", artifacts={"postmortem": str(path)}, to="archived")
