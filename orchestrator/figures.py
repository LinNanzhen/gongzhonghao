#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配图纯函数：引语解析 → 选点 → 抽帧 → 锐化 → 封面 → 图源台账。

职责边界：本模块不碰状态机、不碰 Context、不联网。
- 文本函数（纯）：解析正文引语块、按章节分布选点、插图、封面标题切行、生成台账；
- 媒体函数（只读写显式传入的路径）：探测 ffmpeg、按时间戳抽帧、缩放 + USM、封面合成；
- 字体函数：在 TTC 内扫描**简体（SC）**字面，避开 index=0 常见是 JP 的坑。

失败约定：抽帧返回 ``(ok, err)`` 不抛异常；图像/字体函数在 Pillow 缺失时抛
``FigureError``，其余 I/O 错误原样抛出，由 illustrator 逐项捕获降级。
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

try:  # 降级契约：没有 Pillow 也要能 import，配图环节整体跳过
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat

    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover - 依赖缺失只在异常环境走到
    Image = ImageDraw = ImageFilter = ImageFont = ImageOps = ImageStat = None  # type: ignore[assignment]
    PIL_AVAILABLE = False


# ---------------------------------------------------------------- 常量

FIGURE_WIDTH = 1000                      # 正文图发布宽度（只缩不放）
FIGURE_QUALITY = 88                      # JPEG q88 progressive（与手工样例一致）
FIGURE_USM = (1.5, 90, 2)                # UnsharpMask(radius, percent, threshold)
COVER_SIZE = (900, 383)                  # 公众号首图 2.35:1
COVER_QUALITY = 90
COVER_MARGIN = 44

DEFAULT_FIGURES = 3                      # 每篇正文图数量


class FigureError(RuntimeError):
    """配图环节可预期的失败（Pillow 缺失、字体缺失、图片打不开等）。"""


def require_pil() -> None:
    if not PIL_AVAILABLE:
        raise FigureError("Pillow 未安装，无法处理图片")


# ---------------------------------------------------------------- 文本：引语块


@dataclass(frozen=True)
class QuoteBlock:
    """正文里一段可定位的原话。

    ``end_line`` 是整块（含 ``—— 原视频`` 那行）最后一行的 0 基行号，
    插图紧跟在它后面；``section_no`` 是所在 ``##`` 章节的序号（0 起）。
    """

    order: int
    section_no: int
    section_title: str
    speaker: str
    text: str
    timestamp: str
    seconds: int | None
    end_line: int

    @property
    def locatable(self) -> bool:
        """带时间戳才能抽帧；没有时间戳的引语保留但不做图。"""
        return self.seconds is not None


_TIMECODE_RX = re.compile(r"(?<!\d)(\d{1,3}):([0-5]\d)(?::([0-5]\d))?(?!\d)")
_ATTRIBUTION_RX = re.compile(r"^>\s*[—–-]{1,2}\s*(.+)$")
_SPEAKER_PREFIX_RX = re.compile(
    r"^(?P<spk>[A-Za-z][A-Za-z0-9._-]{0,9}|[\u4e00-\u9fff·]{1,6})[：:]\s*(?P<text>.+)$")
_HEADING_RX = re.compile(r"^##\s+(.+?)\s*$")
_H1_RX = re.compile(r"^#\s+(.+?)\s*$")
_MD_MARKS_RX = re.compile(r"[*_`~]+")
_HTML_RX = re.compile(r"<[^>]+>")
_TS_MARKERS = ("原视频", "截自", "视频", "时间戳")
_ATTRIBUTION_WORDS = {"原视频", "视频", "路演", "访谈", "节目", "截自", "原片", "素材", "时间戳"}


