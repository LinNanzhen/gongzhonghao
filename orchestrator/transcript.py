#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""转写稿解析：飞书妙记 / SRT / 纯文本 → 统一的 transcript.json。

飞书妙记「文字记录」导出的格式：
    @说话人 1  00:01
    内容……
    @说话人 2  00:28
    内容……

命令行：
    python3 -m orchestrator.transcript 飞书逐字稿.md -o transcript.json
    python3 -m orchestrator.transcript 输入.srt
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# @说话人 1  00:01（说话人名与时间之间至少一个空格）
FEISHU_LINE = re.compile(
    r"^\s*@(?P<spk>[^\s@][^\n]{0,40}?)\s{1,}(?P<t>\d{1,2}:\d{2}(?::\d{2})?)\s*$",
    re.M,
)
# SRT：00:00:01,000 --> 00:00:03,500
SRT_TIME = re.compile(
    r"^\s*(?P<a>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(?P<b>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})",
    re.M,
)
# 简单时间戳行：00:01 单独一行
BARE_TIME = re.compile(r"^\s*(?P<t>\d{1,2}:\d{2}(?::\d{2})?)\s*$", re.M)


def to_seconds(stamp: str) -> float:
    parts = [float(x) for x in stamp.replace(",", ".").split(":")]
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = 0.0, parts[0], parts[1]
    else:
        h, m, s = 0.0, 0.0, parts[0]
    return round(h * 3600 + m * 60 + s, 3)


def parse_feishu(text: str) -> list[dict]:
    """@说话人 N  MM:SS 分段。"""
    segments: list[dict] = []
    cur: dict | None = None
    buf: list[str] = []

    def flush():
        nonlocal cur, buf
        if cur is not None:
            body = "\n".join(buf).strip()
            cur["text"] = body
            if body:
                segments.append(cur)
        cur, buf = None, []

    for line in text.splitlines():
        m = FEISHU_LINE.match(line)
        if m:
            flush()
            cur = {"start": to_seconds(m.group("t")), "end": 0.0,
                   "speaker": m.group("spk").strip(), "text": ""}
        elif cur is not None:
            buf.append(line)
        # 头部无归属的说明文字直接丢弃
    flush()
    return _fill_end(segments)


def parse_srt(text: str) -> list[dict]:
    segments: list[dict] = []
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        m = SRT_TIME.match(lines[0])
        if not m:
            # 序号行 + 时间行
            if len(lines) > 1 and SRT_TIME.match(lines[1]):
                m = SRT_TIME.match(lines[1])
                lines = lines[1:]
            else:
                continue
        body = " ".join(lines[1:]).strip()
        if body:
            segments.append({"start": to_seconds(m.group("a")), "end": to_seconds(m.group("b")),
                             "speaker": "", "text": body})
    return segments


def parse_bare(text: str) -> list[dict]:
    """时间戳独占一行、正文在下的简式。"""
    segments: list[dict] = []
    cur = None
    buf: list[str] = []
    for line in text.splitlines():
        m = BARE_TIME.match(line)
        if m:
            if cur is not None:
                cur["text"] = "\n".join(buf).strip()
                if cur["text"]:
                    segments.append(cur)
            cur = {"start": to_seconds(m.group("t")), "end": 0.0, "speaker": "", "text": ""}
            buf = []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        cur["text"] = "\n".join(buf).strip()
        if cur["text"]:
            segments.append(cur)
    return _fill_end(segments)


def parse_vtt(text: str) -> list[dict]:
    """WebVTT（YouTube 自动字幕）。逐行状态机，避免时间行与文本间的空行切错。"""
    segments: list[dict] = []
    span: tuple[float, float] | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal span, buf
        if span and buf:
            txt = _clean_cue(" ".join(buf))
            if txt:
                segments.append({"start": span[0], "end": span[1], "speaker": "", "text": txt})
        span, buf = None, []

    for line in text.replace("\r\n", "\n").splitlines():
        if "-->" in line:
            flush()
            m = re.search(
                r"(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})",
                line)
            span = (to_seconds(m.group(1)), to_seconds(m.group(2))) if m else None
            continue
        if not line.strip():
            if buf:                      # 只有已收文本才结束 cue
                flush()
            continue
        if line.strip().startswith(("WEBVTT", "Kind:", "Language:", "NOTE", "STYLE", "REGION")):
            continue
        if span is not None:
            buf.append(line.strip())
    flush()
    return _dedupe_rolling(_fill_end(segments))


def _clean_cue(txt: str) -> str:
    txt = re.sub(r"<[^>]+>", "", txt)     # <c.colorE5E5E5>、<00:00:01.000>
    txt = txt.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", txt).strip()


def _dedupe_rolling(segments: list[dict]) -> list[dict]:
    """自动字幕会滚动累积（新句包含旧句），合并成一条。"""
    out: list[dict] = []
    for seg in segments:
        if out:
            prev = out[-1]
            if seg["text"] == prev["text"]:
                prev["end"] = max(prev["end"], seg["end"])
                continue
            if seg["text"].startswith(prev["text"]) and len(seg["text"]) > len(prev["text"]):
                prev["text"] = seg["text"]
                prev["end"] = max(prev["end"], seg["end"])
                continue
        out.append(dict(seg))
    return out


def parse_plain(text: str) -> list[dict]:
    """没有时间戳：整段作为一条，start=0。"""
    body = text.strip()
    return [{"start": 0.0, "end": 0.0, "speaker": "", "text": body}] if body else []


def _fill_end(segments: list[dict]) -> list[dict]:
    for i, seg in enumerate(segments):
        if seg.get("end"):
            continue
        nxt = segments[i + 1]["start"] if i + 1 < len(segments) else None
        seg["end"] = nxt if nxt is not None else round(seg["start"] + 30.0, 3)
    return segments


def parse_text(text: str) -> list[dict]:
    if text.lstrip().startswith("WEBVTT"):
        return parse_vtt(text)
    if FEISHU_LINE.search(text):
        return parse_feishu(text)
    if SRT_TIME.search(text):
        return parse_srt(text)
    if BARE_TIME.search(text):
        return parse_bare(text)
    return parse_plain(text)


def load_any(path: Path) -> list[dict]:
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".json":
        data = json.loads(raw)
        if isinstance(data, dict) and "segments" in data:
            return data["segments"]
        if isinstance(data, list):
            return data
    return parse_text(raw)


def to_payload(segments: list[dict], source: str = "") -> dict:
    return {
        "source": source,
        "count": len(segments),
        "duration": segments[-1]["end"] if segments else 0.0,
        "segments": segments,
    }


def convert(path: Path, out: Path | None = None) -> Path:
    segs = load_any(path)
    out = out or path.with_suffix(".json")
    out.write_text(json.dumps(to_payload(segs, source=str(path.name)),
                              ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="转写稿转 transcript.json")
    ap.add_argument("input")
    ap.add_argument("-o", "--out", default="")
    args = ap.parse_args(argv)
    src = Path(args.input)
    if not src.exists():
        print(f"找不到：{src}", file=sys.stderr)
        return 2
    out = convert(src, Path(args.out) if args.out else None)
    segs = json.loads(out.read_text(encoding="utf-8"))
    print(f"{out}  段数={segs['count']}  时长={segs['duration']:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
