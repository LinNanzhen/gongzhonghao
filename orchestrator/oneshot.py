#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次出稿入口：视频 + 飞书转写文档 → 一条命令跑到 awaiting_publish 并打包导入素材。

用法：
    python3 -m orchestrator.oneshot --video /path/v.mp4 --transcript /path/转写.docx \\
        --title "标题" [--guest 嘉宾] [--provider openai] [--max-ticks 40] \\
        [--runs-dir ...] [--base ...] [--no-render] [--keep-inbox]

流程（全程复用 orchestrator.loop 的状态机，不绕过任何阶段）：
1. 在 inbox/oneshot/ 生成一条事件（主 md 写标题/来源，转写作为同名附件）；
2. loop.ingest 建 run（附件复制进 案例/转写/），再写「媒体路径.txt」供 illustrator 抽帧；
3. 每轮「load_policy → apply_auto_approve / try_auto_gate → loop.step 推进一步」，
   直到 awaiting_publish、blocked 或超过 max-ticks（render 默认开，--no-render 关）；
4. 到达 awaiting_publish 后，在案例目录生成导入包 zip 并打印 JSON 摘要。

退出码：0=awaiting_publish，3=blocked/超时，2=配置错误。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
import zipfile
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from orchestrator import events as ev                  # noqa: E402
from orchestrator import loop                         # noqa: E402
from orchestrator.agents.base import guest_of         # noqa: E402
from orchestrator.providers.base import ProviderError, get_provider  # noqa: E402
from orchestrator.state import Run, RunStore, Source, run_id_for_text  # noqa: E402
from orchestrator.transcript import DOCX_EXTS, load_any  # noqa: E402

EXIT_OK = 0
EXIT_BLOCKED = 3
EXIT_CONFIG = 2

# 可作为转写底稿的扩展名（.doc 仅支持改名为 docx 的 zip；旧二进制会报配置错误）
TRANSCRIPT_EXTS = {".docx", ".docm", ".doc", ".md", ".txt",
                   ".srt", ".vtt", ".json"}


# ---------------------------------------------------------------- 输入与 inbox


def sanitize_name(text: str, *, fallback: str = "oneshot") -> str:
    """把标题变成安全的文件/目录名片段（保留中文）。"""
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", (text or "").strip())
    safe = safe.strip("._-")
    return safe[:60] or fallback


def build_source(video: Path | None, transcript: Path, title: str) -> Source:
    """inbox 事件的来源信息：本地素材，标题用于选题卡与案例目录名。"""
    media = f"视频 {video.name}" if video else "纯音频（无视频）"
    return Source(kind="local", url="", title=title,
                  notes=f"oneshot：{media}；转写 {transcript.name}")


def write_inbox_event(inbox_dir: Path, stem: str, *, video: Path | None, transcript: Path,
                      title: str) -> ev.Event:
    """生成 inbox 事件文件：主 md 写标题/来源，转写复制成同名附件。

    放在 `inbox/oneshot/` 子目录里——正常 `scan_inbox` 只扫 inbox 顶层文件，
    这样临时事件不会被定时任务重复建 run；`--keep-inbox` 时留作审计。
    """
    inbox_dir.mkdir(parents=True, exist_ok=True)
    md = inbox_dir / f"{stem}.md"
    attachment = inbox_dir / f"{stem}{transcript.suffix.lower()}"
    md.write_text(
        f"{title}\n\n"
        f"来源：{'视频 ' + str(video.resolve()) if video else '纯音频（无视频）'}\n"
        f"转写：{transcript.resolve()}\n"
        f"生成：{date.today().isoformat()}（orchestrator.oneshot）\n",
        encoding="utf-8",
    )
    shutil.copy2(transcript, attachment)
    return ev.Event(source=build_source(video, transcript, title),
                    run_id=run_id_for_text(stem),
                    attachments=[attachment])


def cleanup_inbox(inbox_dir: Path, stem: str, transcript: Path) -> None:
    """删掉本次生成的 inbox 事件文件（默认行为；--keep-inbox 关闭）。"""
    for path in (inbox_dir / f"{stem}.md",
                 inbox_dir / f"{stem}{transcript.suffix.lower()}"):
        try:
            path.unlink(missing_ok=True)
        except OSError:            # pragma: no cover - 清理失败不值得让出稿失败
            continue
    try:
        inbox_dir.rmdir()          # 目录空了才删；目录里有别人的东西就留着
    except OSError:
        pass