def plain_text(value: str) -> str:
    """去掉 markdown 强调符 / 行内 HTML，压平空白。"""
    if not value:
        return ""
    text = _HTML_RX.sub("", str(value))
    text = _MD_MARKS_RX.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_timecode(raw: str) -> tuple[str, int] | None:
    """把 ``26:59`` / ``01:17:24`` / ``62:37-63:01`` 归一成 ``(显示文本, 秒)``。

    - ``mm:ss`` 保留分（分可 >59，素材里就有 ``62:37`` 这种写法）；
    - ``hh:mm:ss`` 归一成两位小时；
    - 区间只取起点（抽帧从第一句开始）。
    """
    if not raw:
        return None
    m = _TIMECODE_RX.search(str(raw))
    if not m:
        return None
    a, b, c = m.group(1), m.group(2), m.group(3)
    if c is None:
        seconds = int(a) * 60 + int(b)
        text = f"{int(a):02d}:{b}"
    else:
        seconds = int(a) * 3600 + int(b) * 60 + int(c)
        text = f"{int(a):02d}:{b}:{c}"
    return text, seconds


def _attribution_name(rest: str) -> str:
    """从 ``—— 原视频 26:59`` / ``—— Dario Amodei，CEO，路演 00:30`` 里取说话人。"""
    cleaned = _TIMECODE_RX.sub("", rest or "").strip()
    first = re.split(r"[，,；;]", cleaned, maxsplit=1)[0].strip(" ·—–-")
    first = re.sub(r"[（(].*?[)）]", "", first).strip()
    if not first or len(first) > 24:
        return ""
    if first in _ATTRIBUTION_WORDS:
        return ""
    if first.split()[0] in _ATTRIBUTION_WORDS and len(first.split()) <= 2:
        return ""
    return first


def _split_speaker(body: str) -> tuple[str, str]:
    """``YP：原话`` → (``YP``, ``原话``)；不是说话人前缀返回 ("", body)。"""
    m = _SPEAKER_PREFIX_RX.match(body)
    if not m:
        return "", body.strip()
    speaker = m.group("spk").strip()
    if speaker in _ATTRIBUTION_WORDS:
        return "", body.strip()
    return speaker, m.group("text").strip()


def _quote_text(lines: Sequence[str]) -> tuple[str, str]:
    """从引语块内容行里取（说话人, 中文原话）。

    中英对照时跳过 ``EN|`` 行；同一块里多句时取第一句中文。
    """
    speaker = ""
    text = ""
    for raw in lines:
        body = re.sub(r"^>\s?", "", raw).strip()
        if not body or _ATTRIBUTION_RX.match(raw):
            continue
        if re.match(r"^(EN|英|English)\s*[|｜]", body, re.I):
            continue
        head, rest = _split_speaker(body)
        if head:
            if not speaker:
                speaker = head
            if not text:
                text = rest
        elif not text:
            text = body
    return speaker, plain_text(text)


def _block_timestamp(lines: Sequence[str]) -> tuple[str, int | None]:
    """优先取 ``—— 原视频`` 归属行的第一个时间码，其次块内带「原视频/截自」的行。"""
    attribution = ""
    for raw in lines:
        m = _ATTRIBUTION_RX.match(raw)
        if m:
            attribution = m.group(1)
    candidates: list[str] = []
    if attribution:
        candidates.append(attribution)
    for raw in lines:
        body = re.sub(r"^>\s?", "", raw).strip()
        if body and any(mark in body for mark in _TS_MARKERS):
            candidates.append(body)
    for cand in candidates:
        parsed = parse_timecode(cand)
        if parsed:
            return parsed
    return "", None


def quote_blocks(md_text: str) -> list[QuoteBlock]:
    """解析正文里所有引语块（``>`` 开头、带时间戳的原话）。

    说话人来源优先级：引语行的 ``缩写：`` 前缀 > 归属行里的姓名。
    没有任何正文的块（纯标题/空引用）直接丢弃。
    """
    lines = (md_text or "").splitlines()
    blocks: list[QuoteBlock] = []
    section_no = -1
    section_title = ""
    i = 0
    order = 0
    while i < len(lines):
        line = lines[i]
        heading = _HEADING_RX.match(line)
        if heading:
            section_no += 1
            section_title = plain_text(heading.group(1))
            i += 1
            continue
        if not line.lstrip().startswith(">"):
            i += 1
            continue
        start = i
        while i < len(lines) and lines[i].lstrip().startswith(">"):
            i += 1
        end = i - 1
        raw_block = lines[start:end + 1]
        speaker, text = _quote_text(raw_block)
        timestamp, seconds = _block_timestamp(raw_block)
        if not text:
            order += 1
            continue
        if not speaker:
            for raw in raw_block:
                m = _ATTRIBUTION_RX.match(raw)
                if m:
                    speaker = _attribution_name(m.group(1))
                    if speaker:
                        break
        blocks.append(QuoteBlock(
            order=order, section_no=section_no, section_title=section_title,
            speaker=speaker, text=text, timestamp=timestamp, seconds=seconds,
            end_line=end,
        ))
        order += 1
    return blocks


