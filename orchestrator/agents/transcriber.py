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
from ..transcript import load_any, to_payload, FEISHU_LINE

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

        tr_dir = ctx.case_dir / "转写"
        transcript = tr_dir / "transcript.json"

        if not transcript.exists():
            # 飞书妙记 / SRT / 纯文本：自动转成带时间戳的 transcript.json
            transcript = self._convert_if_possible(ctx, tr_dir) or transcript
            if not transcript.exists():
                return Result.blocked(
                    f"缺少转写底稿：{tr_dir}；把飞书妙记导出的逐字稿（.md/.txt/.srt）"
                    f"放进该目录即可，会自动转成 transcript.json"
                )

        card = ctx.case_dir / f"素材卡_{date.today().isoformat()}_{guest}.md"
        text = MATERIAL_CARD.format(guest=guest, today=date.today().isoformat(),
                                    transcript=transcript.relative_to(ctx.case_dir))
        self.write(ctx, card, text)
        return Result.ok(f"素材卡：{card.name}（片段表待人工/模型填充）",
                         artifacts={"material_card": str(card), "transcript": str(transcript)},
                         to="transcribed")

    def _convert_if_possible(self, ctx: Context, tr_dir):
        """把目录里的逐字稿转成 transcript.json；不合法就返回 None。"""
        if not tr_dir.exists():
            return None
        cands = [p for p in sorted(tr_dir.iterdir())
                 if p.suffix.lower() in (".md", ".txt", ".srt")
                 and "纪要" not in p.name and not p.name.startswith(".")]
        if not cands:
            return None
        src = cands[0]
        try:
            segs = load_any(src)
        except Exception:  # noqa: BLE001 - 解析失败不该让整轮崩
            return None
        if not segs:
            return None
        out = tr_dir / "transcript.json"
        self.write(ctx, out, __import__("json").dumps(
            to_payload(segs, source=src.name), ensure_ascii=False, indent=2))
        return out