def ensure_case_transcript(case_dir: Path, transcript: Path) -> None:
    """复用已有 run 时，保证案例 转写/ 里有底稿可转换（附件不会被二次复制）。"""
    tr_dir = case_dir / "转写"
    if (tr_dir / "transcript.json").exists():
        return
    if tr_dir.is_dir():
        for cand in tr_dir.iterdir():
            if cand.is_file() and cand.suffix.lower() in TRANSCRIPT_EXTS:
                return
    tr_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(transcript, tr_dir / f"oneshot_{transcript.name}")


def write_media_path(case_dir: Path, video: Path) -> Path:
    """写「媒体路径.txt」= 视频绝对路径（illustrator / 抽帧依赖它）。"""
    case_dir.mkdir(parents=True, exist_ok=True)
    marker = case_dir / "媒体路径.txt"
    marker.write_text(str(video.resolve()) + "\n", encoding="utf-8")
    return marker


def apply_guest_to_case(store: RunStore, run: Run, guest: str) -> Path:
    """--guest 给了嘉宾名时，把自动分配的案例目录改成 `NN_嘉宾_案例`。

    不改状态机：只重命名目录 + 更新 run.case_dir（ingest 用标题猜人名，
    中文标题常常猜不出；显式 --guest 应该覆盖它）。
    """
    case = loop.case_path(run)
    safe = sanitize_name(guest, fallback="")
    if not safe or safe in case.name:
        return case
    prefix = case.name.split("_", 1)[0]
    target = case.parent / f"{prefix}_{safe}_案例"
    if target.exists():
        print(f"[warn] 案例目录已存在，保留原名：{target.name}")
        return case
    try:
        case.rename(target)
    except OSError as exc:         # pragma: no cover - 磁盘异常不阻塞出稿
        print(f"[warn] 案例目录改名失败：{exc}")
        return case
    run.case_dir = target.name
    store.save(run, f"case_dir\t{target.name}\t(--guest)")
    return target


# ---------------------------------------------------------------- 推进状态机


def advance_to_release(store: RunStore, run: Run, provider, policy: dict, *,
                       render: bool, max_ticks: int) -> tuple[Run | None, str, list[str]]:
    """循环调用 loop.step，直到 awaiting_publish / blocked / 超过 max-ticks。

    返回 (run, reason, messages)：reason 为空串表示成功到达 awaiting_publish。
    闸门复用 loop.try_auto_gate：policy 不放行的闸门（如 publish）一律停下来等人。
    """
    messages: list[str] = []
    for tick in range(1, max(1, max_ticks) + 1):
        run = store.get(run.run_id)
        if run is None:                                   # pragma: no cover - 状态文件被删
            return run, "run 状态文件丢失", messages
        if run.stage == "awaiting_publish":
            return run, "", messages
        if run.status in ("blocked", "stale"):
            return run, f"run {run.status}：{run.error or '（无错误信息）'}", messages
        if run.status == "done":
            return run, f"run 已结束（stage={run.stage}）", messages

        # 复用 tick 的闸门策略：只放行 policy 允许的闸门（publish 永远不自动）
        loop.apply_auto_approve(store, [run], policy)
        run = store.get(run.run_id)
        if run is not None and run.stage == "awaiting_publish":
            return run, "", messages
        if run is not None and run.waiting_gate:
            gate = run.waiting_gate
            if loop.try_auto_gate(store, run, policy):
                msg = f"[{tick}] auto 放行闸门 {gate}"
                messages.append(msg)
                print(msg)
                continue
            return run, f"停在人工闸门 {gate}（stage={run.stage}，policy 未自动放行）", messages

        try:
            msg = loop.step(store, run, provider, dry_run=False, render=render)
        except Exception as exc:  # noqa: BLE001 - 单步异常要给可解释退出码，不裸奔 traceback
            return run, f"推进异常：{type(exc).__name__}: {exc}", messages
        messages.append(f"[{tick}] {msg}")
        print(messages[-1])

        run = store.get(run.run_id)
        if run is not None and run.status == "blocked":
            return run, f"run blocked：{run.error or '未知原因'}", messages
    run = store.get(run.run_id)
    return run, f"超过 max-ticks={max_ticks}", messages


# ---------------------------------------------------------------- 导入包


def _artifact_path(case_dir: Path, run: Run, key: str) -> Path | None:
    """按 run.artifacts[key] 找文件；相对路径按 case_dir 解析，不存在返回 None。"""
    raw = (run.artifacts or {}).get(key)
    if not raw:
        return None
    path = Path(raw)
    path = path if path.is_absolute() else case_dir / path
    return path if path.is_file() else None