def _spread_indexes(total: int, count: int) -> list[int]:
    """在 ``[0, total)`` 上均匀取 ``count`` 个互不相同的下标（含首尾）。"""
    if count <= 0:
        return []
    if total <= count:
        return list(range(total))
    if count == 1:
        return [0]
    out: list[int] = []
    step = (total - 1) / (count - 1)
    for i in range(count):
        idx = int(i * step + 0.5)
        while idx in out:
            idx += 1
        out.append(min(idx, total - 1))
    return sorted(set(out))


def _pick_from_group(group: Sequence[QuoteBlock], used_ts: set[str]) -> QuoteBlock | None:
    for quote in group:
        if not quote.locatable or quote.timestamp in used_ts:
            continue
        if len(quote.text) >= 6:
            return quote
    for quote in group:
        if quote.locatable and quote.timestamp not in used_ts:
            return quote
    return None


def select_quotes(blocks: Sequence[QuoteBlock], count: int = DEFAULT_FIGURES) -> list[QuoteBlock]:
    """按章节分布选 ``count`` 处引语（文档顺序返回）。

    规则：
    1. 先按章节均匀取章（首章 + 中间 + 末章），每章取第一句可定位原话；
    2. 章节数不够时，从全部候选里按文档位置均匀补位；
    3. 同一时间戳只取一次（避免同一帧被讲两遍）。
    """
    if count <= 0:
        return []
    candidates = [b for b in blocks if b.locatable]
    if not candidates:
        return []

    groups: list[list[QuoteBlock]] = []
    for quote in candidates:
        if not groups or groups[-1][0].section_no != quote.section_no:
            groups.append([quote])
        else:
            groups[-1].append(quote)

    picks: list[QuoteBlock] = []
    used_ts: set[str] = set()
    if len(groups) >= count:
        for gi in _spread_indexes(len(groups), count):
            quote = _pick_from_group(groups[gi], used_ts)
            if quote:
                picks.append(quote)
                used_ts.add(quote.timestamp)

    if len(picks) < count:
        picks_set = {id(q) for q in picks}
        pool = [b for b in candidates if id(b) not in picks_set]
        for gi in _spread_indexes(len(pool), count - len(picks)):
            quote = pool[gi]
            if quote.timestamp in used_ts:
                continue
            picks.append(quote)
            used_ts.add(quote.timestamp)

    return sorted(picks, key=lambda b: (b.end_line, b.order))


def _clip(text: str, limit: int) -> str:
    """截前 ``limit`` 个字；被截断时补省略号（截断处标点先去掉）。"""
    text = re.sub(r"\s+", "", str(text or ""))
    if len(text) <= limit:
        return text
    head = text[:limit].rstrip("，,。；;、：: ")
    return (head or text[:limit]) + "…"


def caption(speaker: str, text: str, timestamp: str) -> str:
    """图注：``▲ 说话人：引语前 20 字（截自原视频 mm:ss）``。"""
    head = _clip(text, 20)
    who = f"{plain_text(speaker)}：" if plain_text(speaker) else ""
    return f"▲ {who}{head}（截自原视频 {timestamp}）"


def figure_purpose(section_title: str, speaker: str, text: str) -> str:
    """台账「用途」列：用所在章节标题 + 是谁的哪句话说明插这张图干什么。"""
    title = plain_text(section_title) or "正文"
    who = plain_text(speaker) or "原话"
    return f"{title}：{who}「{_clip(text, 12)}」"


