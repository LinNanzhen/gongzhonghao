#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""feishu_bot · 飞书自建应用（长连接）：推送候选卡片 + 接收按钮点击

为什么用长连接：ECS 没有公网回调地址，长连接由 SDK 主动向飞书建链，
不用开端口、不用备案域名。发送消息走裸 HTTP（无第三方依赖），
只有「接收事件」需要 lark-oapi 的 WebSocket 客户端。

用法（在 gongzhonghao/ 下）：
    python3 -m feishu_bot test            # 发一条测试消息
    python3 -m feishu_bot push            # 把 nightly 的 last_run.json 推成卡片
    python3 -m feishu_bot serve           # 常驻：收按钮点击 → 写 state/choice.json

环境变量（放 /etc/gzh/env 或 ~/.config/gzh/env）：
    FEISHU_APP_ID / FEISHU_APP_SECRET      自建应用凭证
    GZH_FEISHU_CHAT_ID                     推送目标群 chat_id（oc_xxx）
    GZH_STATE_DIR                          默认 /opt/gzh/state（本地可指向 workspace/.state）

卡片按钮点下去只做一件事：把 {key, title, ts} 写进 state/choice.json，
然后由 08:30 的推送或下一轮 nightly 读它。这样机器人不阻塞、可重放。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE = Path(os.environ.get("GZH_WORKSPACE") or HERE.parent)
DEFAULT_STATE = Path(os.environ.get("GZH_STATE_DIR") or (WORKSPACE / ".state"))
LAST_RUN = WORKSPACE / "播客监控" / "nightly" / "last_run.json"
CHOICE = DEFAULT_STATE / "choice.json"
BASE = "https://open.feishu.cn/open-apis"


# ---------------------------------------------------------------- 配置


def load_env() -> None:
    """把 /etc/gzh/env、~/.config/gzh/env、仓库 .env 里的 KEY=VALUE 读进 os.environ。

    已存在的环境变量优先（systemd 已经注入时不会被覆盖）。
    """
    for path in (Path("/etc/gzh/env"), Path.home() / ".config/gzh/env",
                 WORKSPACE / ".env", HERE / ".env"):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and val and key not in os.environ:
                os.environ[key] = val


CHAT_FILE = DEFAULT_STATE / "chat_id.txt"


def chat_id() -> str:
    """优先 env；没有就用 bot 自己从群里收到的 chat_id（见 remember_chat）。"""
    env = (os.environ.get("GZH_FEISHU_CHAT_ID") or "").strip()
    if env:
        return env
    try:
        saved = CHAT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        saved = ""
    if saved:
        return saved
    raise SystemExit("还不知道推给哪个群：把机器人拉进群，在群里发一句「1」，它自己记住")


def remember_chat(cid: str) -> bool:
    """第一次看到群消息就记下来，省得手工抄 oc_xxx。返回是否新记住。"""
    if not cid or (os.environ.get("GZH_FEISHU_CHAT_ID") or "").strip():
        return False
    if CHAT_FILE.exists() and CHAT_FILE.read_text(encoding="utf-8").strip() == cid:
        return False
    DEFAULT_STATE.mkdir(parents=True, exist_ok=True)
    CHAT_FILE.write_text(cid + "\n", encoding="utf-8")
    print(f"[chat] 记住了群 chat_id={cid} → {CHAT_FILE}")
    return True


def need(name: str) -> str:
    val = (os.environ.get(name) or "").strip()
    if not val:
        raise SystemExit(f"缺少环境变量 {name}：写进 /etc/gzh/env 后重试")
    return val


# ---------------------------------------------------------------- webhook 通道
#
# 为什么留两条通道：自建应用需要租户给 API 权限，撞上 21001 就只能退到
# 「群自定义机器人」（webhook）——它不经过应用权限体系，但收不到按钮回调，
# 所以卡片按钮改成「链接」：点开 → 浏览器访问 ECS 上的 choice_server →
# 写 choice.json。@ 机器人回复数字那条路只有应用通道才有。

WEBHOOK_ENVS = ("GZH_FEISHU_WEBHOOK", "FEISHU_WEBHOOK")


def webhook_url() -> str:
    for env in WEBHOOK_ENVS:
        url = (os.environ.get(env) or "").strip()
        if url:
            return url
    return ""


def choice_base() -> str:
    """选择回传服务的公网基址，如 http://1.2.3.4:8788；没配就退回应用回调。"""
    return (os.environ.get("GZH_CHOICE_BASE") or "").strip().rstrip("/")


