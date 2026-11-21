#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地视频/音频转写 CLI —— ffmpeg 抽音轨 + faster-whisper(CTranslate2) int8

用法：
    stt.py 视频.mp4                       # 输出 transcript.json + .md 到同目录 转写/
    stt.py a.mp4 b.mov -o 转写/           # 批量
    stt.py 视频.mp4 -l en --model small   # 指定语种/模型
    stt.py 视频.mp4 --start 600 --duration 300   # 只转 10:00–15:00 片段
    stt.py 视频.mp4 --srt                 # 附带字幕文件
    stt.py "https://www.bilibili.com/video/BVxxx"          # 链接直取（yt-dlp）
    stt.py "https://youtu.be/xxx" -o 转写/                 # 有字幕就直取字幕，跳过转写
    stt.py "https://youtu.be/xxx" --no-subs                # 强制跑语音识别

产物（默认 <视频所在目录>/转写/）：
    <name>.transcript.json  [{"start","end","text"}]  与既有案例格式一致
    <name>.md               带 [hh:mm:ss] 的逐字稿
    <name>.srt              可选
    <name>.meta.json        链接模式：标题/上传者/发布日期/来源/字幕语种（喂 oneshot --title）
    <name>.封面.jpg          链接模式：视频封面（插图降级时的备选）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

MEDIA_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".flv",
             ".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus"}
MODEL_ALIAS = {
    "tiny": "tiny", "base": "base", "small": "small",
    "medium": "medium", "large": "large-v3", "large-v3": "large-v3",
    "turbo": "large-v3-turbo", "large-v3-turbo": "large-v3-turbo",
}


def fmt_ts(sec: float) -> str:
    sec = max(0.0, sec)
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def fmt_srt(sec: float) -> str:
    ms = int(round(max(0.0, sec) * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def extract_audio(src: Path, dst: Path, start: float | None, duration: float | None) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if start:
        cmd += ["-ss", f"{start}"]
    cmd += ["-i", str(src), "-vn", "-sn", "-dn"]
    if duration:
        cmd += ["-t", f"{duration}"]
    cmd += ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)]
    subprocess.run(cmd, check=True)


def resolve_model(name: str, model_dir: str | None) -> str:
    """优先用本地已缓存的 HF 快照（避免 xet/hub 联网与写缓存权限问题）。"""
    if Path(name).is_dir():
        return name
    alias = MODEL_ALIAS.get(name, name)
    roots = [model_dir] if model_dir else []
    roots += [os.environ.get("HF_HUB_CACHE") or "",
              os.path.expanduser("~/.cache/huggingface/hub")]
    repo = f"models--Systran--faster-whisper-{alias}"
    hits = []
    for root in filter(None, roots):
        snap = Path(root) / repo / "snapshots"
        if snap.is_dir():
            hits += [p for p in snap.iterdir() if (p / "model.bin").exists()]
    if hits:
        return str(max(hits, key=lambda p: p.stat().st_mtime))
    return alias  # 未缓存，交给 hub 下载


def load_wav(path: Path):
    """读 16k mono s16le wav 为 float32 数组（绕开 PyAV 版本兼容问题）。"""
    import wave
    import numpy as np
    with wave.open(str(path), "rb") as w:
        sr, n = w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if sr != 16000:
        raise RuntimeError(f"采样率应为 16000，实际 {sr}")
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def probe_duration(path: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, check=True).stdout.strip()
        return float(out)
    except Exception:
        return None