def insert_figure(md_text: str, end_line: int, caption_text: str, image_rel: str) -> str:
    """在引语块后插入 ``![图注](图片/fig_0N.jpg)``，前后各留一个空行。

    幂等：同一 ``image_rel`` 已在正文里出现时原样返回。
    """
    if not image_rel:
        return md_text
    if f"]({image_rel})" in (md_text or ""):
        return md_text
    lines = (md_text or "").splitlines()
    pos = max(0, min(int(end_line) + 1, len(lines)))
    marker = f"![{caption_text}]({image_rel})"
    if pos < len(lines) and not lines[pos].strip():
        lines[pos + 1:pos + 1] = [marker, ""]
    else:
        lines[pos:pos] = ["", marker, ""]
    body = "\n".join(lines)
    return body + "\n" if md_text.endswith("\n") or not md_text else body


def insert_figures(md_text: str, inserts: Iterable[tuple[int, str, str]]) -> str:
    """批量插图：``(end_line, image_rel, caption)``；按行号从后往前插，行号不串位。"""
    text = md_text
    for end_line, image_rel, caption_text in sorted(inserts, key=lambda x: -x[0]):
        text = insert_figure(text, end_line, caption_text, image_rel)
    return text


# ---------------------------------------------------------------- 文本：标题


_SEP_CHARS = "｜|，,：:；;。！!？?"


def split_title(title: str, max_lines: int = 2) -> list[str]:
    """封面标题切两行：优先在标点处切（取最平衡的位置），没有标点按中点切。"""
    text = plain_text(title).strip()
    if not text:
        return []
    if max_lines <= 1 or len(text) < 4:
        return [text]

    best: tuple[int, int, str, str] | None = None
    for idx, ch in enumerate(text):
        if ch not in _SEP_CHARS:
            continue
        left, right = text[:idx].strip(), text[idx + 1:].strip()
        if len(left) < 2 or len(right) < 2:
            continue
        score = abs(len(left) - len(right))
        if best is None or score < best[0]:
            best = (score, idx, left, right)
    if best:
        return [best[2], best[3]]

    # 无标点：英文/中英混排优先在空格断行（取最平衡的空格，避免把短词挤成一行）
    spaces = [i for i, ch in enumerate(text) if ch == " "]
    word_splits = []
    for idx in spaces:
        left, right = text[:idx].strip(), text[idx + 1:].strip()
        if len(left) >= 3 and len(right) >= 3:
            word_splits.append((abs(len(left) - len(right)), left, right))
    if word_splits:
        return [min(word_splits, key=lambda item: item[0])[1],
                min(word_splits, key=lambda item: item[0])[2]]
    mid = len(text) // 2
    return [text[:mid].strip(), text[mid:].strip()]


def parse_h1(md_text: str) -> str:
    """取正文 H1 作封面标题；没有返回空串。"""
    for line in (md_text or "").splitlines():
        if line.strip().startswith("# "):
            return plain_text(_H1_RX.match(line.strip()).group(1)) if _H1_RX.match(line.strip()) else ""
    return ""


# ---------------------------------------------------------------- 文本：转写稿补时间戳


_TRANSCRIPT_SPEAKER_RX = re.compile(r"^(?:说话人|Speaker)\s*\d+$", re.I)
_NORM_DROP_RX = re.compile(r"[\s，,。；;、：:！!？?…\-—–\"'“”‘’（）()\[\]]+")


def format_seconds(seconds: float | int) -> str:
    """秒 → ``mm:ss`` / ``hh:mm:ss``（转写稿的 start 是秒数）。"""
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        return ""
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def load_transcript(path: Path) -> list[dict]:
    """读 ``转写/transcript.json`` 的 segments；读不了返回空列表（配图照常降级）。"""
    import json

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        segments = data.get("segments") or []
    elif isinstance(data, list):
        segments = data
    else:
        return []
    return [seg for seg in segments if isinstance(seg, dict) and str(seg.get("text") or "").strip()]


def _norm_match(value: str) -> str:
    return _NORM_DROP_RX.sub("", str(value or "")).lower()


