#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""质检判定契约（T1c）：流水线与 gatekeeper 共用一份判定值。

为什么要有这个模块：此前 `流水线/gzh_pipeline.py` 写"**未通过（需修正）**"，
而 `agents/gatekeeper.py` 只匹配 `(通过|不通过|失败)`——"未通过"匹配不到，
verdict 变 None，门禁语义断裂。判定字符串一旦分散在两处手写正则，就必然再漂移。

三值语义（退出码见 gzh_pipeline.py）：
  PASS        通过：质检门禁全过，且三档渲染验收真的跑过
  FAIL        未通过：质检门禁有 FAIL，或任一 viewport 渲染验收失败 → 打回重排
  INCOMPLETE  未完成：没跑渲染（--skip-render / 无 Chrome / 渲染没产出任何结果）
              —— **绝不等于通过**，必须停下来补跑渲染

解析优先级：`判定码：` 行 > `总判定：` 行；两者都没有 → None（报告不可信）。
"""
from __future__ import annotations

import re

PASS = "PASS"
FAIL = "FAIL"
INCOMPLETE = "INCOMPLETE"

# 旧报告兼容值：v8 时代 gatekeeper 契约里出现过"失败"，历史产物可能仍带这个字面值。
LEGACY_FAIL = "失败"

ALL = (PASS, FAIL, INCOMPLETE)

# 机器可读行，例如：`- 判定码：INCOMPLETE`
_CODE_RE = re.compile(r"判定码[：:]\s*\**\s*(PASS|FAIL|INCOMPLETE)\b", re.IGNORECASE)

# 人类可读行，例如：`- 总判定：**未通过（需修正）**`
_TOTAL_RE = re.compile(r"总判定[：:]\s*([^\n]*)")

# 旧口径报告（T1 之前，没有 `判定码：` 行）里"渲染其实没跑完"的标记。
# 这些报告会写「总判定：**通过**」，只认总判定就会把假通过放进来——
# 例如 cases/16_苏姿丰回归_案例/苏姿丰回归_v1_质检报告.md 就是这种：
# 「总判定：通过」+「渲染验收 已跳过（--skip-render）」。
_INCOMPLETE_MARKERS = (
    "--skip-render",
    "渲染验收未执行",
    "渲染验收未完成",
    "渲染验收失败或未执行",
    "没有任何 viewport 产出结果",
)


def _from_total_line(line: str) -> str | None:
    """把「总判定」那行的自然语言判成三值。

    顺序很关键：「未通过」「未完成」都含子串「通过」，
    所以必须先判否定式，再判肯定式，否则永远命中 PASS。
    """
    if "未通过" in line or LEGACY_FAIL in line:
        return FAIL
    if "未完成" in line:
        return INCOMPLETE
    if "通过" in line:
        return PASS
    return None


def parse_report(text: str | None) -> str | None:
    """从质检报告全文解析判定值；解析不出返回 None。"""
    if not text:
        return None

    m = _CODE_RE.search(text)
    if m:
        return m.group(1).upper()

    # 无判定码 → 旧报告。先取总判定行，但确定性 FAIL 优先于"没跑完"。
    total: str | None = None
    for m in _TOTAL_RE.finditer(text):
        total = _from_total_line(m.group(1))
        if total:
            break
    if total == FAIL:
        return FAIL
    # 旧报告里"跳过渲染/无 Chrome/渲染没出结果"仍然是 UNFINISHED，不能算通过
    if any(marker in text for marker in _INCOMPLETE_MARKERS):
        return INCOMPLETE
    return total