def transcribe_one(args, src: Path, model_cache: dict, *, meta: dict | None = None,
                   out_stem: str | None = None, title: str | None = None) -> dict:
    outdir = Path(args.output) if args.output else src.parent / "转写"
    outdir.mkdir(parents=True, exist_ok=True)
    stem = out_stem or src.stem
    label = title or src.name

    total = probe_duration(src)
    if total and args.start:
        total = max(0.0, total - args.start)
    if total and args.duration:
        total = min(total, args.duration)

    tmpdir = Path(tempfile.mkdtemp(prefix="stt-"))
    wav = tmpdir / "audio.wav"
    try:
        t0 = time.time()
        print(f"[{label}] 抽音轨 → 16k mono wav …", flush=True)
        extract_audio(src, wav, args.start, args.duration)
        dur = probe_duration(wav) or total or 0.0
        print(f"[{label}] 音频 {fmt_ts(dur)}，加载模型 {args.model} …", flush=True)

        key = (args.model, args.device, args.compute_type)
        if key not in model_cache:
            from faster_whisper import WhisperModel
            model_cache[key] = WhisperModel(
                resolve_model(args.model, args.model_dir),
                device=args.device,
                compute_type=args.compute_type,
                cpu_threads=args.threads,
            )
        model = model_cache[key]

        t_load = time.time()
        audio = load_wav(wav)
        segments, info = model.transcribe(
            audio,
            language=None if args.lang in ("auto", "") else args.lang,
            beam_size=args.beam,
            vad_filter=args.vad,
            vad_parameters=dict(min_silence_duration_ms=500),
            condition_on_previous_text=not args.no_context,
            initial_prompt=args.prompt or None,
        )
        print(f"[{label}] 语言={info.language}({info.language_probability:.2f}) "
              f"用时 {time.time()-t_load:.1f}s，开始转写 …", flush=True)

        items: list[dict] = []
        last_report = 0.0
        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue
            items.append({"start": round(seg.start, 2),
                          "end": round(seg.end, 2),
                          "text": text})
            if time.time() - last_report > 3:
                last_report = time.time()
                pct = 100 * seg.end / dur if dur else 0
                rtf = seg.end / max(1e-6, time.time() - t_load)
                print(f"\r  {fmt_ts(seg.end)}/{fmt_ts(dur)} {pct:5.1f}%  "
                      f"{rtf:.1f}×实时  {len(items)} 段", end="", flush=True)
        print("", flush=True)

        elapsed = time.time() - t0
        rtf = dur / elapsed if elapsed > 0 else 0

        note = (f"本地 faster-whisper({args.model}) 转写 · 语言 {info.language} · "
                f"时长 {fmt_ts(dur)} · 生成 {time.strftime('%Y-%m-%d %H:%M')}")
        if meta is not None:
            meta = dict(meta)
            meta["transcript_source"] = f"whisper:{args.model}"
            meta["segments"] = len(items)
        res = emit(args, outdir, stem, items, title=title or stem, note=note,
                   meta=meta, elapsed=elapsed, rtf=rtf)
        print(f"[{label}] 完成：{len(items)} 段，{fmt_ts(dur)} 音频 / "
              f"{elapsed/60:.1f} min（{rtf:.1f}×实时）→ {outdir}", flush=True)
        return res
    finally:
        if args.keep_audio:
            keep = outdir / f"{stem}.16k.wav"
            shutil.move(str(wav), keep)
        shutil.rmtree(tmpdir, ignore_errors=True)


