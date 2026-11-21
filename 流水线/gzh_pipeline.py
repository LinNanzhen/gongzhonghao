#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公众号一键流水线 v1.1（对应 SOP v10：新增对话块渲染）
====================================
输入：Markdown 人味版正文（或 HTML 正文片段）＋ 元信息
输出：
  {name}_排版预览.html    —— 浏览器审阅完整页
  {name}_导入片段.html    —— 微信编辑器导入片段（无外壳）
  {name}_质检报告.md      —— 质检门禁结果
  {name}_渲染_{375|390|430}.png —— 三档视口截图

流程：解析正文 → 组装双 HTML → 质检门禁 → Chrome headless 三档渲染验收 → 报告
依赖：仅 Python 3 标准库；渲染验收需要本机 Chrome（自动探测路径）。

判定契约（T1，机器可读行 `- 判定码：`）：
  PASS        退出码 0  质检门禁全过，且三档渲染验收真的跑过
  FAIL        退出码 1  质检门禁有 FAIL，或任一 viewport 渲染验收失败
  INCOMPLETE  退出码 3  渲染验收没跑（--skip-render / 无 Chrome / 无任何 viewport 结果）
              —— 未完成 ≠ 通过，不得作为放行依据；补跑渲染后重生成报告

用法示例：
  python3 gzh_pipeline.py --md 布林约尔松AI就业观_人味版.md \\
      --name 布林约尔松AI就业观 --title "AI 不会平均地消灭工作" \\
      --series 人物与科技 --episode 13 --prev "马斯克：一个担心 AI 毁灭人类的人" \\
      --card 人物卡_Brynjolfsson.html --outdir ./案例
  # 直接用 HTML 正文片段：
  python3 gzh_pipeline.py --body-html body.html --name xx --outdir ./
  # 完整联网校验图片可达性（较慢）：
  python3 gzh_pipeline.py --md x.md --name x --check-links
