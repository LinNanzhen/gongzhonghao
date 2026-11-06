#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scout：选题侦察（对应 SOP v8 §02）。

产出选题卡，把「初步判断」写成一句可复述的话。桩模式下用模板，
真实 provider 时把素材标题/简介喂给模型生成候选。
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .base import Agent, Context, Result

TOPIC_CARD = """# 选题卡 · {guest}

> 素材：{url}
> 频道：{channel}
> 建立：{today}（由 scout 生成，待人工确认）

## 一、五项必答

| 项目 | 内容 |
|---|---|
| 读者问题 | 这份素材里，读者最需要被解释的是什么？（待填） |
| 人物选择 | 哪一次真实选择或事件能回答上面这个问题？（待填） |
| 初步判断 | 一句可被证据修正的话；写不出就说明还没有判断（待填） |
| 阅读收益 | 读前读者以为 __，读后应该知道 __（待填） |
| 素材条件 | 原话/关键事实/对照资料是否足以支撑（待填） |

## 二、最强材料（3—5 条，标注拟用章节）

| # | 原话 / 数据 / 反转 | 拟用章节 | 是否已定位时间戳 |
|---|---|---|---|
| 1 | | | |
| 2 | | | |
| 3 | | | |

## 三、报道类型

- [ ] 人物选择型（这个人为什么做出这样的选择）
- [ ] 主题型访谈（技术变化怎样影响读者的工作与决定）
- [ ] 完整访谈整理（受访者完整说了什么）

> 一篇只能选一条主线。主题型不先铺完整履历。

## 四、删除清单（避免旁支）

- 

---

**人工闸门**：确认本卡后才进入转写。未确认时 run 停在 `triaged`。
"""


def guess_guest(source) -> str:
    """从标题里猜人名；猜不出用「待确认人物」。"""
    title = (source.title or "").strip()
    if not title:
        return "待确认人物"
    # 去掉常见节目前缀
    cleaned = re.sub(r"^(【[^】]*】|\[[^\]]*\]|《[^》]*》)\s*", "", title)
    cleaned = re.split(r"[：:|丨\-—]", cleaned)[0].strip()
    cleaned = re.sub(r"[^\w\u4e00-\u9fff ]", "", cleaned).strip()
    return (cleaned[:12] or "待确认人物")


def _parse_json(raw: str) -> dict:
    """从可能带前后缀/代码块的输出里抠出 JSON 对象。"""
    import json
    body = raw.strip()
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", body)
    a, b = body.find("{"), body.rfind("}")
    if a == -1 or b <= a:
        return {}
    try:
        data = json.loads(body[a:b + 1])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _fill_fields(text: str, f: dict) -> str:
    """把 JSON 字段填进模板，空值保留（待填）占位。"""
    if not f:
        return text
    mapping = {
        "| 读者问题 |": f.get("reader_question"),
        "| 人物选择 |": f.get("person_choice"),
        "| 初步判断 |": f.get("thesis"),
        "| 阅读收益 |": f.get("payoff"),
        "| 素材条件 |": f.get("material"),
    }
    out = []
    for line in text.splitlines():
        hit = next((k for k in mapping if line.startswith(k)), None)
        if hit and isinstance(mapping[hit], str) and mapping[hit].strip():
            prefix = line.split("|", 2)[:2]
            out.append(f"{prefix[0]}|{prefix[1]}| {mapping[hit].strip()} |")
        else:
            out.append(line)
    return "\n".join(out)


class Scout(Agent):
    name = "scout"
    stage = "new"

    def run(self, ctx: Context) -> Result:
        src = ctx.run.source
        guest = guess_guest(src)
        card_path = ctx.case_dir / f"选题卡_{date.today().isoformat()}_{guest}.md"

        if card_path.exists():
            return Result.skip(f"选题卡已存在：{card_path.name}")

        llm = ctx.provider.complete(
            task="scout.topic_card",
            system=("你是深度报道编辑。只输出一个 JSON 对象，不要解释、不要代码块。"
                    "字段：reader_question（读者最需要理解什么）、person_choice（哪次真实选择能解释它）、"
                    "thesis（一句可被证据修正的判断）、payoff（读前以为X、读后知道Y）、"
                    "material（原话/数据/对照是否够，缺什么）。每项不超过 60 字。"),
            prompt=(f"标题：{src.title or '（无）'}\n频道：{src.channel or '（无）'}\n"
                    f"链接：{src.url}\n备注：{src.notes or '（无）'}"),
            max_tokens=2000,
        )
        ctx.save_llm("scout", llm)

        fields = _parse_json(llm) or {}
        text = TOPIC_CARD.format(guest=guest, url=src.url,
                                 channel=src.channel or "未标注",
                                 today=date.today().isoformat())
        text = _fill_fields(text, fields)
        if not fields and not llm.startswith("[STUB]"):
            text += f"\n## 五、模型原始输出（未解析为字段）\n\n{llm.strip()}\n"

        self.write(ctx, card_path, text)
        return Result.ok(f"选题卡：{card_path.name}；等待人工确认选题",
                         artifacts={"topic_card": str(card_path)}, to="triaged")