def emit(args, outdir: Path, stem: str, items: list[dict], *, title: str, note: str,
         meta: dict | None = None, elapsed: float = 0.0, rtf: float = 0.0) -> dict:
    """统一产物：transcript.json / .md / 可选 .srt / 可选 .meta.json。"""
    js = outdir / f"{stem}.transcript.json"
    js.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")

    md = outdir / f"{stem}.md"
    lines = [f"# 逐字稿 · {title}", ""]
    if note:
        lines.append(f"> {note}")
    if meta:
        bits = []
        if meta.get("uploader"):
            bits.append(f"上传者 {meta['uploader']}")
        ud = str(meta.get("upload_date") or "")
        if len(ud) == 8 and ud.isdigit():
            bits.append(f"发布 {ud[:4]}-{ud[4:6]}-{ud[6:]}")
        if meta.get("duration"):
            bits.append(f"时长 {fmt_ts(float(meta['duration']))}")
        if bits:
            lines.append("> " + " · ".join(bits))
        if meta.get("webpage_url"):
            lines.append(f"> 来源 {meta['webpage_url']}")
    lines.append("")
    lines += [f"**[{fmt_ts(it['start'])}]** {it['text']}" for it in items]
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if args.srt:
        srt = outdir / f"{stem}.srt"
        blocks = [f"{i}\n{fmt_srt(it['start'])} --> {fmt_srt(it['end'])}\n{it['text']}\n"
                  for i, it in enumerate(items, 1)]
        srt.write_text("\n".join(blocks), encoding="utf-8")

    if meta:
        keep = ("title", "uploader", "channel", "upload_date", "duration", "webpage_url",
                "original_url", "language", "view_count", "like_count", "description",
                "transcript_source", "segments", "thumbnail_file")
        slim = {k: meta[k] for k in keep if meta.get(k) not in (None, "")}
        if isinstance(slim.get("description"), str) and len(slim["description"]) > 2000:
            slim["description"] = slim["description"][:2000] + "…"
        (outdir / f"{stem}.meta.json").write_text(
            json.dumps(slim, ensure_ascii=False, indent=1), encoding="utf-8")

    return {"src": str(js), "title": title, "segments": len(items),
            "duration": float(items[-1]["end"]) if items else 0.0,
            "elapsed": elapsed, "rtf": rtf, "outdir": str(outdir),
            "json": str(js), "md": str(md)}


# ---------- 链接模式：yt-dlp 取元数据 / 字幕 / 音轨 ----------

def ytdlp_path() -> str:
    cand = os.environ.get("YTDLP")
    if cand:
        return cand
    here = Path(__file__).resolve().parent
    for q in (here.parent / "ytdlp" / "yt-dlp", here / "yt-dlp"):
        if q.exists():
            return str(q)
    found = shutil.which("yt-dlp")
    if found:
        return found
    sys.exit("链接模式需要 yt-dlp：pip install yt-dlp 或 brew install yt-dlp，"
             "也可以用 YTDLP=/path/to/yt-dlp 指定")


COOKIES_FILE = ""     # --cookies 或 YTDLP_COOKIES 指定，给需要登录态的站点（B站 412/会员）

def _ytdlp_base() -> list[str]:
    cmd = [ytdlp_path(), *shlex.split(os.environ.get("YTDLP_ARGS", ""))]
    imp = os.environ.get("YTDLP_IMPERSONATE")
    if imp:
        cmd += ["--impersonate", imp]
    cookies = COOKIES_FILE or os.environ.get("YTDLP_COOKIES")
    if not cookies and os.environ.get("YTDLP_NO_COOKIES") != "1":
        default_ck = Path(__file__).resolve().parent.parent / "ytdlp" / "bili-cookies.txt"
        if default_ck.exists():
            cookies = str(default_ck)      # 有 B站登录态就默认带上，省得每次敲 --cookies
    if cookies:
        cmd += ["--cookies", os.path.expanduser(cookies)]
    return cmd