def _latest_glob(case_dir: Path, patterns: tuple[str, ...]) -> Path | None:
    """按顺序找最新匹配文件（先按修改时间，再按文件名，避免 v10 排在 v2 前）。"""
    for pattern in patterns:
        hits = sorted(case_dir.glob(pattern),
                      key=lambda p: (p.stat().st_mtime, p.name))
        if hits:
            return hits[-1]
    return None


def _readme(run: Run, case_dir: Path, video: Path | None,
            included: list[tuple[Path, str]], missing: list[str]) -> str:
    """生成「导入说明.md」：导入步骤 + 文件清单 + 发布前检查。"""
    guest = guest_of(case_dir)
    today = date.today().isoformat()
    lines = [
        f"# 导入说明 · {guest}",
        "",
        f"> 生成：{today}（orchestrator.oneshot）",
        f"> run：{run.run_id} · 阶段：{run.stage}",
        f"> 视频：{video.resolve() if video else '（未记录）'}",
        "",
        "## 一、导入步骤（公众号后台）",
        "",
        "1. 用浏览器打开「导入片段.html」，`⌘A` 全选 → `⌘C` 复制。",
        "2. 公众号后台「图文消息」正文编辑器里 `⌘V` 粘贴；粘贴后核对样式是否被过滤。",
        "3. 用「预览.html」核对长文版式；封面用「封面」图；正文配图在「图片/」目录。",
        "4. 标题从「备选标题.md」里选，或直接用正文 H1。",
        "5. 发布前按「质检报告.md」的判定码确认（PASS 才能发）。",
        "",
        "## 二、文件清单",
        "",
        "| 文件 | 期望大小/用途 |",
        "|---|---|",
    ]
    usage = {
        ".html": "粘贴用/版式核对",
        ".jpg": "封面/配图",
        ".jpeg": "封面/配图",
        ".png": "封面/配图",
        ".md": "说明/台账/报告",
    }
    for _path, arc in included:
        lines.append(f"| `{arc}` | {usage.get(Path(arc).suffix.lower(), '素材')} |")
    if missing:
        lines += ["", "缺失（已跳过，不影响导入）：" + "、".join(missing), ""]
    lines += [
        "",
        "## 三、发布前检查",
        "",
        "- [ ] 质检判定码为 PASS（不是 INCOMPLETE）",
        "- [ ] 引文时间戳、说话人缩写已按「说话人对照」人工复核",
        "- [ ] 封面 900×383，未被平台裁切",
        "- [ ] 标题与正文 H1 一致，且未被截断",
        "",
    ]
    return "\n".join(lines)


def build_import_zip(case_dir: Path, run: Run, *,
                     video: Path | None = None) -> Path:
    """在案例目录生成导入包 zip；缺哪项跳过哪项，不报错。

    打包内容：导入片段 / 预览 / 封面 / 图片/ / 图源 / 质检报告 /
    导入说明.md / 备选标题.md（从 run.artifacts 与 case_dir 取）。
    """
    case_dir.mkdir(parents=True, exist_ok=True)
    guest = guest_of(case_dir)
    files: list[tuple[Path, str]] = []
    missing: list[str] = []

    def add_file(label: str, path: Path | None, arc: str) -> None:
        if path is None or not path.is_file():
            missing.append(label)
            return
        files.append((path, arc))

    add_file("导入片段", _artifact_path(case_dir, run, "import")
             or _latest_glob(case_dir, ("*_导入片段.html", "导入片段.html")), "导入片段.html")
    add_file("预览", _artifact_path(case_dir, run, "preview")
             or _latest_glob(case_dir, ("*_排版预览.html", "预览.html")), "预览.html")

    cover = (_artifact_path(case_dir, run, "cover")
             or _latest_glob(case_dir, ("封面*.jpg", "封面*.jpeg", "封面*.png")))
    add_file("封面", cover, f"封面{cover.suffix.lower()}" if cover else "封面.jpg")

    figure = (_artifact_path(case_dir, run, "figure_ledger")
              or _latest_glob(case_dir, ("图源_*.md", "图源*.md")))
    add_file("图源", figure, f"图源{figure.suffix.lower()}" if figure else "图源.md")

    report = (_artifact_path(case_dir, run, "qc_report")
              or _latest_glob(case_dir, ("*_质检报告.md", "质检报告.md")))
    add_file("质检报告", report, f"质检报告{report.suffix.lower()}" if report else "质检报告.md")

    titles = _latest_glob(case_dir, ("*备选标题*.md",))
    add_file("备选标题", titles, f"备选标题{titles.suffix.lower()}" if titles else "备选标题.md")

    images = case_dir / "图片"
    image_files = ([p for p in sorted(images.rglob("*")) if p.is_file()]
                   if images.is_dir() else [])
    if not image_files:
        missing.append("图片")

    readme_text = _readme(run, case_dir, video, files, missing)
    readme = case_dir / "导入说明.md"
    readme.write_text(readme_text, encoding="utf-8")
    # 导入说明放最前面，清单里的内容顺序与人直觉一致
    files.insert(0, (readme, "导入说明.md"))

    zip_path = case_dir / f"导入包_{date.today().isoformat()}_{guest}.zip"
    tmp_path = zip_path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, arc in files:
            try:
                zf.write(path, arc)
            except OSError:        # pragma: no cover - 单文件读失败跳过
                continue
        for path in image_files:
            try:
                zf.write(path, Path("图片") / path.relative_to(images))
            except OSError:        # pragma: no cover
                continue
    tmp_path.replace(zip_path)
    return zip_path