"""

import argparse
import base64
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------- 判定契约
# 判定值单一来源：orchestrator/verdict.py（与 gatekeeper 共用，T1c）。
# 本脚本也可能被单独拷出去跑，所以 import 失败时就地兜底，不让工具直接挂掉。
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
try:  # pragma: no cover - 兜底分支只在脱离仓库单跑时触发
    from orchestrator.verdict import FAIL, INCOMPLETE, PASS
except ImportError:  # pragma: no cover
    PASS, FAIL, INCOMPLETE = "PASS", "FAIL", "INCOMPLETE"

# 总判定（人类可读）与退出码：0=通过，1=未通过（质检门禁 FAIL），3=未完成。
TOTAL_LABEL = {
    PASS: "**通过**",
    FAIL: "**未通过（需修正）**",
    INCOMPLETE: "**未完成（渲染验收未执行）**",
}
EXIT_CODE = {PASS: 0, FAIL: 1, INCOMPLETE: 3}


# ---------------------------------------------------------------- 视觉参数
VISUAL = {
    "body_margin": "margin:0 auto;padding:20px;max-width:680px;background:#ffffff;color:#333333;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;word-wrap:break-word;",
    "p": "margin:0 0 18px;font-size:16px;line-height:1.8;color:#333333;",
    "p_brand": "margin:0 0 24px;font-size:12px;line-height:1.6;letter-spacing:2px;color:#666666;",
    "sec_no": "margin:0 0 8px;font-size:24px;line-height:1.2;letter-spacing:1px;font-weight:700;color:#9a7841;",
    "sec_title": "margin:0;font-size:20px;line-height:1.5;font-weight:700;color:#222222;",
    "sec_gap": "margin:40px 0 20px;",
    "quote": "margin:24px 0;padding:16px 18px;background:#faf7f2;border-left:4px solid #9a7841;",
    "quote_cn": "margin:0 0 6px;font-size:16px;line-height:1.8;color:#333333;font-weight:600;",
    "quote_en": "margin:0;font-size:14px;line-height:1.8;color:#8a857e;font-style:italic;",
    "quote_src": "margin:10px 0 0;font-size:13px;line-height:1.7;color:#999999;",
    # 对话块（SOP v10）：说话人缩写品牌金、译文正文色不加粗、英文灰斜体
    "quote_speaker": "color:#9a7841;font-weight:700;",
    "quote_dialogue_cn": "margin:0 0 4px;font-size:16px;line-height:1.8;color:#333333;",
    "quote_dialogue_en": "margin:0 0 8px;font-size:14px;line-height:1.8;color:#8a857e;font-style:italic;",
    "img_block": "margin:26px 0;",
    "img": "display:block;width:100%;height:auto;margin:0;",
    "caption": "margin:8px 0 0;font-size:13px;line-height:1.7;color:#666666;",
    "src_title": "margin:0 0 12px;font-size:14px;line-height:1.7;font-weight:700;color:#333333;",
    "src_line": "margin:0 0 10px;font-size:13px;line-height:1.7;color:#666666;",
    "nav": "margin:28px 0 0;padding:18px 0 0;border-top:1px solid #e5e2dc;",
    "nav_line": "margin:0 0 8px;font-size:13px;line-height:1.7;color:#666666;",
    "nav_line_last": "margin:0;font-size:13px;line-height:1.7;color:#666666;",
    "card_box": "margin:28px 0;padding:18px;background:#faf8f5;",
    "strong": "color:#222222;",
    "highlight": "color:#f05a28;",
    "section_top": "margin:40px 0 0;padding:20px 0 0;border-top:1px solid #e5e2dc;",
}

# ---------------------------------------------------------------- 禁用词（与模板/禁用词表.md 同步维护）
# 高危词：命中即 FAIL（典型财经稿 AI 味词汇）
# 分级依据见 01_SOP工作流/模板/人味检查表.md：
# 自动门禁判不了语境，只把「几乎总是空话」的放 FAIL；
# 在技术或正式语境里可能成立的（闭环、链路、沉淀、对齐）降为 WARN。
FORBIDDEN_WORDS = [
    "赋能", "抓手", "打法", "底层逻辑", "颗粒度", "拉齐",
    "复盘", "势能", "破局",
    "值得注意的是", "不可忽视的是", "总而言之",
    "让我们一起", "值得深思的是", "综合来看",
    "人的价值在重新定义", "这说明了", "在这个信息爆炸的时代", "随着……的不断发展",
]
# 语境词：命中即 WARN（如"心智"在科技语境下可正常使用、"最后"是叙事而非列举）
FORBIDDEN_CONTEXT = ["首先", "其次", "最后", "无疑", "显然", "心智",
                     "闭环", "链路", "沉淀", "对齐"]
FORBIDDEN_RE = re.compile(r"在[^。；，]{2,12}的今天|这背后是")
# 句式层面的语境词：假对比有时成立（前后两半各自有信息），只提示
FORBIDDEN_CONTEXT_RE = re.compile(r"不仅仅是[^。；，]{1,30}更是")

# ---------------------------------------------------------------- 硬规则门禁（模板/公众号HTML硬规则.md）
HARD_GATES = [
    (re.compile(r"<div\b"), "残留 div（公众号 ProseMirror 会整段吞掉）"),
    (re.compile(r'\bid="'), "残留 id 属性（会被删除）"),
    (re.compile(r'class="'), "残留 class 属性（无效）"),
    (re.compile(r"<script\b"), "残留 script"),
    (re.compile(r"position:"), "残留 position 定位（整行删除）"),
    (re.compile(r"linear-gradient"), "残留渐变（降级为透明）"),
    (re.compile(r"display:flex"), "残留 flex"),
    (re.compile(r"REPLACE_IMG_"), "未换图占位符"),
    (re.compile(r"@media"), "残留媒体查询"),
    (re.compile(r"<!DOCTYPE|</html>|<html\b|<head>|</head>|<body\b|</body>|<style\b", re.I), "导入片段含页面外壳"),
]

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]

VIEWPORTS = [375, 390, 430]


# ---------------------------------------------------------------- 工具
def find_chrome():
    for c in CHROME_CANDIDATES:
        if os.path.exists(c):
            return c
    return None


def chrome_sandbox_args():
    """headless Chrome 的启动参数补丁，返回 (参数列表, profile 目录)。

    受限环境（DSH 文件沙箱 / CI 容器）里 Chrome 直接起 headless 会报
    `Failed to create a unique user data directory for headless`，
    随后 `--dump-dom` 退出 0 但输出为空 → 渲染验收永远"未捕获测量结果"。
    显式给每个调用一个独立 profile 并关闭 Chrome 自带沙箱后即可正常输出。
    调用方负责在 finally 里 shutil.rmtree 掉 profile 目录（不并发共用、不锁）。
    """
    profile = tempfile.mkdtemp(prefix="gzh-chrome-")
    return ["--no-sandbox", f"--user-data-dir={profile}"], profile


def p(text, style=VISUAL["p"]):
    return f'<p style="{style}">{text}</p>'


def esc(text):
    return html.escape(text, quote=False)


def inline(text):
    """行内转换：**加粗** → strong，保留其他文本。"""
    text = esc(text)
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: f'<strong style="{VISUAL["strong"]}">{m.group(1)}</strong>', text)
    return text


# 对话说话人（SOP v10）：行首「大写姓名缩写＋冒号」，如 IS： / DP：
SPEAKER_RE = re.compile(r"^([A-Z]{1,4})[：:]\s*(.*)$")


def speaker_inline(text):
    """对话行：姓名缩写渲染为品牌金说话人，其余按正文；非对话行原样走 inline。"""
    m = SPEAKER_RE.match(text)
    if not m:
        return inline(text)
    return f'<strong style="{VISUAL["quote_speaker"]}">{esc(m.group(1))}：</strong>{inline(m.group(2))}'


# ---------------------------------------------------------------- md 解析
def _embed_local_image(url, md_dir):
    """本地相对路径图片 → data URI（导入片段必须自包含，微信编辑器不认本地路径）。

    只有 http(s):// 与 data: 原样保留；其余按 md 所在目录解析，文件存在才内联，
    不存在则原样返回（交给质检「图片链接合规」报 FAIL，不静默吞掉）。
    """
    if url.startswith(("http://", "https://", "data:")):
        return url
    if md_dir is None:
        return url
    cand = Path(md_dir) / url
    if not cand.exists():
        return url
    suffix = cand.suffix.lower().lstrip(".")
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp",
            "gif": "gif"}.get(suffix, "jpeg")
    b64 = base64.b64encode(cand.read_bytes()).decode("ascii")
    return f"data:image/{mime};base64,{b64}"


def parse_md(md_text, card_html=None, series="", episode="", prev="", md_dir=None):
    """把 Markdown 人味版正文解析为 BODY_HTML。

    md_dir：正文 md 所在目录；相对路径图片会内联成 data URI，保证导入片段自包含。
    """
    lines = md_text.split("\n")
    body = []
    brand = ""
    if series:
        brand += series
    if episode:
        brand += (" · 第 " + str(episode) + " 期" if series else "第 " + str(episode) + " 期")
    if brand:
        body.append(p(esc(brand), VISUAL["p_brand"]))

    i = 0
    n = len(lines)
    card_inserted = False
    in_appendix = False
    sec_no = 0

    def insert_card():
        nonlocal card_inserted
        if card_html and not card_inserted:
            body.append(card_html)
            card_inserted = True

    while i < n:
        raw = lines[i].rstrip()
        line = raw.strip()

        # 附录截断
        if line.startswith("## ") and ("内部附录" in line or "不进入正文" in line):
            in_appendix = True
            i += 1
            continue
        if in_appendix:
            i += 1
            continue

        # 主标题（H1）——仅作页面 title，不重复进正文
        if line.startswith("# ") and not line.startswith("## "):
            i += 1
            continue

        # 章节标题
        if line.startswith("## "):
            title = line[3:].strip()
            m = re.match(r"^(\d{2})[｜|]\s*(.*)", title)
            if m:
                sec_no += 1
                # SOP：人物卡紧随导语、章节 01 之前
                if sec_no == 1:
                    insert_card()
                no = m.group(1)
                t = m.group(2)
                body.append(
                    f'<section style="{VISUAL["sec_gap"]}"><p style="{VISUAL["sec_no"]}">{no}</p>'
                    f'<h2 style="{VISUAL["sec_title"]}">{inline(t)}</h2></section>'
                )
            else:
                # 导语 / 结尾 / 来源与说明 等无编号标题
                if title in ("导语", "结尾"):
                    pass  # 标题不进正文，直接进入段落
                elif "来源" in title or "说明" in title:
                    body.append(
                        f'<section style="{VISUAL["section_top"]}"><p style="{VISUAL["src_title"]}">{esc(title)}</p>'
                    )
                    i += 1
                    # 标题与条目之间允许空行（标准 md 写法）；逐条渲染，去列表符
                    while i < n and not lines[i].strip():
                        i += 1
                    while i < n and lines[i].strip() and not lines[i].startswith("## "):
                        src = re.sub(r"^[-*]\s+", "", lines[i].strip())
                        body.append(p(inline(src), VISUAL["src_line"]))
                        i += 1
                    body.append("</section>")
                    continue
                else:
                    # 其他标题按加粗段处理
                    body.append(p(f'<strong style="{VISUAL["strong"]}">{inline(title)}</strong>'))
            # 章节1前插入人物卡
            if sec_no == 1:
                insert_card()
            i += 1
            continue

        # 人物卡标记
        if line == "<!-- 人物卡 -->" or line == "<!-- 人物卡: -->":
            insert_card()
            i += 1
            continue

        # 引用块 / 导读条 / 对话块（连续 > 行）
        #   普通引用：> 文本
        #   中英对照导读条：> 中文观点  +  > EN|英文原话（EN 前缀行渲染为灰色斜体）
        #   对话块（SOP v10）：> IS：中文译文  +  > EN|English original
        #     行首「大写缩写＋冒号」识别为说话人；问答可同块，按行序交替渲染
        if line.startswith(">"):
            quotes = []
            while i < n and lines[i].strip().startswith(">"):
                q = lines[i].strip()[1:].strip()
                if q:
                    quotes.append(q)
                i += 1
            has_en = any(re.match(r"^EN[|｜:：]", q) for q in quotes)

            def is_src(q):
                return q.startswith("——") or q.startswith("—") or (q.startswith("- ") and "：" not in q)

            dialogue = any(SPEAKER_RE.match(q) for q in quotes
                           if not re.match(r"^EN[|｜:：]", q) and not is_src(q))
            if has_en and dialogue:
                # 对话块：说话人金色、译文不加粗、英文灰斜体，逐行交替
                block = [f'<section style="{VISUAL["quote"]}">']
                for q in quotes:
                    m_en = re.match(r"^EN[|｜:：]\s*", q)
                    if m_en:
                        block.append(p(inline(q[m_en.end():]), VISUAL["quote_dialogue_en"]))
                    elif is_src(q):
                        block.append(p(inline(q), VISUAL["quote_src"]))
                    else:
                        block.append(p(speaker_inline(q), VISUAL["quote_dialogue_cn"]))
                block.append("</section>")
            elif has_en:
                # 导读条：中英对照
                block = [f'<section style="{VISUAL["quote"]}">']
                cn_lines, en_lines, src_lines = [], [], []
                for q in quotes:
                    if re.match(r"^EN[|｜:：]", q):
                        en_lines.append(re.sub(r"^EN[|｜:：]\s*", "", q))
                    elif is_src(q):
                        src_lines.append(q)
                    else:
                        cn_lines.append(q)
                if cn_lines:
                    block.append(p(inline(" ".join(cn_lines)), VISUAL["quote_cn"]))
                if en_lines:
                    block.append(p(inline(" ".join(en_lines)), VISUAL["quote_en"]))
                if src_lines:
                    block.append(p(inline(" ".join(src_lines)), VISUAL["quote_src"]))
                block.append("</section>")
            else:
                # 普通引用块
                block = [f'<section style="{VISUAL["quote"]}">']
                for j, q in enumerate(quotes):
                    if is_src(q):
                        block.append(p(inline(q), VISUAL["quote_src"]))
                    else:
                        block.append(p(speaker_inline(q)))
                block.append("</section>")
            body.append("\n".join(block))
            continue

        # 图片行 ![图注](url)
        m_img = re.match(r"^!\[(.*?)\]\((.*?)\)\s*$", line)
        if m_img:
            cap = m_img.group(1).strip()
            url = _embed_local_image(m_img.group(2).strip(), md_dir)
            alt = esc(cap) if cap else "配图"
            block = [f'<section style="{VISUAL["img_block"]}">']
            block.append(f'<img src="{esc(url)}" style="{VISUAL["img"]}" alt="{alt}">')
            if cap:
                block.append(p(inline(cap), VISUAL["caption"]))
            block.append("</section>")
            body.append("\n".join(block))
            i += 1
            continue

        # 空行
        if not line:
            i += 1
            continue

        # 普通段落（可能多行合并）
        para = []
        while i < n and lines[i].strip() and not lines[i].strip().startswith(("## ", "# ", ">", "!")):
            para.append(lines[i].strip())
            i += 1
        body.append(p(inline(" ".join(para))))
        continue

    # 确保人物卡已插入（无章节结构时兜底，插在文首品牌行后）
    if card_html and not card_inserted:
        # 插到第一个 section（品牌行）之后
        if len(body) >= 1:
            body.insert(1, card_html)
        else:
            body.insert(0, card_html)

    # 系列导航
    if series or prev:
        nav = [f'<section style="{VISUAL["nav"]}">']
        nav.append(p(esc(series or "本系列"), VISUAL["nav_line"]))
        if prev:
            nav.append(p("上一篇：" + esc(prev), VISUAL["nav_line_last"]))
        nav.append("</section>")
        body.append("\n".join(nav))

    return "\n\n".join(body)


# ---------------------------------------------------------------- HTML 组装
def build_html(body_html, title):
    preview = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)}</title>
</head>
<body style="margin:0;background:#ffffff;">
<section style="{VISUAL['body_margin']}">
{body_html}
</section>
</body>
</html>
"""
    imp = f"""<section style="{VISUAL['body_margin']}">
{body_html}
</section>
"""
    return preview, imp


