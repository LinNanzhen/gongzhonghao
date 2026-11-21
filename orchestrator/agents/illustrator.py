#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""illustrator：抽帧配图 + 900×383 封面 + 图源台账（对应 SOP §06 配图环节）。

契约（供 composer 与状态机使用）：
- ``name = "illustrator"``，处理 ``stage = "critiqued"`` 的 run；
- 成功/降级都 ``Result.ok(..., to="illustrated")``——绝不 blocked，没有图也要能出稿；
- 产物 artifacts（相对 ``case_dir`` 的 posix 路径）：
    ``draft``         → 带图正文 ``{guest}_人味版_v{N}_插图.md``（覆盖 writer 的 draft 键）
    ``cover``         → 封面 ``封面_{date}_{guest}.jpg``（900×383）
    ``figure_ledger`` → 图源台账 ``图源_{date}_{guest}.md``

降级策略（逐项跳过、写清 note，不编造图片）：
- 没有正文 md / Pillow 缺失 → 整体跳过，原稿直接进 composer；
- 找不到视频 / 找不到 ffmpeg / 抽帧失败 → 跳过正文图（封面也不做）；
- 没有可定位引语（没有 ``—— 原视频 mm:ss``）→ 不做正文图；
- 找不到简体中文字体 / 正文没有 H1 → 只跳过封面，正文图照做。

字体：NotoSansCJK-*.ttc 的 index=0 常是 JP，直接用会让「门」「骨」等字形变形；
封面用 :func:`figures.find_cjk_font` 扫 TTC 字面，优先取 getname() 含 SC 的。
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .. import figures
from ..versions import next_version
from .base import Agent, Context, Result, guest_of

# 正文图数量与文件名模板
FIGURE_COUNT = figures.DEFAULT_FIGURES
FIGURE_DIR = "图片"
IMAGE_DIR_POSIX = "图片"


def _rel_posix(case_dir: Path, path: Path) -> str:
    """artifacts 契约：相对 case_dir 的 posix 路径；不在目录内返回绝对路径。"""
    try:
        return path.relative_to(case_dir).as_posix()
    except ValueError:  # pragma: no cover - 正常流程不会走到
        return path.as_posix()


def _unique_md_path(case_dir: Path, guest: str) -> Path:
    """``{guest}_人味版_v{N}_插图.md``：版本只增不删，绝不覆盖上一版。"""
    version = next_version(case_dir, re.escape(guest) + r"_人味版_v(\d+)_插图\.md")
    while True:
        path = case_dir / f"{guest}_人味版_v{version}_插图.md"
        if not path.exists():
            return path
        version += 1


def _transcript_path(ctx: Context) -> Path | None:
    """转写稿定位：优先 run.artifacts["transcript"]，其次 ``转写/transcript.json``。"""
    path = ctx.artifact_path("transcript")
    if path and path.exists():
        return path
    fallback = ctx.case_dir / "转写" / "transcript.json"
    return fallback if fallback.exists() else None