def _run_ytdlp(extra: list[str], what: str, fatal: bool = True) -> bool:
    try:
        subprocess.run(_ytdlp_base() + extra, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except FileNotFoundError:
        sys.exit(f"找不到 yt-dlp 可执行文件：{ytdlp_path()}")
    except subprocess.CalledProcessError as exc:
        tail = " / ".join((exc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-3:])
        if fatal:
            sys.exit(f"yt-dlp {what}失败（退出码 {exc.returncode}）：{tail}")
        print(f"[warn] yt-dlp {what}失败：{tail}", file=sys.stderr)
        return False
    return True


def _pick_file(workdir: Path, exts: set[str]) -> Path | None:
    got = [q for q in workdir.iterdir() if q.is_file() and q.suffix.lower() in exts]
    return max(got, key=lambda q: q.stat().st_size) if got else None


def fetch_meta(url: str) -> dict:
    try:
        proc = subprocess.run(
            _ytdlp_base() + ["--no-playlist", "-J", "--skip-download", "--no-warnings", url],
            check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        tail = " / ".join((exc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-3:])
        sys.exit(f"yt-dlp 读取元数据失败（退出码 {exc.returncode}）：{tail}")
    try:
        meta = json.loads(proc.stdout)
    except json.JSONDecodeError:
        sys.exit("yt-dlp 返回的元数据不是合法 JSON")
    if meta.get("_type") == "playlist" and meta.get("entries"):
        meta = meta["entries"][0] or {}
    return meta


def _match_lang(table: dict, want: str) -> str | None:
    keys = list(table)
    low = want.lower()
    for k in keys:
        if k.lower() == low:
            return k
    for k in keys:
        if k.lower().startswith(low + "-"):
            return k
    if "-" not in low:
        for k in keys:
            if k.lower().split("-")[0] == low:
                return k
    return None


def pick_sub(meta: dict, want: list[str]) -> tuple[str, str] | None:
    """挑字幕：优先人工字幕，其次自动字幕；语言按 视频语言 + want 顺序。"""
    manual = meta.get("subtitles") or {}
    auto = meta.get("automatic_captions") or {}
    lang = (meta.get("language") or "").strip()
    order = ([lang] if lang else []) + want
    for kind, table in (("字幕", manual), ("自动字幕", auto)):
        for w in order:
            if not w:
                continue
            k = _match_lang(table, w)
            if k:
                return kind, k
    return None


def fetch_subs(url: str, workdir: Path, lang: str, auto: bool) -> Path | None:
    out = str(workdir / "%(title).80B.%(ext)s")
    extra = ["--no-playlist", "--skip-download", "--sub-format", "srt",
             "--convert-subs", "srt", "--sub-langs", lang,
             "--write-auto-subs" if auto else "--write-subs",
             "--write-thumbnail", "--convert-thumbnails", "jpg",
             "-o", out, url]
    if not _run_ytdlp(extra, "抓字幕", fatal=False):
        return None
    return _pick_file(workdir, {".srt"})


def fetch_audio(url: str, workdir: Path) -> Path:
    out = str(workdir / "%(title).80B.%(ext)s")
    extra = ["--no-playlist", "-x", "--audio-format", "m4a",
             "--write-thumbnail", "--convert-thumbnails", "jpg", "-o", out, url]
    if not _run_ytdlp(extra, "抓音轨", fatal=False):
        # 封面/转码失败时退一步，只取音轨
        _run_ytdlp(["--no-playlist", "-x", "--audio-format", "m4a", "-o", out, url], "抓音轨")
    got = _pick_file(workdir, MEDIA_EXT)
    if not got:
        sys.exit(f"yt-dlp 没有产出音频文件：{url}")
    return got


_TAG_RE = re.compile(r"<[^>]+>")
_NOISE_RE = re.compile(r"^[\s♪\[\]\(\)\-–—.,!?…、，。？！]*$")
_T_SPAN = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{3})")


def _clean_sub_text(t: str) -> str:
    t = _TAG_RE.sub("", t)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"),
                 ("&lt;", "<"), ("&gt;", ">")):
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


def parse_srt(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    items: list[dict] = []
    for block in re.split(r"\n{2,}", raw):
        m = _T_SPAN.search(block)
        if not m:
            continue
        start = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) + int(m[4]) / 1000
        end = int(m[5]) * 3600 + int(m[6]) * 60 + int(m[7]) + int(m[8]) / 1000
        text = _clean_sub_text(" ".join(block[m.end():].strip("\n").split("\n")))
        if not text or _NOISE_RE.match(text):
            continue
        items.append({"start": round(start, 2), "end": round(end, 2), "text": text})
    # 自动字幕常见「滚动重复」：后一条把前一条包住时，用后一条覆盖
    merged: list[dict] = []
    for it in items:
        if merged:
            prev = merged[-1]
            if it["text"] == prev["text"]:
                prev["end"] = it["end"]
                continue
            if it["text"].startswith(prev["text"]) and len(it["text"]) > len(prev["text"]):
                prev["text"] = it["text"]
                prev["end"] = it["end"]
                continue
        merged.append(it)
    return merged