def _sign(secret: str, timestamp: str) -> str:
    """自定义机器人开启「签名校验」时的 string_to_sign = ts + "\n" + secret。"""
    import base64
    import hashlib
    import hmac
    digest = hmac.new(f"{timestamp}\n{secret}".encode("utf-8"), b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def post_webhook(payload: dict) -> dict:
    """POST 到群自定义机器人。飞书出错时也可能返回 HTTP 200 + code!=0，要判 code。"""
    url = webhook_url()
    if not url:
        raise SystemExit("没有配置 webhook：把 GZH_FEISHU_WEBHOOK 写进 /etc/gzh/env")
    body = dict(payload)
    secret = (os.environ.get("GZH_FEISHU_WEBHOOK_SECRET") or "").strip()
    if secret:
        ts = str(int(time.time()))
        body = {"timestamp": ts, "sign": _sign(secret, ts), **body}
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"webhook HTTP {exc.code}: {exc.read()[:200]!r}")
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = {}
    code = parsed.get("code", parsed.get("StatusCode", 0))
    if code not in (0, None):
        raise SystemExit(f"webhook 失败：code={code} {parsed.get('msg', '')}")
    return parsed


def send_webhook_card(card: dict) -> dict:
    return post_webhook({"msg_type": "interactive", "card": card})


def send_webhook_text(text: str) -> dict:
    return post_webhook({"msg_type": "text", "content": {"text": text}})


# ---------------------------------------------------------------- 发消息（裸 HTTP）


def tenant_token() -> str:
    app_id = need("FEISHU_APP_ID")
    secret = need("FEISHU_APP_SECRET")
    body = json.dumps({"app_id": app_id, "app_secret": secret}).encode()
    req = urllib.request.Request(f"{BASE}/auth/v3/tenant_access_token/internal",
                                 data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    if data.get("code") != 0:
        raise SystemExit(f"拿 token 失败：{data.get('code')} {data.get('msg')}")
    return data["tenant_access_token"]


def send_message(msg_type: str, content: dict | str) -> dict:
    cid = chat_id()
    body = json.dumps({
        "receive_id": cid,
        "msg_type": msg_type,
        "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
    }).encode()
    req = urllib.request.Request(
        f"{BASE}/im/v1/messages?receive_id_type=chat_id", data=body,
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": f"Bearer {tenant_token()}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    if data.get("code") != 0:
        raise SystemExit(f"发消息失败：{data.get('code')} {data.get('msg')}")
    return data


# ---------------------------------------------------------------- 卡片


def build_card(record: dict) -> dict:
    cands = record.get("candidates") or []
    lines = []
    if not cands:
        lines.append("今晚没有新候选（窗口内没有新节目）。")
    for i, c in enumerate(cands, 1):
        reasons = "；".join(c.get("reasons") or [])
        lines.append(f"**{i}. {c.get('title', '')}**\n{c.get('date', '')} · 分 {c.get('score', '')} · {reasons}")
    draft = record.get("draft") or {}
    if draft.get("ok"):
        status = "✅ v0 底稿已生成" if not draft.get("dry_run") else "（dry-run）"
    else:
        status = "⚠️ 底稿未生成：" + str(draft.get("reason", ""))[:120]
    errors = record.get("errors") or []
    if errors:
        lines.append("**错误**：" + "；".join(str(e)[:120] for e in errors[-3:]))

    actions = []
    base = choice_base()
    token = (os.environ.get("GZH_CHOICE_TOKEN") or "").strip()
    webhook_mode = bool(webhook_url())
    for i, c in enumerate(cands, 1):
        btn = {
            "tag": "button",
            "text": {"tag": "plain_text", "content": f"选 {i}"},
            "type": "primary" if i == 1 else "default",
            "value": {"key": c.get("key", ""), "idx": i, "title": c.get("title", "")},
        }
        if base:  # webhook 通道：按钮只能带链接，点开由 choice_server 记账
            btn["url"] = f"{base}/c?t={token}&i={i}"
            btn.pop("value", None)
        elif webhook_mode:
            # webhook 收不到回调，又没有回传服务：按钮是死的，宁可不放
            continue
        actions.append(btn)
    elements: list[dict] = [
        {"tag": "markdown", "content": "\n\n".join(lines) or "（空）"},
        {"tag": "markdown", "content": f"运行时间：{record.get('started', '')} · 用时 {record.get('elapsed', '?')}s\n{status}"},
    ]
    if actions:
        elements.append({"tag": "action", "actions": actions})
    elif cands and webhook_mode:
        elements.append({"tag": "markdown",
                         "content": "（选择回传未配置：在会话里回我数字 1/2/3 也行）"})

    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue",
                   "title": {"tag": "plain_text",
                             "content": f"今晚的选题 · {datetime.now().strftime('%m-%d')}"}},
        "elements": elements,
    }


