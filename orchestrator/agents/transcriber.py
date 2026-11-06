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

import os

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

    fetch_reason: str = ""
    feishu_reason: str = ""

    def run(self, ctx: Context) -> Result:
        src = ctx.run.source
        guest = guest_of(ctx.case_dir)

        tr_dir = ctx.case_dir / "转写"
        transcript = tr_dir / "transcript.json"

        if not transcript.exists():
            # 本地已有的飞书/SRT/文本稿优先
            transcript = self._convert_if_possible(ctx, tr_dir) or transcript
        if not transcript.exists():
            # 飞书妙记链接：直接调 API 拉转写
            transcript = self._try_feishu(ctx, tr_dir) or transcript
        if not transcript.exists():
            # 再尝试自动抓 YouTube 字幕（需 policy 开启且装了 yt-dlp）
            transcript = self._try_fetch(ctx, tr_dir) or transcript
            if not transcript.exists():
                return Result.blocked(
                    f"缺少转写底稿：{tr_dir}；把飞书妙记导出的逐字稿（.md/.txt/.srt）"
                    f"放进该目录即可，会自动转成 transcript.json。"
                    f"飞书：{self.feishu_reason or '不适用'}；"
                    f"YouTube：{self.fetch_reason or '未启用'}"
                )

        card = ctx.case_dir / f"素材卡_{date.today().isoformat()}_{guest}.md"
        text = MATERIAL_CARD.format(guest=guest, today=date.today().isoformat(),
                                    transcript=transcript.relative_to(ctx.case_dir))
        self.write(ctx, card, text)
        return Result.ok(f"素材卡：{card.name}（片段表待人工/模型填充）",
                         artifacts={"material_card": str(card), "transcript": str(transcript)},
                         to="transcribed")

    def _try_feishu(self, ctx: Context, tr_dir):
        """source 是飞书妙记链接时，用开放平台接口拉转写。"""
        self.feishu_reason = ""
        if ctx.dry_run:
            return None
        from ..feishu import FeishuError, fetch_minutes_transcript, is_minutes_url, minute_token
        url = ctx.run.source.url
        if not (is_minutes_url(url) and minute_token(url)):
            return None
        if not (os.environ.get("FEISHU_APP_ID") and os.environ.get("FEISHU_APP_SECRET")):
            self.feishu_reason = "缺少飞书应用凭证（FEISHU_APP_ID / FEISHU_APP_SECRET）"
            return None

        path = fetch_minutes_transcript(url, tr_dir)
        if not path:
            log = tr_dir / "feishu.log"
            detail = log.read_text(encoding="utf-8")[:160] if log.exists() else ""
            self.feishu_reason = f"飞书接口未返回转写：{detail}"
            return None
        try:
            segs = load_any(path)
        except Exception:  # noqa: BLE001
            self.feishu_reason = "飞书转写解析失败"
            return None
        if not segs:
            self.feishu_reason = "飞书转写为空"
            return None
        out = tr_dir / "transcript.json"
        import json as _json
        self.write(ctx, out, _json.dumps(to_payload(segs, source=path.name),
                                         ensure_ascii=False, indent=2))
        return out

    def _try_fetch(self, ctx: Context, tr_dir):
        """policy.auto_transcribe 开启时，尝试抓 YouTube 字幕。"""
        self.fetch_reason = ""
        if ctx.dry_run:
            return None
        try:
            import json as _json
            policy = _json.loads((ctx.orch_dir / "policy.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            policy = {}
        if not (policy.get("auto_transcribe") or {}).get("enabled"):
            self.fetch_reason = "policy 未开启 auto_transcribe"
            return None

        from ..fetcher import fetch_subtitles, looks_like_youtube, yt_dlp_version
        if not looks_like_youtube(ctx.run.source.url):
            self.fetch_reason = "非 YouTube 链接，跳过自动抓取"
            return None

        ver = yt_dlp_version()
        if ver is None:
            self.fetch_reason = "yt-dlp 不可用（未安装或不在 PATH）"
            return None

        vtt = fetch_subtitles(ctx.run.source.url, tr_dir)
        if not vtt:
            self.fetch_reason = f"yt-dlp {ver} 未取到字幕，详见 转写/yt-dlp.log"
            return None
        try:
            segs = load_any(vtt)
        except Exception:  # noqa: BLE001
            return None
        if not segs:
            return None
        out = tr_dir / "transcript.json"
        import json as _json
        self.write(ctx, out, _json.dumps(to_payload(segs, source=vtt.name),
                                         ensure_ascii=False, indent=2))
        return out

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