def clip_items(items: list[dict], start: float | None, duration: float | None) -> list[dict]:
    if not start and not duration:
        return items
    s0 = float(start or 0.0)
    s1 = s0 + float(duration) if duration else None
    out = []
    for it in items:
        if it["end"] <= s0:
            continue
        if s1 is not None and it["start"] >= s1:
            break
        e = it["end"] if s1 is None else min(it["end"], s1)
        out.append({"start": round(max(0.0, it["start"] - s0), 2),
                    "end": round(max(0.0, e - s0), 2), "text": it["text"]})
    return out


def _save_thumb(workdir: Path, outdir: Path, stem: str, meta: dict) -> dict:
    thumb = _pick_file(workdir, {".jpg", ".jpeg", ".png", ".webp"})
    if not thumb:
        return meta
    dst = outdir / f"{stem}.封面{thumb.suffix.lower()}"
    shutil.copy2(thumb, dst)
    meta = dict(meta)
    meta["thumbnail_file"] = str(dst)
    print(f"      封面 → {dst.name}", flush=True)
    return meta


def transcribe_url(args, url: str, model_cache: dict) -> dict:
    workdir = Path(tempfile.mkdtemp(prefix="stt-url-"))
    outdir = Path(args.output) if args.output else Path.cwd() / "转写"
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        t0 = time.time()
        meta = fetch_meta(url)
        title = (meta.get("title") or "untitled").strip()
        stem = re.sub(r'[/\\:*?"<>|\n\r\t]', "_", title)[:80].strip() or "untitled"
        meta["webpage_url"] = meta.get("webpage_url") or url
        print(f"[url] {title}", flush=True)
        if meta.get("uploader"):
            print(f"      上传者 {meta['uploader']}", flush=True)

        # ① 有字幕就直接用字幕（比跑语音识别又快又准）
        if args.subs:
            want = [w.strip() for w in (args.sub_langs or "").split(",") if w.strip()]
            picked = pick_sub(meta, want)
            if picked:
                kind, lang = picked
                print(f"      有{kind}（{lang}）→ 直接取字幕，跳过语音识别", flush=True)
                srt = fetch_subs(url, workdir, lang, auto=(kind == "自动字幕"))
                items = parse_srt(srt) if srt else []
                if items:
                    items = clip_items(items, args.start, args.duration)
                    meta = _save_thumb(workdir, outdir, stem, meta)
                    meta["transcript_source"] = f"{kind}:{lang}"
                    meta["segments"] = len(items)
                    elapsed = time.time() - t0
                    span = fmt_ts(items[-1]["end"]) if items else "00:00:00"
                    print(f"[url] 字幕直取：{len(items)} 段 / {span} / {elapsed:.1f} s → {outdir}",
                          flush=True)
                    return emit(args, outdir, stem, items, title=title,
                                note=f"来源 {kind}（{lang}）· 未经语音识别 · "
                                     f"生成 {time.strftime('%Y-%m-%d %H:%M')}",
                                meta=meta, elapsed=elapsed, rtf=0.0)
                print("      字幕抓成空 → 回落到语音识别", flush=True)

        # ② 抓音轨 + 封面，走 whisper
        audio = fetch_audio(url, workdir)
        meta = _save_thumb(workdir, outdir, stem, meta)
        return transcribe_one(args, audio, model_cache, meta=meta, out_stem=stem, title=title)
    finally:
        if not args.keep_audio:
            shutil.rmtree(workdir, ignore_errors=True)


def collect(paths: list[str]) -> list[Path]:
    found: list[Path] = []
    for p in paths:
        path = Path(p).expanduser()
        if path.is_dir():
            found += sorted(q for q in path.iterdir() if q.suffix.lower() in MEDIA_EXT)
        elif path.exists():
            found.append(path)
        else:
            sys.exit(f"找不到文件：{p}")
    return found


