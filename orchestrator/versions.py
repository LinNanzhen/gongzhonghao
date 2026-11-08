#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""产物版本化（T3）：同一案例里重跑不得覆盖上一版。

工作区既有约定是「版本只增不删」：文件名带 `_vN`，新版本另存。
这里只提供一个纯函数，writer / composer 共用，避免各写一套扫描逻辑。

    >>> next_version(case, r"苏姿丰回归_v(\\d+)_排版预览\\.html")
    2   # 已存在 v1
"""
from __future__ import annotations

import re
from pathlib import Path


def next_version(case_dir: Path, pattern: str, start: int = 1) -> int:
    """扫描 `case_dir` 下匹配 `pattern` 的文件，返回下一个可用版本号。

    `pattern` 必须只有一个捕获组，组内容是可转 int 的版本号。
    没有匹配文件时返回 `start`。
    """
    rx = re.compile(pattern)
    best = start - 1
    try:
        names = [p.name for p in case_dir.iterdir() if p.is_file()]
    except OSError:
        return start
    for name in names:
        m = rx.search(name)
        if not m:
            continue
        try:
            best = max(best, int(m.group(1)))
        except (TypeError, ValueError):
            continue
    return best + 1