# ---------------------------------------------------------------- 质检
def qa_gates(body_html, preview_html, import_html):
    """返回 [(检查名, 结果, 详情)]，result ∈ ok/warn/fail"""
    report = []

    # 1. 硬规则门禁（对导入片段）
    issues = []
    for pat, desc in HARD_GATES:
        for m in pat.finditer(import_html):
            ctx = import_html[max(0, m.start() - 30):m.end() + 30].replace("\n", " ")
            issues.append(f"{desc}：…{ctx}…")
    report.append(("HTML 硬规则门禁", "fail" if issues else "ok", "; ".join(issues) if issues else "8+ 项门禁全过"))

    # 2. 禁用词
    hits = []
    ctx_hits = []
    text = re.sub(r"<[^>]+>", "", body_html)
    for w in FORBIDDEN_WORDS:
        if w in text:
            idx = text.find(w)
            hits.append(f"{w}（…{text[max(0, idx - 10):idx + 20]}…）")
    for w in FORBIDDEN_CONTEXT:
        for m in re.finditer(w, text):
            idx = m.start()
            ctx_hits.append(f"{w}（…{text[max(0, idx - 10):idx + 20]}…）")
    for m in FORBIDDEN_RE.finditer(text):
        hits.append(f"句式「{m.group(0)}」")
    for m in FORBIDDEN_CONTEXT_RE.finditer(text):
        idx = m.start()
        ctx_hits.append(f"句式「{m.group(0)}」（…{text[max(0, idx - 8):idx + 26]}…）")
    if hits:
        report.append(("禁用词/句式", "fail", "; ".join(hits)))
    elif ctx_hits:
        report.append(("禁用词/句式（语境词）", "warn", "; ".join(ctx_hits[:6]) + ("…" if len(ctx_hits) > 6 else "")))
    else:
        report.append(("禁用词/句式", "ok", "高危词、句式黑名单均未命中"))

    # 3. 预览/导入一致性（去掉外壳后比较）
    core_imp = re.sub(r"^\s*<section[^>]*>|</section>\s*$", "", import_html, flags=re.S)
    if body_html.strip() in import_html and body_html.strip() in preview_html:
        report.append(("预览/导入同源", "ok", "两份文件由同一 BODY 生成，内容一致"))
    else:
        report.append(("预览/导入同源", "fail", "正文源未同时进入两份文件，请检查生成逻辑"))

    # 4. 占位符
    placeholders = re.findall(r"【[^】]{1,30}】", body_html)
    report.append(("占位符残留", "fail" if placeholders else "ok", "; ".join(placeholders) if placeholders else "无【】占位符"))

    # 5. 图片本地路径 / 空 src / 无图注
    img_issues = []
    for m in re.finditer(r'<img[^>]*>', body_html):
        tag = m.group(0)
        src = re.search(r'src="([^"]*)"', tag)
        if not src or not src.group(1):
            img_issues.append("存在无 src 的 img")
            continue
        url = src.group(1)
        if url.startswith(("file:", "/", "./", "../")):
            img_issues.append(f"本地路径图片（{url}），正式稿必须为平台/网络链接")
        alt = re.search(r'alt="([^"]*)"', tag)
        if not alt or not alt.group(1):
            img_issues.append(f"图片缺 alt：{url[:60]}")
    report.append(("图片链接合规", "fail" if img_issues else "ok", "; ".join(img_issues) if img_issues else "无本地路径/空 src，alt 齐全"))

    # 6. 外链（可选，慢）
    return report


