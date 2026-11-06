#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桩实现：不联网、确定性输出，用于跑通状态机与门禁。

约定：所有输出以 `[STUB]` 开头，agent 看到这个前缀就知道"这是占位"，
改用内置的安全骨架，从而保证 end-to-end 能过流水线门禁。
"""
from __future__ import annotations

import hashlib

from .base import Provider

STUB_RESPONSES: dict[str, str] = {
    "scout.topic_card": """[STUB] 选题卡占位。
读者问题：这份素材里，读者最需要被解释的是什么？
人物选择：哪一次真实选择能回答上面的问题？
初步判断：一句可被证据修正的话。
阅读收益：读前知道 X，读后知道 Y。
素材条件：原话/数据/对照是否够。""",

    "writer.draft": """[STUB] 正文占位。见 agent 内置骨架。""",

    "critic.report": """[STUB] 体检报告占位。""",

    "researcher.notes": """[STUB] 研究笔记占位。""",
}


class StubProvider(Provider):
    name = "stub"

    def __init__(self, noise: str = "", **_: object):
        self.noise = noise

    def complete(self, *, task: str, system: str, prompt: str,
                 max_tokens: int = 2000, temperature: float = 0.7) -> str:
        base = STUB_RESPONSES.get(task, f"[STUB] 未定义的 task：{task}")
        digest = hashlib.sha1(f"{task}{prompt}".encode("utf-8")).hexdigest()[:8]
        return f"{base}\n\n<!-- stub {task} {digest} -->"
