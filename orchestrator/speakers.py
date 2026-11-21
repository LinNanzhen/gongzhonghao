#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""说话人自动对照：把转写里的「说话人 1 / 2」标签映射成姓名与缩写。

优先顺序（宁可不写，也不编名字）：
1. LLM（`provider.complete`）：把每个说话人的前几句发言喂给模型，要求只输出 JSON；
   调用失败或 JSON 解析不出时各重试 1 次；
2. 启发式兜底：片头名单行「嘉宾：A、B、C」、自我介绍「我是 X」、主持人口播；
3. 都失败 → `{}`（transcriber 据此跳过写文件，不 blocked）。

返回结构（值是给人看的对照行，写进 `说话人对照_{date}_{guest}.md`）：
    {
        "说话人 1": {"name": "小雨", "abbr": "XY", "basis": "主持人口播「我是主持人小雨」"},
        "说话人 2": {"name": "杨攀", "abbr": "YP", "basis": "模型对照（speakers.map）"},
    }

无 provider / dry_run 时调用方应跳过 LLM 分支，只跑启发式；本模块不联网、无新依赖。
"""
from __future__ import annotations

import json
import re
from datetime import date

# 值字段：name 姓名 / abbr 缩写 / basis 依据
SpeakerInfo = dict[str, str]
SpeakerMap = dict[str, SpeakerInfo]

# 「说话人 1: 内容」标签前缀（飞书 SRT / 纯文本常见）
LABEL_PREFIX = re.compile(r"^\s*(说话人\s*\d+|Speaker\s*\d+)\s*[：:]\s*(.*)$", re.I)
# 片头名单行：嘉宾：杨攀、张三 / 本期嘉宾是甲、乙（连接词必填，避免「嘉宾分享…」误判）
GUEST_LIST = re.compile(
    r"(?:嘉宾|受访者|来宾|对谈|对话对象)\s*(?:是|为|有|包括|：|:)\s*([^\n。；;！!？?]{2,80})")
# 英文片头：joined by XX / my guest is XX
GUEST_LIST_EN = re.compile(
    r"(?:joined by|with us|guest is|guests are|my guest is)\s+([A-Z][^.;!?]{2,60})")
# 主持人口播：我是主持人小雨 / 我是本期主播 XX
HOST_INTRO = re.compile(
    r"(?:我是|我叫|我是本期|我是今天的)\s*(?:主持人|主播|主持)\s*([\u4e00-\u9fffA-Za-z·]{1,8})")
# 英文主持人口播：I'm your host Emily
HOST_INTRO_EN = re.compile(
    r"\bI(?:'m| am)\s+(?:your\s+)?(?:host|anchor)\s*[,:]?\s*"
    r"([A-Z][A-Za-z'-]*(?:\s+[A-Z][A-Za-z'-]*){0,2})")
# 自我介绍：我是杨攀 / 我叫 Lisa / I'm Emily Chang
# 中文名限 2—4 字且后面必须是标点/空白，否则「我是……说」会被当成名字
SELF_INTRO = re.compile(
    r"(?:我是|我叫)\s*([\u4e00-\u9fff·]{2,4})(?=[，。！？、,.!?\s」』”]|$)")
SELF_INTRO_EN = re.compile(
    r"\bI(?:'m| am)\s+([A-Z][A-Za-z'-]*(?:\s+[A-Z][A-Za-z'-]*){0,2})"
    r"(?=[,.;!?]|\s+(?:and|from|with|speaking|here)\b|$)")
HOST_HINTS = ("大家好", "欢迎来到", "欢迎收听", "欢迎收看", "本期节目", "这一期",
              "这期节目", "直播间", "各位听众", "各位观众",
              "welcome to", "welcome back", "this is")
HOST_HINTS_LOWER = tuple(word.lower() for word in HOST_HINTS)
# 名单切分符
NAME_SPLIT = re.compile(r"[、,，;；/和与及&]|以及|\band\b")
# 常见称谓后缀（从名单/自我介绍里剥掉）
TITLE_SUFFIX = re.compile(r"(老师|先生|女士|博士|教授|同学|总|姐|哥)$")
GENERIC_NAMES = {"主持人", "主播", "嘉宾", "受访者", "记者", "我", "我们", "大家", "他", "她"}

# 姓名（可带英文缩写括号）：杨攀（YP） / Lisa Su (LS)
NAME_WITH_ABBR = re.compile(
    r"^\s*([\u4e00-\u9fff·]{2,6})\s*[（(]\s*([A-Za-z][A-Za-z. ]{0,8})\s*[)）]\s*$")


# ---------------------------------------------------------------- 基础工具


def _norm_label(label: str) -> str:
    """标签归一：去空白、小写，便于模型输出的键与转写标签对齐。"""
    return re.sub(r"\s+", "", (label or "")).lower()


def speaker_labels(segments: list[dict]) -> list[str]:
    """按首次出现顺序去重的非空说话人标签。"""
    out: list[str] = []
    seen: set[str] = set()
    for seg in segments or []:
        label = (seg.get("speaker") or "").strip()
        key = _norm_label(label)
        if not label or key in seen:
            continue
        seen.add(key)
        out.append(label)
    return out


def _samples(segments: list[dict], label: str, *, max_segs: int = 4,
             max_chars: int = 220) -> str:
    """取某标签的前几段发言，拼成给模型/启发式看的线索文本。"""
    picked: list[str] = []
    for seg in segments or []:
        if _norm_label(seg.get("speaker") or "") != _norm_label(label):
            continue
        text = re.sub(r"\s+", " ", (seg.get("text") or "")).strip()
        if text:
            picked.append(text)
        if len(picked) >= max_segs:
            break
    return " / ".join(picked)[:max_chars]


def _clean_name(raw: str) -> tuple[str, str]:
    """清洗姓名：剥称谓、拆显式缩写。返回（姓名, 缩写）。"""
    name = (raw or "").strip().strip("\"'“”‘’《》<>")
    abbr = ""
    m = NAME_WITH_ABBR.match(name)
    if m:
        return m.group(1).strip(), m.group(2).strip().upper()
    name = TITLE_SUFFIX.sub("", name).strip()
    # 英文名：取首字母作缩写
    if re.fullmatch(r"[A-Za-z][A-Za-z. ]{0,30}", name):
        letters = re.findall(r"[A-Za-z]+", name)
        abbr = "".join(w[0].upper() for w in letters[:4])
    return name, abbr


def _split_names(chunk: str) -> list[tuple[str, str]]:
    """把「杨攀（YP）、张三老师」切成 (姓名, 缩写) 列表。"""
    picked: list[tuple[str, str]] = []
    for part in NAME_SPLIT.split(chunk or ""):
        raw = re.sub(r"(等|等人|他们|她们)$", "", (part or "").strip())
        name, abbr = _clean_name(raw)
        if not (1 < len(name) <= 20) or name in GENERIC_NAMES:
            continue
        # 中文名一般 2—4 字；长串多半是整句，不当作人名
        if re.fullmatch(r"[\u4e00-\u9fff·]{5,}", name):
            continue
        picked.append((name, abbr))
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for name, abbr in picked:
        if name in seen:
            continue
        seen.add(name)
        out.append((name, abbr))
    return out


def _entry(name: str, abbr: str, basis: str) -> SpeakerInfo:
    return {"name": name, "abbr": (abbr or "").strip().upper(), "basis": basis}


# ---------------------------------------------------------------- LLM


def _extract_json(raw: str) -> dict:
    """从可能带代码块/前后缀的模型输出里抠出 JSON 对象。"""
    body = (raw or "").strip()
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", body)
    a, b = body.find("{"), body.rfind("}")
    if a == -1 or b <= a:
        return {}
    try:
        data = json.loads(body[a:b + 1])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _coerce_entry(value) -> tuple[str, str] | None:
    """模型值容错：{"name","abbr"} 或 "姓名 缩写" 两种写法。"""
    if isinstance(value, dict):
        name = str(value.get("name") or "").strip()
        abbr = str(value.get("abbr") or "").strip()
        if not name:
            return None
        name, guessed = _clean_name(name)
        return name, (abbr or guessed)
    if isinstance(value, str):
        parts = value.strip().split()
        if not parts:
            return None
        name, guessed = _clean_name(parts[0])
        return name, (parts[1] if len(parts) > 1 else guessed)
    return None


def _llm_map(segments: list[dict], labels: list[str], provider) -> SpeakerMap:
    """调模型对照；调用/解析失败各重试 1 次，仍失败返回 {}。"""
    blocks = []
    for label in labels:
        blocks.append(f"### {label}\n{_samples(segments, label) or '（无内容）'}")
    system = (
        "你是访谈转写校对员。只输出一个 JSON 对象，不要解释、不要代码块。"
        "键是转写里的说话人标签原样（如「说话人 1」），值是该说话人的姓名与缩写："
        '{"说话人 1": {"name": "杨攀", "abbr": "YP"}}。'
        "只能依据给出的发言线索；线索不足以判断的说话人不要写进 JSON，不要编造姓名；"
        "缩写用英文姓名首字母或中文姓名拼音首字母（如 杨攀→YP）。"
    )
    prompt = ("以下是转写中每个说话人的前几段发言（可能没有自我介绍）：\n\n"
              + "\n\n".join(blocks)
              + "\n\n请输出说话人对照 JSON。")

    for _attempt in range(2):        # 失败重试 1 次
        try:
            raw = provider.complete(task="speakers.map", system=system, prompt=prompt,
                                    max_tokens=1200, temperature=0)
        except Exception:            # noqa: BLE001 - 对照失败不能拖垮转写
            continue
        data = _extract_json(raw)
        by_norm = {_norm_label(label): label for label in labels}
        out: SpeakerMap = {}
        for key, value in data.items():
            label = by_norm.get(_norm_label(str(key)))
            if label is None:
                continue
            parsed = _coerce_entry(value)
            if parsed is None or parsed[0] in GENERIC_NAMES:
                continue
            name, abbr = parsed
            out[label] = _entry(name, abbr, "模型对照（speakers.map）")
        if out:
            return out
    return {}


# ---------------------------------------------------------------- 启发式


def heuristic_speaker_map(segments: list[dict]) -> SpeakerMap:
    """不用模型的兜底：名单行 + 自我介绍 + 主持人口播。判不出就返回 {}。"""
    labels = speaker_labels(segments)
    if not labels:
        return {}

    texts: list[tuple[str, str]] = []      # (标签, 合并文本)
    for label in labels:
        merged = " ".join(re.sub(r"\s+", " " , (s.get("text") or "")).strip()
                          for s in segments
                          if _norm_label(s.get("speaker") or "") == _norm_label(label))
        texts.append((label, merged))

    mapping: SpeakerMap = {}
    used: set[str] = set()

    # 1) 主持人口播（有名字用名字；只判出角色就写「主持人」）
    host_label = ""
    for label, text in texts:
        m = HOST_INTRO.search(text) or HOST_INTRO_EN.search(text)
        if m:
            name, abbr = _clean_name(m.group(1))
            mapping[label] = _entry(name, abbr, "主持人口播「我是主持人…」")
            used.add(name)
            host_label = label
            break
    if not host_label:
        for label, text in texts:
            if any(word in text.lower() for word in HOST_HINTS_LOWER):
                mapping[label] = _entry("主持人", "", "主持人口播（未报姓名）")
                used.add("主持人")
                host_label = label
                break

    # 2) 片头名单行：嘉宾：杨攀、张三 / joined by Lisa Su
    listed: list[tuple[str, str]] = []
    for _label, text in texts:
        for pattern in (GUEST_LIST, GUEST_LIST_EN):
            for m in pattern.finditer(text):
                chunk = m.group(1)
                names = _split_names(chunk)
                # 长句里只抠出一个词，多半不是名单（如「嘉宾分享观察、实践与判断」）
                if len(names) < 2 and len(chunk) > 12:
                    continue
                for name, abbr in names:
                    if all(name != seen_name for seen_name, _ in listed):
                        listed.append((name, abbr))

    # 3) 自我介绍：我是杨攀 / I'm Emily Chang
    intros: dict[str, tuple[str, str]] = {}
    for label, text in texts:
        if label == host_label:
            continue
        m = SELF_INTRO.search(text) or SELF_INTRO_EN.search(text)
        if not m:
            continue
        name, abbr = _clean_name(m.group(1))
        if name and name not in GENERIC_NAMES:
            intros[label] = (name, abbr)

    # 4) 合并：自我介绍优先于名单，名单按出现顺序补空
    for label in labels:
        if label in mapping:
            continue
        if label in intros:
            name, abbr = intros[label]
            if name not in used:
                mapping[label] = _entry(name, abbr, "自我介绍「我是…」")
                used.add(name)
    rest = [(name, abbr) for name, abbr in listed if name not in used]
    for label in labels:
        if label in mapping or not rest:
            continue
        name, abbr = rest.pop(0)
        mapping[label] = _entry(name, abbr, "片头名单「嘉宾：…」")
        used.add(name)

    return mapping


# ---------------------------------------------------------------- 对外


def guess_speaker_map(segments: list[dict], provider=None) -> SpeakerMap:
    """猜说话人对照表：LLM 优先，失败用启发式兜底，再不行返回 {}。

    `provider` 为 None（或 dry_run）时跳过模型，只跑启发式。
    """
    if not segments:
        return {}
    labels = speaker_labels(segments)
    if not labels:
        return {}          # 转写里没有说话人标签，无从对照

    # 两路都跑，按标签取并集（模型优先，启发式补漏）：
    # 模型偏保守，只给有把握的；启发式能抓到片头名单/自我介绍，合起来覆盖更全。
    via_heur = heuristic_speaker_map(segments)
    via_llm = _llm_map(segments, labels, provider) if provider is not None else {}
    if not via_llm:
        return via_heur
    merged = dict(via_heur)
    merged.update(via_llm)
    allowed = set(labels)
    return {label: info for label, info in merged.items() if label in allowed}


def render_speaker_map(mapping: SpeakerMap, *, guest: str = "",
                       today: str = "") -> str:
    """把对照表渲染成 markdown（transcriber 直接写盘用）。"""
    day = today or date.today().isoformat()
    name = guest or "未命名"
    lines = [
        f"# 说话人对照 · {name}",
        "",
        f"> 生成：{day}（speakers 自动对照，**需人工复核**；不确定的说话人留空，不要编）",
        "> 用途：writer 引用原话时用「缩写：」，正文行文用「姓名」。",
        "",
        "| 标签 | 姓名 | 缩写 | 依据 |",
        "|---|---|---|---|",
    ]
    for label, info in mapping.items():
        lines.append("| {label} | {name} | {abbr} | {basis} |".format(
            label=label,
            name=(info.get("name") or "—"),
            abbr=(info.get("abbr") or "—"),
            basis=(info.get("basis") or "—").replace("|", "／"),
        ))
    lines.append("")
    return "\n".join(lines)


__all__ = ["guess_speaker_map", "heuristic_speaker_map", "render_speaker_map",
           "speaker_labels"]