class Illustrator(Agent):
    name = "illustrator"
    stage = "critiqued"

    def run(self, ctx: Context) -> Result:
        case_dir = ctx.case_dir
        source = ctx.artifact_path("draft")
        if not source or not source.exists():
            return Result.ok("找不到 writer 正文 md：配图跳过，原稿直接进排版",
                             to="illustrated")
        try:
            md_text = source.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return Result.ok(f"正文读取失败（{exc}）：配图跳过", to="illustrated")

        if ctx.dry_run:
            return Result.ok("dry-run：跳过抽帧 / 封面 / 台账", to="illustrated")
        if not figures.PIL_AVAILABLE:
            return Result.ok("Pillow 不可用：跳过正文配图与封面，原稿直接进排版",
                             to="illustrated")

        guest = guest_of(case_dir)
        today = date.today().isoformat()
        notes: list[str] = []

        # ---- 1. 解析引语并选点 ------------------------------------------------
        blocks = figures.quote_blocks(md_text)
        transcript = _transcript_path(ctx)
        if transcript is not None:
            segments = figures.load_transcript(transcript)
            if segments:
                located = figures.locate_quotes(blocks, segments)
                recovered = sum(1 for before, after in zip(blocks, located)
                                if not before.locatable and after.locatable)
                if recovered:
                    notes.append(f"{recovered} 处引语缺时间戳，已按转写稿补齐")
                blocks = located
        # 多备 2 个候选：抽到黑帧/空镜时可以顺延，避免整篇缺图
        picks = figures.select_quotes(blocks, FIGURE_COUNT + 2)
        if not picks:
            return Result.ok("正文里没有带「—— 原视频 mm:ss」的可定位引语：配图跳过",
                             to="illustrated")
        if len(picks) < FIGURE_COUNT:
            notes.append(f"可定位引语只有 {len(picks)} 处（少于 {FIGURE_COUNT} 处），"
                         "正文图按实际数量出")

        # ---- 2. 定位视频与 ffmpeg --------------------------------------------
        media = ctx.media_path()
        ffmpeg = figures.find_ffmpeg()
        if media is None:
            notes.append("找不到本期原视频：跳过正文图与封面")
            return Result.ok("找不到本期原视频：配图跳过（不编造图片）", to="illustrated")
        if not ffmpeg:
            notes.append("找不到 ffmpeg：跳过正文图与封面")
            return Result.ok("找不到 ffmpeg：配图跳过（不编造图片）", to="illustrated")

        # ---- 3. 抽帧 + 缩放锐化 ----------------------------------------------
        image_dir = case_dir / FIGURE_DIR
        try:
            image_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return Result.ok(f"图片目录无法创建（{exc}）：配图跳过", to="illustrated")

        produced: list[tuple[figures.QuoteBlock, int]] = []
        for quote in picks:
            if len(produced) >= FIGURE_COUNT:
                break
            number = len(produced) + 1
            raw = image_dir / f".fig_{number:02d}.raw.jpg"
            out = image_dir / f"fig_{number:02d}.jpg"
            taken = False
            # 同一处引语最多试 3 个偏移：黑场/空镜时往后挪几秒再抽
            for offset in (0, 6, 12):
                ok, err = figures.extract_frame(media, int(quote.seconds or 0) + offset,
                                                raw, ffmpeg=ffmpeg)
                if not ok:
                    notes.append(f"抽帧失败（{quote.timestamp}）：{err[:100]}；跳过该处")
                    raw.unlink(missing_ok=True)
                    break
                if not figures.frame_is_usable(raw):
                    notes.append(f"{quote.timestamp} 抽到黑帧/空镜（+{offset}s），顺延下一候选")
                    raw.unlink(missing_ok=True)
                    continue
                try:
                    figures.render_figure(raw, out)
                except (OSError, figures.FigureError) as exc:
                    notes.append(f"图片处理失败（{quote.timestamp}）：{exc}；跳过该张")
                    raw.unlink(missing_ok=True)
                    continue
                raw.unlink(missing_ok=True)
                taken = True
                break
            if taken:
                produced.append((quote, number))

        if not produced:
            return Result.ok("抽帧全部失败：配图跳过（不编造图片）",
                             to="illustrated")

        # ---- 4. 插图（按行号从后往前，保证行号不串位；同路径幂等）-------------
        inserts: list[tuple[int, str, str]] = []
        figure_rows: list[dict] = []
        for quote, number in produced:
            image_rel = f"{IMAGE_DIR_POSIX}/fig_{number:02d}.jpg"
            image_path = image_dir / f"fig_{number:02d}.jpg"
            inserts.append((quote.end_line, image_rel,
                            figures.caption(quote.speaker, quote.text, quote.timestamp)))
            figure_rows.append({
                "name": f"fig_{number:02d}",
                "timestamp": quote.timestamp,
                "file": image_rel,
                "purpose": figures.figure_purpose(quote.section_title, quote.speaker, quote.text),
                "sha256": figures.sha256_file(image_path),
            })
        new_md_text = figures.insert_figures(md_text, inserts)

        # ---- 5. 封面（第一张正文帧裁 900×383）--------------------------------
        cover_path: Path | None = None
        cover_row: dict | None = None
        title = figures.parse_h1(md_text)
        font = figures.find_cjk_font(extra_paths=[case_dir, case_dir / "字体"]) if title else None
        if not title:
            notes.append("正文没有 H1 标题：跳过封面")
        elif font is None:
            notes.append("找不到简体中文字体（CJK）：跳过封面")
        else:
            cover_quote, cover_number = (produced[1] if len(produced) > 1 else produced[0])
            cover_path = case_dir / f"封面_{today}_{guest}.jpg"
            try:
                meta = figures.render_cover(
                    image_dir / f"fig_{cover_number:02d}.jpg", cover_path, title, font,
                    footer=f"{guest} · 原视频 {cover_quote.timestamp}")
                cover_row = {
                    "timestamp": cover_quote.timestamp,
                    "file": _rel_posix(case_dir, cover_path),
                    "sha256": figures.sha256_file(cover_path),
                    "title_lines": meta.get("title_lines", []),
                }
            except (OSError, figures.FigureError) as exc:
                notes.append(f"封面生成失败（{exc}）：跳过封面")
                cover_path = None

        # ---- 6. 新正文 md（版本只增不删）-------------------------------------
        artifacts: dict[str, str] = {}
        target_md = _unique_md_path(case_dir, guest)
        self.write(ctx, target_md, new_md_text)
        artifacts["draft"] = _rel_posix(case_dir, target_md)

        # ---- 7. 图源台账 ------------------------------------------------------
        ledger_path = case_dir / f"图源_{today}_{guest}.md"
        ledger_text = figures.render_ledger(
            guest=guest, date_str=today, media=str(media),
            figures=figure_rows, cover=cover_row, notes=notes)
        self.write(ctx, ledger_path, ledger_text)
        artifacts["figure_ledger"] = _rel_posix(case_dir, ledger_path)
        if cover_path is not None and cover_path.exists():
            artifacts["cover"] = _rel_posix(case_dir, cover_path)

        # ---- 8. 完成 ----------------------------------------------------------
        note = f"配图：{len(produced)} 张正文图（fig_01–fig_{len(produced):02d}）"
        note += " + 封面" if cover_path is not None else "（封面已跳过）"
        note += " + 图源台账"
        if notes:
            note += "；降级：" + "；".join(notes)
        return Result.ok(note, artifacts=artifacts, to="illustrated")