def find_transcript_segment(text: str, segments: Sequence[dict]) -> dict | None:
    """用引语前若干字在转写稿里找对应分段（模糊到去标点/空格/大小写）。"""
    norm = _norm_match(text)
    if len(norm) < 4:
        return None
    for probe_len in (min(len(norm), 16), 12, 8, 6):
        if probe_len < 4:
            continue
        probe = norm[:probe_len]
        for seg in segments:
            if probe and probe in _norm_match(str(seg.get("text") or "")):
                return seg
    return None


def locate_quotes(blocks: Sequence[QuoteBlock], segments: Sequence[dict]) -> list[QuoteBlock]:
    """给没有时间戳的引语块补时间戳（/补说话人）。

    正文里的 ``—— 原视频 mm:ss`` 是首选；缺归属行时才回落到转写稿，
    只做「原文在前 16/12/8/6 字能对上」的保守匹配，对不上就保持不可定位。
    """
    if not segments:
        return list(blocks)
    from dataclasses import replace

    out: list[QuoteBlock] = []
    for block in blocks:
        if block.locatable:
            out.append(block)
            continue
        seg = find_transcript_segment(block.text, segments)
        if not seg:
            out.append(block)
            continue
        start = seg.get("start")
        timestamp = format_seconds(start)
        if not timestamp:
            out.append(block)
            continue
        speaker = block.speaker
        raw_speaker = str(seg.get("speaker") or "").strip()
        if not speaker and raw_speaker and not _TRANSCRIPT_SPEAKER_RX.match(raw_speaker):
            speaker = raw_speaker
        try:
            seconds = int(round(float(start)))
        except (TypeError, ValueError):
            out.append(block)
            continue
        out.append(replace(block, speaker=speaker, timestamp=timestamp, seconds=seconds))
    return out


# ---------------------------------------------------------------- 媒体：ffmpeg


def find_ffmpeg() -> str | None:
    """探测 ffmpeg；找不到返回 None（配图应降级而不是报错）。"""
    return shutil.which("ffmpeg")


