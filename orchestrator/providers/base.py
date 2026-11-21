#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 适配器协议与工厂。

agent 只依赖这个接口，不依赖具体厂商。换模型 = 新增一个 Provider 子类，
在 loop.py 的 --provider 参数里选，不改 agent 代码。
"""
from __future__ import annotations

import os
from typing import Protocol, runtime_checkable


class ProviderError(RuntimeError):
    """模型调用失败（含限流、超时、鉴权、返回不可用）。"""


@runtime_checkable
class Provider(Protocol):
    name: str

    def complete(self, *, task: str, system: str, prompt: str,
                 max_tokens: int = 2000, temperature: float = 0.7) -> str:
        """返回纯文本。失败抛 ProviderError，由调用方决定重试或降级。"""
        ...


# 别名 → 规范名
ALIASES = {
    "stub": "stub",
    "openai": "openai-compat",
    "openai-compat": "openai-compat",
    "openai_compat": "openai-compat",
    "deepseek": "openai-compat",
    "kimi": "openai-compat",
    "moonshot": "openai-compat",
    "qwen": "openai-compat",
    "dashscope": "openai-compat",
}


def get_provider(name: str = "stub", **kwargs) -> Provider:
    """按名字构造 provider。kwargs 只透传给需要的实现。

    可用名字：stub / openai-compat（别名 deepseek、kimi、qwen…）
    """
    raw = (name or "stub").strip().lower()
    key = ALIASES.get(raw)
    if key is None:
        raise ProviderError(
            f"未知 provider：{name}（可用：{', '.join(sorted(set(ALIASES)))}）"
        )

    if key == "stub":
        from .stub import StubProvider
        return StubProvider(**{k: v for k, v in kwargs.items() if k == "noise"})

    from .openai_compat import OpenAICompatProvider
    allowed = ("base_url", "api_key", "model", "timeout", "max_retries", "backoff")
    picked = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    # 长文生成（writer 一次 5k 字）常有 >90s 无数据；读超时可用环境变量放宽
    try:
        picked.setdefault("timeout", float(os.environ.get("GZH_LLM_TIMEOUT", "300")))
    except ValueError:
        picked.setdefault("timeout", 300.0)
    try:
        picked.setdefault("max_retries", int(os.environ.get("GZH_LLM_MAX_RETRIES", "2")))
    except ValueError:
        picked.setdefault("max_retries", 2)
    picked.setdefault("preset", raw)   # 让 deepseek/kimi/qwen 各自匹配端点
    return OpenAICompatProvider(**picked)
