#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 适配器协议。

agent 只依赖这个接口，不依赖具体厂商。换模型 = 新增一个 Provider 子类，
在 loop.py 的 --provider 参数里选，不改 agent 代码。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


class ProviderError(RuntimeError):
    """模型调用失败（含限流、超时、返回不可用）。"""


@runtime_checkable
class Provider(Protocol):
    name: str

    def complete(self, *, task: str, system: str, prompt: str,
                 max_tokens: int = 2000, temperature: float = 0.7) -> str:
        """返回纯文本。失败抛 ProviderError，由调用方决定重试或降级。"""
        ...


def get_provider(name: str = "stub", **kwargs) -> Provider:
    if name == "stub":
        from .stub import StubProvider
        return StubProvider(**kwargs)
    raise ProviderError(f"未知 provider：{name}（可用：stub）")