def extract_frame(video: Path, seconds: int, out_path: Path, *,
                  ffmpeg: str | None = None, timeout: int = 120) -> tuple[bool, str]:
    """按时间戳抽一帧。

    ``-ss`` 放在 ``-i`` 之前（快速 seek），``-frames:v 1 -q:v 2`` 取单帧高质量 JPEG。
    失败返回 ``(False, 原因)``，不抛异常——单张图失败不该中断整篇。
    """
    exe = ffmpeg or find_ffmpeg()
    if not exe:
        return False, "找不到 ffmpeg"
    if not video or not Path(video).exists():
        return False, f"原视频不存在：{video}"
    out_path = Path(out_path)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"无法创建图片目录：{exc}"
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-y",
           "-ss", str(int(seconds)), "-i", str(video),
           "-frames:v", "1", "-q:v", "2", str(out_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"抽帧超时（{timeout}s）"
    except OSError as exc:
        return False, f"ffmpeg 无法执行：{exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, (detail[-1] if detail else f"ffmpeg 退出码 {proc.returncode}")
    if not out_path.exists() or out_path.stat().st_size <= 0:
        return False, "ffmpeg 未产出帧（时间戳可能超出视频长度）"
    return True, ""


# ---------------------------------------------------------------- 媒体：Pillow


def frame_stats(path: Path) -> tuple[float, float]:
    """返回 (亮度均值, 亮度标准差)，用于识别黑帧 / 纯色空镜。

    按 BT.601 亮度加权，0–255 量纲；样本取整帧（1000px 级图片足够快）。
    """
    require_pil()
    with Image.open(path) as opened:
        img = opened.convert("RGB")
        stat = ImageStat.Stat(img)
    weights = (0.299, 0.587, 0.114)
    mean = sum(m * w for m, w in zip(stat.mean, weights))
    std = sum(s * w for s, w in zip(stat.stddev, weights))
    return float(mean), float(std)


def frame_is_usable(path: Path, *, min_mean: float = 28.0,
                    min_std: float = 14.0) -> bool:
    """抽到的帧是否可用：太暗（转场/黑场）或太均匀（纯色）判为不可用。

    判断失败（读不了图）时返回 True——宁可放过，不因质检辅路误杀正文图。
    """
    try:
        mean, std = frame_stats(path)
    except Exception:  # noqa: BLE001 - 读图失败不阻断配图
        return True
    return mean >= min_mean and std >= min_std


def render_figure(src: Path, dst: Path, *, max_width: int = FIGURE_WIDTH,
                  quality: int = FIGURE_QUALITY,
                  usm: tuple[float, int, int] = FIGURE_USM) -> tuple[int, int]:
    """原帧 → 1000px 宽（只缩不放）→ USM 锐化 → JPEG q88 progressive。"""
    require_pil()
    dst = Path(dst)
    with Image.open(src) as opened:
        img = opened.convert("RGB")
        width, height = img.size
        if max_width and width > max_width:
            new_h = max(1, int(round(height * max_width / width)))
            img = img.resize((max_width, new_h), Image.LANCZOS)
        img = img.filter(ImageFilter.UnsharpMask(*usm))
        dst.parent.mkdir(parents=True, exist_ok=True)
        img.save(dst, "JPEG", quality=quality, progressive=True, optimize=True)
        return img.size


def _crop_cover(img: "Image.Image", size: tuple[int, int] = COVER_SIZE) -> "Image.Image":
    """按 2.35:1 中心裁切（垂直方向略偏上，保住人脸）。"""
    return ImageOps.fit(img, size, Image.LANCZOS, centering=(0.5, 0.42))


def _bottom_gradient_mask(size: tuple[int, int], *, strength: int = 215,
                          start_ratio: float = 0.30) -> "Image.Image":
    """底部渐变压暗遮罩：从 ``start_ratio`` 处开始加深到 0..strength。"""
    width, height = size
    column = Image.new("L", (1, height))
    px = column.load()
    for y in range(height):
        ratio = (y / max(1, height - 1) - start_ratio) / (1.0 - start_ratio)
        ratio = min(1.0, max(0.0, ratio))
        px[0, y] = int(strength * (ratio ** 1.6))
    return column.resize((width, height))


# 字体候选：按优先级排列（Noto CJK 在前；mac 没装 Noto 时退到系统简体字面）
_FONT_FILENAMES = (
    "NotoSansCJK-Regular.ttc", "NotoSansCJKsc-Regular.otf", "NotoSansSC-Regular.otf",
    "NotoSansCJK-Bold.ttc", "NotoSansCJKsc-Bold.otf", "NotoSansSC-Bold.otf",
    "NotoSansCJK-Medium.ttc", "NotoSansCJK-Regular.otf", "NotoSansSC-Medium.otf",
    "SourceHanSansSC-Regular.otf", "SourceHanSans-Regular.ttc",
    "SourceHanSansCN-Regular.otf",
    "STHeiti Light.ttc", "STHeiti Medium.ttc", "Songti.ttc",
    "Hiragino Sans GB.ttc", "Arial Unicode.ttf",
)
_FONT_GLOBS = ("*NotoSans*CJK*", "*NotoSansSC*", "*SourceHanSans*")
_FONT_DIRS = (
    "/usr/share/fonts/opentype/noto",
    "/usr/share/fonts/truetype/noto",
    "/usr/share/fonts/truetype/noto-cjk",
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "~/.fonts",
    "~/.local/share/fonts",
    "~/Library/Fonts",
    "/Library/Fonts",
    "/System/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
)
_PINGFANG_GLOB = "/System/Library/AssetsV2/com_apple_MobileAsset_Font8/*/AssetData/PingFang.ttc"
_FONT_SUFFIXES = {".ttf", ".ttc", ".otf", ".otc"}


@dataclass(frozen=True)
class FontSpec:
    """一个可直接给 PIL 用的字体文件 + TTC 内字面序号。"""

    path: str
    index: int
    family: str
    style: str

    @property
    def name(self) -> str:
        return f"{self.family} {self.style}".strip()


def _iter_font_files(paths: Iterable[Path | str]) -> list[Path]:
    """把文件/目录/glob 展开成字体文件列表（去重、保序）。"""
    out: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = str(path)
        if key in seen or not path.is_file() or path.suffix.lower() not in _FONT_SUFFIXES:
            return
        seen.add(key)
        out.append(path)

    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            for child in sorted(path.iterdir()):
                add(child)
        elif path.is_file():
            add(path)
    return out


def _candidate_font_files(extra_paths: Sequence[Path | str] = (),
                          env: dict[str, str] | None = None) -> list[Path]:
    """按优先级列出候选字体文件：GZH_FONT > 案例目录 > 系统常见路径。"""
    env = dict(os.environ if env is None else env)
    candidates: list[Path] = []
    override = (env.get("GZH_FONT") or "").strip()
    if override:
        candidates += _iter_font_files([override])
    candidates += _iter_font_files(extra_paths)
    dirs = [Path(d).expanduser() for d in _FONT_DIRS]
    for directory in dirs:
        if not directory.is_dir():
            continue
        for name in _FONT_FILENAMES:
            candidates += _iter_font_files([directory / name])
        for pattern in _FONT_GLOBS:
            candidates += _iter_font_files(sorted(directory.glob(pattern)))
    candidates += _iter_font_files(sorted(Path("/").glob(_PINGFANG_GLOB.lstrip("/"))))
    return candidates


def _face_score(family: str, style: str) -> int:
    """给 TTC 内一个字面打分：简体（SC）优先，明确压低 JP/KR/TC/HK。"""
    fam = family or ""
    sty = style or ""
    score = 0
    if re.search(r"(?:^|\s)SC(?:\s|$)", fam) or "CJK SC" in fam or "Simplified" in fam \
            or re.search(r"(?:^|\s)CN(?:\s|$)", fam):
        score += 100
    if "CJK" in fam or "Noto" in fam:
        score += 5
    if re.search(r"(?:^|\s)(?:JP|KR|TC|HK)(?:\s|$)", fam):
        score -= 80
    if "Mono" in fam:
        score -= 5
    if "Italic" in sty or "Oblique" in sty:
        score -= 5
    if any(word in sty for word in ("Bold", "Medium", "SemiBold", "Semibold")):
        score += 2
    return score


def _pick_face(path: Path, *, fallback_ok: bool = False) -> FontSpec | None:
    """在 TTC/TTF 内扫字面：找 SC；``fallback_ok`` 时允许退到任意中文可用字面。"""
    require_pil()
    best: tuple[int, int, str, str] | None = None
    fallback: tuple[int, int, str, str] | None = None
    for index in range(24):
        try:
            font = ImageFont.truetype(str(path), size=16, index=index)
        except (OSError, ValueError):
            break
        try:
            family, style = font.getname()
        except Exception:  # noqa: BLE001 - 个别字体元数据异常，按无名处理
            family, style = "", ""
        score = _face_score(family, style)
        if score >= 100 and (best is None or score > best[0]):
            best = (score, index, family, style)
        if fallback is None and score > -40:
            fallback = (score, index, family, style)
    if best is not None:
        return FontSpec(str(path), best[1], best[2], best[3])
    if fallback_ok and fallback is not None:
        return FontSpec(str(path), fallback[1], fallback[2], fallback[3])
    return None


def find_cjk_font(extra_paths: Sequence[Path | str] = (),
                  env: dict[str, str] | None = None) -> FontSpec | None:
    """找一款**简体中文**字面。

    TTC 里 index=0 常是 JP（NotoSansCJK）或 TC（STHeiti），直接 index=0 会让
    「门」「骨」这类字形变体变形；这里逐字面扫描 ``getname()``，优先含 SC 的。
    找不到返回 None（调用方跳过封面，不 block 流水线）。
    """
    require_pil()
    files = _candidate_font_files(extra_paths, env)
    for path in files:
        spec = _pick_face(path)
        if spec is not None:
            return spec
    for path in files:
        spec = _pick_face(path, fallback_ok=True)
        if spec is not None:
            return spec
    return None


def load_font(spec: FontSpec, size: int) -> "ImageFont.FreeTypeFont":
    """按字面加载指定字号。"""
    require_pil()
    return ImageFont.truetype(spec.path, size=size, index=spec.index)


def render_cover(frame: Path, out_path: Path, title: str, font: FontSpec, *,
                 footer: str = "", size: tuple[int, int] = COVER_SIZE,
                 quality: int = COVER_QUALITY) -> dict:
    """900×383 封面：原帧裁切 → 底部渐变压暗 → 两行白字标题（左对齐）。

    返回元信息 dict（``size`` / ``title_lines`` / ``font_size`` / ``footer``）。
    """
    require_pil()
    lines = split_title(title)
    if not lines:
        raise FigureError("封面标题为空")
    width, height = size
    with Image.open(frame) as opened:
        base = _crop_cover(opened.convert("RGB"), size)
    base.paste(Image.new("RGB", size, (0, 0, 0)), (0, 0), _bottom_gradient_mask(size))

    max_width = width - 2 * COVER_MARGIN
    # 版式参照样例封面：两行标题压在画面下三分之一，字号随最长一行收缩
    longest = max(len(line) for line in lines)
    font_size = 46 if longest <= 5 else 42
    while font_size > 22:
        probe = load_font(font, font_size)
        line_height = int(round(font_size * 1.05))
        fits = all(probe.getlength(line) <= max_width for line in lines)
        if fits and line_height * len(lines) <= height * 0.34:
            break
        font_size -= 2
    face = load_font(font, font_size)
    line_height = int(round(font_size * 1.05))
    has_footer = bool(footer)
    top = max(10, height - (50 if has_footer else 30) - line_height * len(lines))

    draw = ImageDraw.Draw(base)
    stroke = max(1, font_size // 40)
    for i, line in enumerate(lines):
        draw.text((COVER_MARGIN, top + i * line_height), line, font=face,
                  fill=(255, 255, 255), stroke_width=stroke, stroke_fill=(255, 255, 255))
    if has_footer:
        footer_face = load_font(font, 16)
        draw.text((COVER_MARGIN, height - 26), footer, font=footer_face, fill=(214, 208, 198))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    base.save(out_path, "JPEG", quality=quality, progressive=True, optimize=True)
    return {"size": base.size, "title_lines": lines, "font_size": font_size, "footer": footer}


# ---------------------------------------------------------------- 台账


def sha256_file(path: Path) -> str:
    """文件 SHA256（台账里用于核对发布副本）。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render_ledger(*, guest: str, date_str: str, media: str,
                  figures: Sequence[dict], cover: dict | None = None,
                  notes: Sequence[str] = ()) -> str:
    """生成 ``图源_{date}_{guest}.md``：时间戳 / 文件 / 处理 / SHA256 台账（纯文本）。"""
    lines: list[str] = [f"# 图源记录 · {guest}（{date_str}）", ""]
    lines.append(f"> 素材：`{media}`。")
    lines.append("> 处理：ffmpeg 原片抽帧 → 缩放到 1000px 宽（不放大）→ USM 锐化 → "
                 "JPEG q88 progressive。")
    if cover:
        lines.append(f"> 封面：{cover.get('timestamp', '')} 帧裁 900×383，底部渐变压暗 + "
                     "两行标题（标题取自正文 H1，SC 字族）。")
    lines += ["", "| 图 | 时间戳 | 原视频 | 文件 | 处理 | 用途 | SHA256 |",
              "|---|---|---|---|---|---|---|"]
    for fig in figures:
        lines.append(
            f"| {fig.get('name', '')} | {fig.get('timestamp', '')} | {media} | "
            f"{fig.get('file', '')} | {fig.get('process', '1000px + USM')} | "
            f"{fig.get('purpose', '')} | {fig.get('sha256', '')} |")
    if cover:
        lines.append(
            f"| 封面 | {cover.get('timestamp', '')} | {media} | {cover.get('file', '')} | "
            f"900×383 裁切 + 渐变压暗 + 两行标题（SC 字族） | 公众号首图 | "
            f"{cover.get('sha256', '')} |")
    lines += ["", "## 说明", ""]
    if notes:
        for note in notes:
            lines.append(f"- {note}")
    else:
        lines.append("- 正文图为同一机位的原视频帧，图注标注了对应引语与时间戳；"
                     "未裁切人物、未替换背景。")
    lines.append("- 正文配图与封面都保留原图母本（`图片/` 下为 1000px 发布副本），"
                 "需要更高清时可从原视频重抽。")
    return "\n".join(lines) + "\n"