def main() -> None:
    ap = argparse.ArgumentParser(description="本地视频/音频转写（faster-whisper）")
    ap.add_argument("inputs", nargs="*", help="视频/音频文件或目录")
    ap.add_argument("-o", "--output", help="输出目录（默认：<素材目录>/转写）")
    ap.add_argument("-m", "--model", default="small",
                    help="模型名（tiny/base/small/medium/large-v3/turbo…）或本地模型目录路径")
    ap.add_argument("-l", "--lang", default="auto", help="语种 zh/en/auto（默认 auto）")
    ap.add_argument("--srt", action="store_true", help="额外输出 .srt 字幕")
    ap.add_argument("--start", type=float, help="起始秒，只转片段")
    ap.add_argument("--duration", type=float, help="片段长度（秒）")
    ap.add_argument("--device", default="cpu", choices=["cpu", "mps", "auto"])
    ap.add_argument("--compute-type", default="int8")
    ap.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 4) - 1))
    ap.add_argument("--beam", type=int, default=5)
    ap.add_argument("--no-vad", dest="vad", action="store_false", help="关闭静音过滤")
    ap.add_argument("--no-context", action="store_true", help="关闭上下文续写（防重复循环）")
    ap.add_argument("--prompt", help="初始提示词，可灌专有名词（人名/术语表）")
    ap.add_argument("--model-dir", default=os.environ.get("STT_MODEL_DIR"),
                    help="模型缓存目录（默认 HF 缓存；受沙箱限制时指向工作区内目录）")
    ap.add_argument("--offline", action="store_true", help="（保留参数；本地快照默认优先）")
    ap.add_argument("--keep-audio", action="store_true", help="保留 16k wav 便于回听")
    ap.add_argument("--url", action="append", default=[], metavar="URL",
                    help="视频链接（yt-dlp 抓音轨，可多次；B站/YouTube/小宇宙等）")
    ap.add_argument("--subs", action=argparse.BooleanOptionalAction, default=True,
                    help="链接模式优先用现成字幕（默认开）；--no-subs 强制跑语音识别")
    ap.add_argument("--sub-langs", default="en,zh-Hans,zh",
                    help="字幕语言优先级，逗号分隔（默认 en,zh-Hans,zh）")
    ap.add_argument("--cookies", metavar="FILE",
                    help="yt-dlp 的 cookies.txt（B站 412/会员视频需要；也可用 YTDLP_COOKIES）")
    args = ap.parse_args()
    global COOKIES_FILE
    COOKIES_FILE = args.cookies or ""
    if not Path(args.model).is_dir() and args.model not in MODEL_ALIAS:
        sys.exit(f"未知模型 {args.model!r}：可选 {', '.join(sorted(MODEL_ALIAS))}，或直接传本地模型目录")

    # 位置参数里直接写链接也认：stt.py "https://..." —— 不用记 --url
    urls: list[str] = []
    for p in [*args.inputs, *args.url]:
        if p.startswith(("http://", "https://")) and p not in urls:
            urls.append(p)
    file_args = [p for p in args.inputs if not p.startswith(("http://", "https://"))]
    if urls and not args.output:
        args.output = str(Path.cwd() / "转写")   # 链接没有「所在目录」，默认落当前目录/转写

    files = collect(file_args)
    if not urls and not files:
        sys.exit("没有可处理的媒体文件或链接")

    model_cache: dict = {}
    results = []
    t_all = time.time()
    for u in urls:
        try:
            results.append(transcribe_url(args, u, model_cache))
        except KeyboardInterrupt:
            sys.exit(130)
        except SystemExit:
            raise
        except Exception as e:  # 单个链接失败不影响其它
            print(f"[url] {u} 失败：{type(e).__name__}: {e}", file=sys.stderr)
    for f in files:
        try:
            results.append(transcribe_one(args, f, model_cache))
        except KeyboardInterrupt:
            sys.exit(130)
        except Exception as e:  # 单个失败不影响批量
            print(f"[{f.name}] 失败：{type(e).__name__}: {e}", file=sys.stderr)

    if len(results) > 1:
        print("\n汇总：")
        for r in results:
            print(f"  {r.get('title') or Path(r['src']).stem}  {r['segments']} 段  "
                  f"{fmt_ts(r['duration'])}  用时 {r['elapsed']/60:.1f} min  "
                  f"{r['rtf']:.1f}×实时")
        print(f"总计 {time.time()-t_all:.0f}s")


if __name__ == "__main__":
    main()