# ---------------------------------------------------------------- 主流程


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python3 -m orchestrator.oneshot",
        description="一次出稿：视频 + 转写文档 → 跑到 awaiting_publish 并打包导入素材",
    )
    ap.add_argument("--video", default="",
                    help="原视频路径（illustrator 抽帧用）；纯音频播客可省略")
    ap.add_argument("--transcript", required=True,
                    help="飞书转写文档（.docx/.md/.txt/.srt/.vtt/.json）")
    ap.add_argument("--title", default="",
                    help="标题（省略时读同名 .meta.json 的 title，再退化为文件名）")
    ap.add_argument("--guest", default="", help="嘉宾名（覆盖自动猜的案例目录名）")
    ap.add_argument("--provider", default="stub",
                    help="LLM provider：stub / openai-compat（别名 deepseek、kimi、qwen）")
    ap.add_argument("--max-ticks", type=int, default=40, dest="max_ticks",
                    help="最多推进多少步（默认 40）")
    ap.add_argument("--runs-dir", default="", dest="runs_dir",
                    help="run 状态目录（默认 orchestrator/runs；自检可指向 tmp）")
    ap.add_argument("--base", default="",
                    help="案例根目录（默认仓库父目录；自检可指向 tmp）")
    ap.add_argument("--no-render", action="store_true", dest="no_render",
                    help="排版时不跑 Chrome 三档渲染（默认跑）")
    ap.add_argument("--retry-blocked", type=int, default=1,
                    help="可恢复阻塞（读超时/连接错误）自动重试次数，默认 1")
    ap.add_argument("--keep-inbox", action="store_true", dest="keep_inbox",
                    help="保留生成的 inbox 事件文件（默认 ingest 后清理）")
    return ap


def _sibling_meta(transcript: Path) -> Path:
    """X.transcript.json → X.meta.json（stt 链接模式落的元数据）。"""
    name = transcript.name
    if name.endswith(".transcript.json"):
        return transcript.with_name(name[: -len(".transcript.json")] + ".meta.json")
    return transcript.with_suffix(".meta.json")


def title_from_meta(transcript: Path) -> str:
    """从 stt 产物旁的 .meta.json 取原始标题，省掉手填 --title。"""
    meta = _sibling_meta(transcript)
    if not meta.is_file():
        return ""
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    return str(data.get("title") or "").strip()


def _validate(args) -> tuple[Path, Path, str] | None:
    """检查输入，返回 (video, transcript, title)；配置错误返回 None。"""
    video = Path(args.video).expanduser() if (args.video or "").strip() else None
    transcript = Path(args.transcript).expanduser()
    title = (args.title or "").strip()
    if not title:
        title = title_from_meta(transcript)
        if title:
            print(f"[title] 采用 {_sibling_meta(transcript).name} 里的标题：{title}")
    if video is not None and not video.is_file():
        print(f"配置错误：视频不存在：{video}", file=sys.stderr)
        return None
    if not transcript.is_file():
        print(f"配置错误：转写文档不存在：{transcript}", file=sys.stderr)
        return None
    suffix = transcript.suffix.lower()
    if suffix not in TRANSCRIPT_EXTS:
        print(f"配置错误：不支持的转写格式 {suffix}（可用：{', '.join(sorted(TRANSCRIPT_EXTS))}）",
              file=sys.stderr)
        return None
    if suffix in DOCX_EXTS or suffix == ".doc":
        try:
            load_any(transcript)      # 提前暴露坏 docx / 旧 .doc，别到转写阶段才 blocked
        except (ValueError, OSError) as exc:
            print(f"配置错误：转写文档解析失败：{exc}", file=sys.stderr)
            return None
    if int(args.max_ticks) < 1:
        print("配置错误：--max-ticks 必须 ≥ 1", file=sys.stderr)
        return None
    return video, transcript, title