def qa_links(body_html, timeout=6):
    urls = list(dict.fromkeys(re.findall(r'src="(https?://[^"]+)"', body_html)))
    bad = []
    for u in urls:
        try:
            req = urllib.request.Request(u, method="HEAD")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status >= 400:
                    bad.append(f"{r.status} {u[:80]}")
        except Exception as e:
            bad.append(f"{type(e).__name__} {u[:80]}")
    return bad


# ---------------------------------------------------------------- 渲染验收

def viewport_shell(width: int) -> str:
    """固定宽度的预览容器：等价于 build_html 里的 body_margin，但宽度=视口宽。

    为什么必须固定宽度：macOS 的 headless Chrome（当前 2026 版）`--window-size` 的
    布局最小宽度是 500px，而 `--screenshot` 仍按 `--window-size` 输出 375/390/430 宽
    的图——直接截会**裁掉右侧 110px**（正文被切断，且此前"跳过渲染"把这个问题完全
    掩盖了）。把内容装进固定宽度、左对齐的容器，截图前 N px 就是真实手机宽度下的布局。

    `text-spacing-trim:space-all`：Chrome 123+ 默认裁剪中日文全角标点间距，会让行尾
    标点在 scrollWidth 上多算约 4px，误报横向溢出；微信 WebView 无此特性，故关掉，
    使"预览图 + 溢出判定"都与微信一致。
    """
    return (
        f"width:{width}px;box-sizing:border-box;margin:0;padding:20px;"
        "background:#ffffff;color:#333333;"
        "font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;"
        "word-wrap:break-word;overflow-wrap:break-word;text-spacing-trim:space-all;"
    )