def push(via: str = "auto") -> int:
    if not LAST_RUN.is_file():
        print(f"没有 {LAST_RUN}：先跑 python3 -m nightly run")
        return 2
    record = json.loads(LAST_RUN.read_text(encoding="utf-8"))
    card = build_card(record)
    use = via
    if use == "auto":
        use = "webhook" if webhook_url() else "app"
    if use == "webhook":
        post_webhook({"msg_type": "interactive", "card": card})
        print("已推送（webhook 通道）")
        return 0
    data = send_message("interactive", card)
    print(f"已推送（应用通道）：message_id={data.get('data', {}).get('message_id', '?')}")
    return 0


# ---------------------------------------------------------------- 收事件（长连接）


def record_choice(value: dict) -> dict:
    DEFAULT_STATE.mkdir(parents=True, exist_ok=True)
    payload = {"key": value.get("key", ""), "idx": value.get("idx", 0),
               "title": value.get("title", ""),
               "ts": datetime.now().isoformat(timespec="seconds"), "source": "feishu"}
    tmp = CHOICE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CHOICE)
    print(f"[choice] {payload['idx']}. {payload['title'][:40]} → {CHOICE}")
    return payload


def serve() -> int:
    """lark-oapi 1.8 的长连接：收 card.action.trigger 与 im.message.receive_v1。

    1.8 没有 builder 风格，回包直接构造 P2CardActionTriggerResponse 并挂 toast。
    """
    try:
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            CallBackToast, P2CardActionTrigger, P2CardActionTriggerResponse)
    except ImportError as exc:                # noqa: BLE001
        print(f"缺 lark-oapi（{exc}）：/opt/gzh-venv/bin/pip install 'lark-oapi>=1.4,<2'")
        return 2

    def on_card(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        resp = P2CardActionTriggerResponse()
        try:
            ctx = getattr(getattr(data, "event", None), "context", None)
            remember_chat(getattr(ctx, "open_chat_id", "") or "")
            action = getattr(getattr(data, "event", None), "action", None)
            value = getattr(action, "value", None) or {}
            if isinstance(value, str):
                value = json.loads(value)
            got = record_choice(value)
            resp.toast = CallBackToast({"type": "success",
                                        "content": f"已记下第 {got['idx']} 篇，早上出 v1"})
        except Exception as exc:              # noqa: BLE001
            print(f"[warn] 处理按钮失败：{exc}")
            resp.toast = CallBackToast({"type": "error", "content": f"记录失败：{exc}"})
        return resp

    def on_message(data: P2ImMessageReceiveV1) -> None:
        """文本「1/2/3」也能选；手机端不方便点卡片时用。顺带记住群 id。"""
        cid = getattr(data.event.message, "chat_id", "") or ""
        if remember_chat(cid):
            send_message("text", {"text": "记住了这个群：以后每天早上把候选卡片推这里。"})
        try:
            text = json.loads(data.event.message.content or "{}").get("text", "").strip()
        except ValueError:
            return
        if text not in ("1", "2", "3"):
            return
        try:
            record = json.loads(LAST_RUN.read_text(encoding="utf-8"))
            cand = (record.get("candidates") or [])[int(text) - 1]
        except (OSError, ValueError, IndexError):
            print(f"[warn] 收到 {text}，但找不到对应候选")
            return
        got = record_choice({"key": cand.get("key", ""), "idx": int(text),
                             "title": cand.get("title", "")})
        send_message("text", {"text": f"已记下第 {got['idx']} 篇：{got['title'][:60]}"})

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_card_action_trigger(on_card)
               .register_p2_im_message_receive_v1(on_message)
               .build())
    client = lark.ws.Client(need("FEISHU_APP_ID"), need("FEISHU_APP_SECRET"),
                            event_handler=handler, log_level=lark.LogLevel.INFO)
    print("长连接启动：在飞书里点卡片按钮 / 回 1-3")
    while True:
        try:
            client.start()                   # 阻塞；断线由 SDK auto_reconnect 处理
        except KeyboardInterrupt:
            return 0
        except Exception as exc:             # noqa: BLE001
            print(f"[warn] 连接断开：{exc}，10 秒后重试")
            time.sleep(10)


# ---------------------------------------------------------------- CLI


def main(argv=None) -> int:
    load_env()
    ap = argparse.ArgumentParser(prog="python3 -m feishu_bot")
    ap.add_argument("cmd", choices=["test", "push", "serve"])
    ap.add_argument("--via", choices=["auto", "webhook", "app"], default="auto")
    args = ap.parse_args(argv)
    if args.cmd == "test":
        if webhook_url():
            mode = "webhook" if not args.via == "app" else "app"
            if mode == "webhook":
                send_webhook_text("gzh webhook 通道已通 ✅")
                print("测试消息已发（webhook 通道）")
                return 0
        send_message("text", {"text": "gzh 机器人已上线 ✅"})
        print("测试消息已发（应用通道）")
        return 0
    if args.cmd == "push":
        return push(args.via)
    return serve()


if __name__ == "__main__":
    sys.exit(main())