def main(argv=None) -> int:
    started = time.monotonic()
    args = build_parser().parse_args(argv)
    checked = _validate(args)
    if checked is None:
        return EXIT_CONFIG
    video, transcript, title = checked

    # 覆盖案例根目录与 run 目录（自检/多实例用，不影响默认路径）
    if args.base:
        loop.BASE = Path(args.base).expanduser().resolve()
        loop.BASE.mkdir(parents=True, exist_ok=True)
    runs_dir = (Path(args.runs_dir).expanduser().resolve() if args.runs_dir
                else loop.HERE / "runs")
    store = RunStore(runs_dir)

    try:
        provider = get_provider(args.provider)
    except ProviderError as exc:
        print(f"provider 配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG
    if getattr(provider, "name", "") != "stub" and not getattr(provider, "api_key", ""):
        print("缺少 API key：请设置 GZH_LLM_API_KEY（或改用 --provider stub）", file=sys.stderr)
        return EXIT_CONFIG

    policy = loop.load_policy()
    stem = sanitize_name(title)
    rid = run_id_for_text(stem)
    run = store.get(rid)

    if run is None:
        inbox_dir = loop.HERE / "inbox" / "oneshot"
        event = write_inbox_event(inbox_dir, stem, video=video,
                                  transcript=transcript, title=title)
        created = loop.ingest(store, [event], dry_run=False)
        if not args.keep_inbox:
            cleanup_inbox(inbox_dir, stem, transcript)
        run = store.get(rid)
        if run is None:
            print("配置错误：ingest 未能创建 run（检查 state 目录写权限）", file=sys.stderr)
            return EXIT_CONFIG
        if created:
            print(f"建 run：{rid} → {run.case_dir}")
        else:                       # 并发跑同一标题：别人已经建好，接着推进即可
            print(f"复用已存在的 run：{rid}（stage={run.stage}）")
        case = loop.case_path(run)
    else:
        print(f"复用已有 run：{rid}（stage={run.stage}）")
        case = loop.case_path(run)
        ensure_case_transcript(case, transcript)

    if args.guest:
        case = apply_guest_to_case(store, run, args.guest)
    if video is not None:
        write_media_path(case, video)

    run, reason, messages = advance_to_release(
        store, run, provider, policy, render=not args.no_render,
        max_ticks=int(args.max_ticks))

    # 网络/服务端抖动导致的 blocked（读超时、连接重置、429/5xx）：清错重试有限次。
    # 内容/门禁类 blocked 不重试——那需要人看，重试只会重复烧钱。
    transient_keys = ("timeout", "timed out", "网络", "connection", "连接",
                      "providererror", "429", "502", "503", "504")
    retries = int(getattr(args, "retry_blocked", 1) or 0)
    while (reason and retries > 0 and run is not None and run.status == "blocked"
           and any(k in (run.error or "").lower() for k in transient_keys)):
        retries -= 1
        print(f"可恢复阻塞（{(run.error or '')[:100]}）：清空错误后重试")
        run.status = "active"
        run.error = ""
        run.errors = 0
        store.save(run, "resume\toneshot 自动重试（可恢复错误）")
        run, reason, more = advance_to_release(
            store, run, provider, policy, render=not args.no_render,
            max_ticks=int(args.max_ticks))
        messages += more

    elapsed = round(time.monotonic() - started, 2)
    zip_path: Path | None = None
    if not reason and run is not None:
        try:
            zip_path = build_import_zip(case, run, video=video)
            print(f"导入包：{zip_path}")
        except Exception as exc:  # noqa: BLE001 - 打包失败要如实报告，不假装成功
            reason = f"导入包生成失败：{type(exc).__name__}: {exc}"

    summary = {
        "ok": not reason,
        "run_id": rid,
        "stage": run.stage if run is not None else "",
        "status": run.status if run is not None else "",
        "case_dir": str(case),
        "zip": str(zip_path) if zip_path is not None else "",
        "elapsed_sec": elapsed,
        "ticks": len(messages),
        "usage": dict((run.usage if run is not None else {}) or {}),
        "reason": reason,
        "steps": messages,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if reason:
        print(f"未到达 awaiting_publish：{reason}", file=sys.stderr)
        return EXIT_BLOCKED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
