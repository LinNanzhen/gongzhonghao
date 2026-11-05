#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gzh_subtitle.py —— 公众号字幕长图一键命令（基于 native-subtitle-quote-image skill 本地化脚本）

把「转写稿 → 选句定位 → manifest → 渲染 3:4 社交长图」从人工多轮目检，
压缩成两条命令（pick 半自动选点 + render 一条龙）。

用法：
  1) 选句定位（半自动）：
     python3 gzh_subtitle.py pick transcript.json \
       --out manifest_candidate.json \
       --quote "金丝雀已启动::already it's already happening" \
       --quote "编码正中靶心::coding is obviously dead center" \
       --quote "数百万工作消失但新工作更大::millions of jobs that are going to disappear" \
       --quote "能力飙升经济温和::raw capabilities are skyrocketing" \
       --quote "三到五年::three to five years" \
       --quote "每个都是CEO::CEO of a bunch of agents" \
       --quote "最好十年最坏十年::best decade" \
       --quote "巨大能动性::agency right now"
     输出候选时间点 JSON。每个观点自动按「关键词命中段」取时间点，
     相邻时间点间隔不足 3s 的去重（烧录字幕按句显示，间隔太近会重复同句）。
     人工微调后即可 render。

  2) 渲染（一条龙）：
     python3 gzh_subtitle.py render video.mp4 \
       --manifest manifest.json \
       --out-dir 输出目录 \
       --band-top 0.84 --band-bottom 0.98 \
       --overwrite
     产出 {NN}_{标题}.jpg（1440x1920 3:4）+ final_contact_sheet.jpg 总览。

子命令：
  pick   transcript.json --quote "标题::关键词1|关键词2" [--quote ...] [--out ...]
  render video --manifest manifest.json --out-dir DIR [--band-top ..] [--band-bottom ..]
              [--aspect ..] [--width ..] [--overwrite] [--tmp-dir ..] [--stitch PATH]
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STITCH = HERE / "subtitle_skill" / "native_subtitle_stitch.py"
MIN_GAP = 3.0  # 相邻时间点最小间隔（秒），低于则丢弃后点（避免同句重复）


# ---------------------------------------------------------------- pick
def _segment_text_hits(segments, pattern):
    """返回按时间排序的命中段列表 [(start, end, text), ...]"""
    hits = []
    for seg in segments:
        text = seg.get("text", "")
        if pattern.search(text):
            hits.append((seg["start"], seg["end"], text))
    return hits


def _dedupe_times(times):
    """按 MIN_GAP 去重：保留第一个，丢弃与上一个间隔 < MIN_GAP 的后续点。"""
    if not times:
        return []
    out = [times[0]]
    for t in times[1:]:
        if t - out[-1] >= MIN_GAP:
            out.append(t)
    return out


def cmd_pick(args):
    transcript_path = Path(args.transcript).expanduser().resolve()
    if not transcript_path.exists():
        raise SystemExit(f"转写稿不存在: {transcript_path}")
    with transcript_path.open(encoding="utf-8") as handle:
        segments = json.load(handle)
    if not isinstance(segments, list):
        raise SystemExit("transcript.json 应为数组")
    # 解析 --quote "标题::关键词1|关键词2"
    quotes = []
    for raw in args.quote:
        if "::" not in raw:
            raise SystemExit(f"--quote 需为「标题::关键词1|关键词2」: {raw}")
        title, keywords = raw.split("::", 1)
        title = title.strip()
        keys = [k.strip() for k in keywords.split("|") if k.strip()]
        if not keys:
            raise SystemExit(f"--quote 缺少关键词: {raw}")
        quotes.append((title, keys))
    images = []
    for title, keys in quotes:
        # 多个关键词取并集命中（任意命中即算），保持时间序
        all_hits = []
        for key in keys:
            try:
                pattern = re.compile(re.escape(key), re.IGNORECASE)
            except re.error:
                pattern = re.compile(key, re.IGNORECASE)
            all_hits.extend(_segment_text_hits(segments, pattern))
        all_hits.sort(key=lambda x: (x[0], x[1]))
        # 取命中段的 start（英文原话起始时刻）
        times = [h[0] for h in all_hits]
        times = _dedupe_times(times)
        if not times:
            print(f"[warn] 「{title}」无任何关键词命中，跳过")
            continue
        images.append({"title": title, "times": times})
    if not images:
        raise SystemExit("没有任何观点命中关键词，请调整 --quote 关键词")
    manifest = {"images": images}
    out = Path(args.out).expanduser().resolve() if args.out else HERE / "manifest_candidate.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    print(f"已生成候选 manifest: {out}")
    print("提示：渲染前请用 band/sample 目检每个时间点画面与字幕完整性，")
    print("      再人工微调 times（本工具只做定位，不做画面质量判断）。")
    print(f"  （共 {len(images)} 张卡，时间点区间见文件）")
    return 0


