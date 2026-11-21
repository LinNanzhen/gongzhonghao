#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenAI 兼容 provider：一套代码覆盖 DeepSeek / Kimi / 通义 / OpenAI 等。

只依赖标准库（urllib），不引入 SDK。

配置（环境变量，key 不入库）：
    GZH_LLM_BASE_URL  默认 https://api.deepseek.com/v1
    GZH_LLM_API_KEY   必填（也接受 DEEPSEEK_API_KEY / OPENAI_API_KEY）
    GZH_LLM_MODEL     默认 deepseek-flash

预算策略：推理模型的 reasoning_content 计入 max_tokens，被占满时自动加倍重试，
直到 max_output（默认 32000，可用 GZH_LLM_MAX_OUTPUT 覆盖）为止。

为什么默认是 32000：writer 要一次写 2—4 千字正文，而推理模型会先把
reasoning_content 计进同一份预算；上限卡在 8192 时，正文会在半句处被截断
（2026-11-09 用 Ilya 访谈实录复现：出稿 2323 字后断在"包括 RL 环境"）。

重试策略（对齐 SOP v9 §07）：
    网络错误 / 429 / 5xx → 指数退避重试（默认 2 次）
    400 / 401 / 403     → 立即失败，不重试（重试没有意义，只会撞更多次）
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import uuid
import urllib.request

from .base import Provider, ProviderError

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

# 厂商预设：显式参数 > 环境变量 > 预设 > 通用默认
PRESETS: dict[str, tuple[str, str]] = {
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-flash"),
    "kimi": ("https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    "moonshot": ("https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "dashscope": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
}


class OpenAICompatProvider(Provider):
    name = "openai-compat"
    DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
    DEFAULT_MODEL = "deepseek-flash"

    def __init__(self, *, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, preset: str | None = None,
                 timeout: float = 90.0, max_retries: int = 2, backoff: float = 1.0,
                 max_output: int | None = None):
        p_base, p_model = PRESETS.get((preset or "").lower(),
                                      (self.DEFAULT_BASE_URL, self.DEFAULT_MODEL))
        self.base_url = (base_url or os.environ.get("GZH_LLM_BASE_URL") or p_base).rstrip("/")
        self.api_key = (api_key
                        or os.environ.get("GZH_LLM_API_KEY")
                        or os.environ.get("DEEPSEEK_API_KEY")
                        or os.environ.get("OPENAI_API_KEY") or "")
        self.model = model or os.environ.get("GZH_LLM_MODEL") or p_model
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        # 推理模型会把 reasoning 计入同一预算；8192 会把正文截在半句。
        self.max_output = int(max_output or os.environ.get("GZH_LLM_MAX_OUTPUT") or 32000)
        # T5a：成本记账。不改 complete() 签名（9 个调用点不用动），失败/成功都只
        # 更新实例属性；字段缺失如实标 None，不假装 0。
        # OpenCode Go 要求客户端带稳定会话头；其他 provider 忽略即可。
        self.session_id = uuid.uuid4().hex
        self.last_usage: dict[str, int | None] | None = None
        self.usage_total: dict[str, int] = {
            "input": 0, "output": 0, "total": 0, "calls": 0,
        }

    # ------------------------------------------------------------ 对外
    def complete(self, *, task: str, system: str, prompt: str,
                 max_tokens: int = 2000, temperature: float = 0.7) -> str:
        if not self.api_key:
            raise ProviderError(
                "缺少 API key：请设置环境变量 GZH_LLM_API_KEY（不要把 key 写进仓库）"
            )

        url = f"{self.base_url}/chat/completions"
        budget = max(1, int(max_tokens))

        while True:
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": budget,
                "temperature": temperature,
                "stream": False,
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                return self._with_retry(url, body, task)
            except _LengthExhausted as exc:
                if budget >= self.max_output:
                    raise ProviderError(
                        f"{task}：推理已占满 {budget} token 上限，仍无正文（{exc}）"
                    ) from None
                budget = min(budget * 2, self.max_output)

    # ------------------------------------------------------------ 内部
    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": os.environ.get("GZH_LLM_USER_AGENT", "gzh-pipeline/1.0"),
        }
        if "opencode.ai" in self.base_url:
            headers["x-opencode-session"] = self.session_id
        return headers

    def _record_usage(self, usage: object) -> None:
        """记一次成功调用的 token 用量（T5a）。

        - `last_usage`：本次 input/output/total；供应商没给的字段为 None（如实标注）；
        - `usage_total`：累计 input/output/total/calls；没回 usage 时 calls 照加、
          token 不加，`loop.py status` 据此把该 run 标成"未上报"而不是假装 0。
        """
        data = usage if isinstance(usage, dict) else {}
        inp = _as_int(data.get("prompt_tokens", data.get("input_tokens")))
        out = _as_int(data.get("completion_tokens", data.get("output_tokens")))
        total = _as_int(data.get("total_tokens"))
        if total is None and (inp is not None or out is not None):
            total = (inp or 0) + (out or 0)

        self.last_usage = {"input": inp, "output": out, "total": total}
        self.usage_total["calls"] += 1
        if total is None:
            return
        for key, value in (("input", inp), ("output", out), ("total", total)):
            if value is not None:
                self.usage_total[key] += value

    def _with_retry(self, url: str, body: bytes, task: str) -> str:
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return self._once(url, body, task)
            except _Retryable as exc:
                last = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(self.backoff * (2 ** attempt))
        raise ProviderError(f"{task}：重试 {self.max_retries} 次后仍失败（{last}）")

    def _once(self, url: str, body: bytes, task: str) -> str:
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = _safe_snippet(exc.read().decode("utf-8", errors="replace"))
            if exc.code in RETRYABLE_STATUS:
                raise _Retryable(f"HTTP {exc.code}：{detail}") from None
            raise ProviderError(f"{task}：HTTP {exc.code}（不重试）{detail}") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            raise _Retryable(f"网络错误：{exc}") from None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise ProviderError(f"{task}：响应不是 JSON（{_safe_snippet(raw)}）") from None

        choices = data.get("choices") or []
        if not choices:
            err = data.get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else None
            raise ProviderError(f"{task}：响应没有 choices（{msg or _safe_snippet(raw)}）")

        message = choices[0].get("message") or {}
        content = (message.get("content") or "").strip()
        if not content:
            finish = choices[0].get("finish_reason")
            reasoning = (message.get("reasoning_content") or "").strip()
            if reasoning and finish == "length":
                raise _LengthExhausted(f"推理 {len(reasoning)} 字占满预算")
            raise ProviderError(f"{task}：返回空内容（finish_reason={finish}）")
        self._record_usage(data.get("usage"))
        return content


class _Retryable(Exception):
    """内部标记：可重试错误。"""


class _LengthExhausted(Exception):
    """内部标记：预算被推理占满，需要加倍。"""


def _as_int(value: object) -> int | None:
    """usage 字段转 int；缺失/非法一律 None（如实标注，不当 0）。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_snippet(text: str, limit: int = 200) -> str:
    text = " ".join((text or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")