RENDER_MEASURE_JS = """
<script>
function gzhDone(){
  var vp = document.getElementById('vp');
  var imgs = vp.getElementsByTagName('img');
  var imgIssues = [];
  var overElems = [];
  var all = vp.querySelectorAll('*');
  for (var i=0;i<all.length;i++){
    var el=all[i];
    if (el.scrollWidth > el.clientWidth + 1) overElems.push(el.tagName + ' sw=' + el.scrollWidth + ' cw=' + el.clientWidth);
  }
  for (var j=0;j<imgs.length;j++){
    var im = imgs[j];
    if (im.naturalWidth && im.offsetWidth){
      var nat = im.naturalWidth / im.naturalHeight;
      var css = im.offsetWidth / im.offsetHeight;
      var err = Math.abs(nat - css) / nat * 100;
      if (err > 3) imgIssues.push('img#' + j + ' nat=' + im.naturalWidth + 'x' + im.naturalHeight + ' css=' + im.offsetWidth + 'x' + im.offsetHeight + ' err=' + err.toFixed(1) + '%');
    } else {
      imgIssues.push('img#' + j + ' NOT_LOADED');
    }
  }
  document.getElementById('result').textContent = 'RESULT vpWidth=' + vp.clientWidth + ' vpScroll=' + vp.scrollWidth + ' pageH=' + document.documentElement.scrollHeight + ' over=[' + overElems.join('; ') + '] imgs=[' + imgIssues.join('; ') + ']';
}
window.addEventListener('load', function(){
  var tries = 0;
  var iv = setInterval(function(){
    tries++;
    var imgs = document.getElementById('vp').getElementsByTagName('img');
    var allLoaded = true;
    for (var i=0;i<imgs.length;i++){ if (!imgs[i].naturalWidth) { allLoaded = false; break; } }
    if (allLoaded || tries > 20) { clearInterval(iv); done(); }
  }, 500);
});
</script>
"""