# ---------------------------------------------------------------- render
def cmd_render(args):
    stitch = Path(args.stitch).expanduser().resolve() if args.stitch else STITCH
    if not stitch.exists():
        raise SystemExit(f"本地化渲染脚本不存在: {stitch}（可 --stitch 指定）")
    video = Path(args.video).expanduser().resolve()
    manifest = Path(args.manifest).expanduser().resolve()
    if not video.exists():
        raise SystemExit(f"视频不存在: {video}")
    if not manifest.exists():
        raise SystemExit(f"manifest 不存在: {manifest}")
    cmd = [sys.executable, str(stitch), "render", str(video),
           "--manifest", str(manifest),
           "--out-dir", args.out_dir]
    if args.aspect:
        cmd += ["--aspect", args.aspect]
    if args.width:
        cmd += ["--width", str(args.width)]
    cmd += ["--band-top", str(args.band_top), "--band-bottom", str(args.band_bottom)]
    if args.tmp_dir:
        cmd += ["--tmp-dir", args.tmp_dir]
    if args.overwrite:
        cmd += ["--overwrite"]
    if args.keep_going:
        cmd += ["--keep-going"]
    if args.resume:
        cmd += ["--resume"]
    print(">", " ".join(cmd))
    proc = subprocess.run(cmd)
    return proc.returncode


# ---------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    pick = sub.add_parser("pick", help="从转写稿按关键词半自动定位时间点，输出候选 manifest")
    pick.add_argument("transcript", help="transcript.json 路径")
    pick.add_argument("--quote", action="append", required=True,
                      help="「标题::关键词1|关键词2」，可多次传入")
    pick.add_argument("--out", help="候选 manifest 输出路径（默认 ./manifest_candidate.json）")
    pick.add_argument("--min-gap", type=float, default=MIN_GAP,
                      help="相邻时间点最小间隔秒数（默认 3.0）")
    pick.set_defaults(func=cmd_pick)

    render = sub.add_parser("render", help="调用本地化 skill 脚本渲染整套 3:4 长图")
    render.add_argument("video", help="视频路径")
    render.add_argument("--manifest", required=True)
    render.add_argument("--out-dir", required=True)
    render.add_argument("--aspect", default="3:4")
    render.add_argument("--width", type=int, default=1440)
    render.add_argument("--band-top", type=float, default=0.84,
                        help="字幕区域上边界（本视频英文底行约 0.96，默认 0.84 不截中文行）")
    render.add_argument("--band-bottom", type=float, default=0.98)
    render.add_argument("--tmp-dir", help="抽帧临时目录（默认系统临时目录）")
    render.add_argument("--stitch", help="本地化渲染脚本路径（默认流水线/subtitle_skill/下）")
    render.add_argument("--overwrite", action="store_true")
    render.add_argument("--keep-going", action="store_true",
                        help="单张失败不中断整批（默认遇错即停）")
    render.add_argument("--resume", action="store_true",
                        help="续跑：跳过已存在且有效的图，只渲染缺失/失败的卡")
    render.set_defaults(func=cmd_render)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
