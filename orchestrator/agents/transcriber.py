#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""transcriber：转写与素材卡（对应 SOP v8 §03）。

真实场景里转写是外部工具（whisperx / 豆包）产出的；本 agent 负责：
1. 检查 `转写/transcript.json` 是否存在；
2. 建素材卡骨架（时间戳｜说话者｜原文｜转述｜限定条件｜读者关联｜画面｜章节）；
3. 缺转写时不假装完成，直接 blocked，让流程停在可解释的位置。
"""
from __future__ import annotations

import json
from datetime import date

from .base import Agent, Context, Result, guest_of

MATERIAL_CARD = """# 素材卡 · {guest}

> 生成：{today}（transcriber）
> 底稿：{transcript}
> 规则：自动转写只是检索底稿；引文、数字、说话者需回听（SOP v8 §03）。

## 一、片段表

| # | 时间戳 | 说话者 | 原文 | 忠实中文转述 | 限定条件 | 读者关联 | 对应画面 | 拟用章节 |
|---|---|---|---|---|---|---|---|---|
| 1 | 00:00:00 |  |  |  |  |  |  |  |
| 2 |  |  |  |  |  |  |  |  |

## 二、待回听清单

| 项目 | 类型 | 原文位置 | 为什么需要回听 |
|---|---|---|---|
|  | 人名/术语/数字 |  |  |

## 三、节目信息

- 节目名称：
- 采访者：
- 录制/发布日期：
- 链接：
- 转写版本：
"""


class Transcriber(Agent):
    name = "transcriber"
    stage = "triaged"

    def run(self, ctx: Context) -> Result:
        src = ctx.run.source
        guest = guest_of(ctx.case_dir)

        transcript = ctx.case_dir / "转写" / "transcript.json"
        if not transcript.exists():
            # 允许逐字稿直接投递（.txt/.md）
            alts = list((ctx.case_dir / "转写").glob("*.md")) if (ctx.case_dir / "转写").exists() else []
            if not alts:
                return Result.blocked(
                    f"缺少转写底稿：{transcript}；请先把音频/逐字稿放入 {ctx.case_dir / '转写'}"
                )
            transcript = alts[0]

        card = ctx.case_dir / f"素材卡_{date.today().isoformat()}_{guest}.md"
        text = MATERIAL_CARD.format(guest=guest, today=date.today().isoformat(),
                                    transcript=transcript.relative_to(ctx.case_dir))
        self.write(ctx, card, text)
        return Result.ok(f"素材卡：{card.name}（片段表待人工/模型填充）",
                         artifacts={"material_card": str(card), "transcript": str(transcript)},
                         to="transcribed")