def render_measure(chrome, body_html, viewport, timeout_s=60):
    """返回 dict：vp_width, vp_scroll, page_h, overflow, img_issues"""
    # text-spacing-trim:space-all —— 这里是在给「微信渲染器」量尺寸，不是给 Chrome 量。
    # Chrome 123+ 默认对中日文全角标点做间距裁剪（text-spacing-trim:normal），会让行尾
    # 全角标点（「」（）等）在 scrollWidth 上多算约 4px，在 375/390 档误报"横向溢出"；
    # 微信 WebView 用的是没有该特性的旧内核，实际不会溢出。关掉裁剪后测量与微信一致，
    # 而固定宽元素、不可断长串这类真溢出仍会照常命中（见 verify_p0 / 实际宽元素自测）。
    meas = (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>MEAS</title></head>\n'
        '<body style="margin:0;background:#ffffff;"><div id="vp" style="%s">\n%s\n</div>\n'
        '<pre id="result" style="position:absolute;left:-9999px;"></pre>\n%s</body></html>'
    ) % (viewport_shell(viewport), body_html, RENDER_MEASURE_JS)
    meas = meas.replace("done()", "gzhDone()")
    tmp = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
    tmp.write(meas)
    tmp.close()
    sandbox_args, profile = chrome_sandbox_args()
    try:
        r = subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", *sandbox_args,
             "--virtual-time-budget=20000", "--dump-dom", "file://" + tmp.name],
            capture_output=True, text=True, timeout=timeout_s)
        dom = r.stdout
    finally:
        os.unlink(tmp.name)
        shutil.rmtree(profile, ignore_errors=True)
    m = re.search(r"RESULT (.*)", dom)
    if not m:
        return {"error": "未捕获测量结果", "dom_head": dom[:200]}
    raw = m.group(1)
    out = {
        "vp_width": int(re.search(r"vpWidth=(\d+)", raw).group(1)),
        "vp_scroll": int(re.search(r"vpScroll=(\d+)", raw).group(1)),
        "page_h": int(re.search(r"pageH=(\d+)", raw).group(1)),
        "overflow": re.search(r"over=\[(.*?)\]", raw).group(1),
        "img_issues": re.search(r"imgs=\[(.*?)\]", raw).group(1),
    }
    return out


def render_shot(chrome, body_html, viewport, out_png, scale=1):
    """整页截图：先测高度，再按高度截全页。"""
    meas = render_measure(chrome, body_html, viewport)
    if "error" in meas:
        return False, meas.get("error", "measure failed")
    h = max(meas["page_h"] + 40, 200)
    tmp = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
    tmp.write(
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width">\n</head>'
        '<body style="margin:0;background:#ffffff;">'
        f'<div style="{viewport_shell(viewport)}">\n{body_html}\n</div></body></html>'
    )
    tmp.close()
    sandbox_args, profile = chrome_sandbox_args()
    try:
        r = subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", *sandbox_args, "--hide-scrollbars",
             f"--force-device-scale-factor={scale}", f"--window-size={viewport},{h}",
             "--virtual-time-budget=20000", f"--screenshot={out_png}", "file://" + tmp.name],
            capture_output=True, text=True, timeout=60)
        ok = os.path.exists(out_png) and os.path.getsize(out_png) > 0
        return ok, "" if ok else r.stderr[:300]
    finally:
        os.unlink(tmp.name)
        shutil.rmtree(profile, ignore_errors=True)


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="公众号一键流水线（SOP v10）")
    ap.add_argument("--md", help="Markdown 人味版正文源文件")
    ap.add_argument("--body-html", help="HTML 正文片段（已含内联样式），与 --md 二选一")
    ap.add_argument("--name", required=True, help="产物名前缀，如 布林约尔松AI就业观_v9")
    ap.add_argument("--title", default="", help="页面标题（预览 <title>）")
    ap.add_argument("--card", default="", help="人物卡 HTML 文件路径（用户确认版，原样插入）")
    ap.add_argument("--series", default="", help="系列名，如 人物与科技")
    ap.add_argument("--episode", default="", help="期号，如 13")
    ap.add_argument("--prev", default="", help="上一篇标题")
    ap.add_argument("--outdir", default=".", help="输出目录（默认当前目录）")
    ap.add_argument("--check-links", action="store_true", help="联网 HEAD 校验图片外链可达性（较慢）")
    ap.add_argument("--skip-render", action="store_true", help="跳过 Chrome 渲染验收（仅质检）")
    ap.add_argument("--save-meas-html", default="", help="调试：保存测量页副本到该路径")
    args = ap.parse_args()

    if not args.md and not args.body_html:
        print("错误：必须提供 --md 或 --body-html 之一")
        sys.exit(2)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    # 1. 读取正文源
    if args.md:
        md_path = Path(args.md)
        if not md_path.exists():
            print(f"错误：找不到 md 文件 {args.md}")
            sys.exit(2)
        md_text = md_path.read_text(encoding="utf-8")
        # title 默认取 H1
        title = args.title or ""
        if not title:
            m = re.search(r"^#\s+(.+)$", md_text, re.M)
            if m:
                title = m.group(1).strip()
    else:
        bh = Path(args.body_html)
        if not bh.exists():
            print(f"错误：找不到 HTML 文件 {args.body_html}")
            sys.exit(2)
        body_html_raw = bh.read_text(encoding="utf-8").strip()
        title = args.title or args.name

    # 2. 人物卡
    card_html = ""
    if args.card:
        cp = Path(args.card)
        if not cp.exists():
            print(f"错误：找不到人物卡文件 {args.card}")
            sys.exit(2)
        card_html = cp.read_text(encoding="utf-8").strip()

    # 3. 生成 BODY / 双 HTML
    if args.md:
        body_html = parse_md(md_text, card_html=card_html,
                             series=args.series, episode=args.episode, prev=args.prev,
                             md_dir=md_path.parent)
    else:
        body_html = body_html_raw
    preview_html, import_html = build_html(body_html, title or args.name)

    preview_path = outdir / f"{args.name}_排版预览.html"
    import_path = outdir / f"{args.name}_导入片段.html"
    preview_path.write_text(preview_html, encoding="utf-8")
    import_path.write_text(import_html, encoding="utf-8")
    print(f"✓ 预览  {preview_path}")
    print(f"✓ 导入  {import_path}")

    # 4. 质检
    print("\n—— 质检门禁 ——")
    report = qa_gates(body_html, preview_html, import_html)
    all_ok = True
    for name, res, detail in report:
        mark = {"ok": "PASS", "warn": "WARN", "fail": "FAIL"}[res]
        print(f"[{mark}] {name}：{detail[:140]}")
        if res == "fail":
            all_ok = False
    if args.check_links:
        print("—— 外链可达性（HEAD）——")
        bad = qa_links(body_html)
        if bad:
            all_ok = False
            for b in bad:
                print(f"[FAIL] {b}")
        else:
            print("[PASS] 全部外链可达")

    # 5. 渲染验收
    # render_state 记录"渲染到底跑没跑成"，它单独决定 INCOMPLETE：
    #   ok       = 三档都出结果且无 FAIL
    #   skipped  = --skip-render（只跳过渲染，质检照旧）
    #   no_chrome= 本机找不到 Chrome（以前只 WARN 然后判通过 → 假通过）
    #   empty    = 找到 Chrome 但没有任何 viewport 产出结果
    #   fail     = 有 viewport 测量/截图报错或验收 FAIL
    render_rows = []
    render_state = "ok"
    chrome = find_chrome()
    if args.skip_render:
        render_state = "skipped"
        print("\n（已跳过 Chrome 渲染验收）")
    elif not chrome:
        render_state = "no_chrome"
        print("\n[WARN] 未找到 Chrome，渲染验收未执行（判定为未完成，不得放行）")
    else:
        print(f"\n—— Chrome 渲染验收（{chrome.split('/')[-1]}）——")
        for vp in VIEWPORTS:
            m = render_measure(chrome, body_html, vp)
            if "error" in m:
                print(f"[{vp}px] FAIL {m['error']}")
                all_ok = False
                render_state = "fail"
                continue
            shot = outdir / f"{args.name}_渲染_{vp}.png"
            ok, err = render_shot(chrome, body_html, vp, str(shot))
            status = "PASS"
            notes = []
            if m["vp_scroll"] > m["vp_width"] + 1:
                status = "FAIL"
                notes.append("横向溢出")
            if m["overflow"]:
                status = "FAIL"
                notes.append("溢出元素: " + m["overflow"][:100])
            if m["img_issues"]:
                status = "FAIL"
                notes.append("图片: " + m["img_issues"][:120])
            if not ok:
                status = "FAIL"
                notes.append("截图失败: " + err[:80])
            if status == "FAIL":
                all_ok = False
                render_state = "fail"
            render_rows.append({
                "viewport": vp, "page_h": m["page_h"], "shot": str(shot), "status": status,
                "overflow": m["overflow"], "img_issues": m["img_issues"],
                "shot_ok": ok, "notes": "; ".join(notes),
            })
            print(f"[{status}] {vp}px  页高 {m['page_h']}px  溢出[{m['overflow'][:40]}]  图片[{m['img_issues'][:60]}]  截图{'✓' if ok else '✗'}")
        if render_state != "fail" and not render_rows:
            render_state = "empty"

    # 6. 判定：质检门禁 FAIL 优先（确定性的坏），其次才是"渲染没跑完"
    if not all_ok:
        verdict = FAIL
    elif render_state != "ok":
        verdict = INCOMPLETE
    else:
        verdict = PASS
    print(f"\n—— 判定：{verdict}（退出码 {EXIT_CODE[verdict]}）——")

    # 7. 质检报告
    report_path = outdir / f"{args.name}_质检报告.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"# 质检报告：{args.name}\n\n")
        f.write(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M')}\n")
        f.write(f"- 总判定：{TOTAL_LABEL[verdict]}\n")
        # 机器可读判定码：gatekeeper 只认这一行（纯文本，不加星号）
        f.write(f"- 判定码：{verdict}\n\n")
        f.write("## 质检门禁\n\n| 检查项 | 结果 | 详情 |\n|---|---|---|\n")
        for name, res, detail in report:
            f.write(f"| {name} | {'✅' if res=='ok' else '⚠️' if res=='warn' else '❌'} | {detail} |\n")
        if args.check_links:
            bad = qa_links(body_html)
            f.write(f"| 外链可达性 | {'✅' if not bad else '❌'} | {'全部可达' if not bad else '；'.join(bad)} |\n")
        f.write("\n## 渲染验收\n\n")
        if render_state == "skipped":
            f.write("已跳过（--skip-render）。**渲染验收未执行，本报告不能作为放行依据。**\n")
        elif render_state == "no_chrome":
            f.write("未执行：本机未找到 Chrome。**渲染验收未执行，本报告不能作为放行依据。**\n")
        elif render_state == "empty" or not render_rows:
            f.write("未执行：Chrome 已找到，但没有任何 viewport 产出结果。**渲染验收未完成，不能放行。**\n")
        else:
            f.write("| 视口 | 页高 | 横向溢出 | 图片比例 | 截图 |\n|---|---|---|---|---|\n")
            for r in render_rows:
                f.write(f"| {r['viewport']}px | {r['page_h']}px | {r['overflow'] or '无'} | {r['img_issues'] or '正常'} | {'✓' if r['shot_ok'] else '✗'} |\n")
            f.write("\n> 溢出 = 元素 scrollWidth 超出容器 clientWidth；图片 = natural 比例与 CSS 显示比例误差 >3% 或未加载。\n")
        if render_state != "ok":
            f.write(f"\n> 判定码 {verdict}：渲染验收未完成 ≠ 通过。"
                    "本地补跑三档渲染（去掉 --skip-render）后重生成报告，再放行。\n")
    print(f"\n✓ 质检报告 {report_path}")
    summary = {
        PASS: "通过 ✅",
        FAIL: "未通过 ❌ 请按 FAIL 项修正后重跑",
        INCOMPLETE: "未完成 ⚠️ 渲染验收未执行，需本地补跑渲染后重生成报告（不得放行）",
    }[verdict]
    print(f"\n总判定：{summary}")

    return EXIT_CODE[verdict]


if __name__ == "__main__":
    sys.exit(main())
