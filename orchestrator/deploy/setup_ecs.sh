#!/bin/sh
# 在 ECS 上执行（由 deploy_ecs.sh 调用）：解包 → 换 unit → 装依赖 → 开定时器
# 假设 /tmp/gzh-deploy.tgz 已上传，内含 gongzhonghao/ 与 stt/。
set -eu
TS=$(date +%Y%m%d_%H%M%S)
ROOT=/opt/gongzhonghao
ENVF=/etc/gzh/env

echo "① 备份 $ROOT → $ROOT.bak_$TS"
[ -d "$ROOT" ] && cp -a "$ROOT" "$ROOT.bak_$TS"

echo "② 解包代码"
cd /opt
tar xzf /tmp/gzh-deploy.tgz
chown -R gzh:gzh /opt/gongzhonghao /opt/stt
chmod +x /opt/stt/stt

echo "③ 目录与权限"
install -d -o gzh -g gzh -m 755 /opt/gzh-state
install -d -o gzh -g gzh -m 755 /opt/gzh-workspace/播客监控/nightly

echo "④ 环境变量（缺啥补啥，不覆盖已有值）"
# upsert：已有同名键就替换（配置改动要能落地），否则追加
add_env() {
  # 值里有空格就加引号：systemd EnvironmentFile 会剥掉，`sh . env` 也能安全 source
  case "$2" in
    *" "*) val="\"$2\"" ;;
    *)     val="$2" ;;
  esac
  if grep -q "^$1=" "$ENVF" 2>/dev/null; then
    # 用 | 当分隔符，值里含 / 也不怕
    sed -i "s|^$1=.*|$1=$val|" "$ENVF"
  else
    printf '%s=%s\n' "$1" "$val" >> "$ENVF"
  fi
}
add_env GZH_STT_CMD "/opt/stt/stt -m /opt/stt-models/faster-whisper-small --device cpu --compute-type int8 --threads 2"
add_env GZH_STATE_DIR "/opt/gzh-state"

# 链接直取用的 yt-dlp（系统那个 /usr/bin/yt-dlp 是 2026.03.17，太旧；指纹伪装要 curl_cffi）
if [ ! -x /opt/ytdlp-venv/bin/yt-dlp ]; then
  echo "   装 /opt/ytdlp-venv（yt-dlp + curl_cffi）"
  python3 -m venv /opt/ytdlp-venv >/dev/null 2>&1 || echo "   [warn] venv 创建失败"
  /opt/ytdlp-venv/bin/pip install -q -i "${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}" \
    yt-dlp curl_cffi >/dev/null 2>&1 || echo "   [warn] yt-dlp 装失败（链接模式不可用，不影响夜跑）"
fi
if [ -x /opt/ytdlp-venv/bin/yt-dlp ]; then
  add_env YTDLP "/opt/ytdlp-venv/bin/yt-dlp"
fi

# 选择回传服务（webhook 卡片的按钮落地）：令牌只生成一次，重部署不轮换
if ! grep -q '^GZH_CHOICE_TOKEN=' "$ENVF" 2>/dev/null; then
  add_env GZH_CHOICE_TOKEN "$(openssl rand -hex 16)"
  echo "   生成 GZH_CHOICE_TOKEN（卡片链接口令）"
fi
CHOICE_PORT="${GZH_CHOICE_PORT:-8788}"
add_env GZH_CHOICE_PORT "$CHOICE_PORT"
PUB_IP="${GZH_PUBLIC_IP:-$(curl -s -m 4 https://ipinfo.io/ip 2>/dev/null || true)}"
if [ -n "$PUB_IP" ]; then
  add_env GZH_CHOICE_BASE "http://$PUB_IP:$CHOICE_PORT"
  echo "   选择回传基址 http://$PUB_IP:$CHOICE_PORT（需要在安全组放行 $CHOICE_PORT/tcp）"
else
  echo "   [warn] 拿不到公网 IP，GZH_CHOICE_BASE 留空：卡片按钮会退回应用回调（不可用）"
fi
# 模型通道：重部署不许冲掉已选模型（2026-11-21 踩过：这里硬写 deepseek-chat，
# 而 key 是 OpenCode Go 的，02:00 直接 HTTP 400）。只有 key 在、模型空时才补默认。
LLM_KEY="$(grep -m1 '^GZH_LLM_API_KEY=' "$ENVF" 2>/dev/null | cut -d= -f2- | tr -d '\"')"
LLM_MODEL="$(grep -m1 '^GZH_LLM_MODEL=' "$ENVF" 2>/dev/null | cut -d= -f2- | tr -d '\"')"
if [ -n "$LLM_KEY" ] && [ -z "$LLM_MODEL" ]; then
  add_env GZH_LLM_PROVIDER "openai-compat"
  add_env GZH_LLM_MODEL "${GZH_LLM_MODEL_DEFAULT:-gpt-6-luna}"
  add_env GZH_LLM_BASE_URL "${GZH_LLM_BASE_URL_DEFAULT:-https://opencode.ai/inference/go/openai/v1}"
fi
chmod 600 "$ENVF"
chown gzh:gzh "$ENVF" 2>/dev/null || true

# 小工具：以后加/改凭证不用再走一遍部署
if [ ! -x /usr/local/bin/gzh-setenv ]; then
  cat > /usr/local/bin/gzh-setenv <<'SETENV'
#!/bin/sh
# gzh-setenv KEY VALUE：写 /etc/gzh/env（600）并重启受影响的服务
set -eu
[ $# -eq 2 ] || { echo "用法：gzh-setenv KEY VALUE"; exit 2; }
KEY=$1; VAL=$2
case "$VAL" in *" "*) Q="\"$VAL\"" ;; *) Q="$VAL" ;; esac
if grep -q "^$KEY=" /etc/gzh/env 2>/dev/null; then
  sed -i "s|^$KEY=.*|$KEY=$Q|" /etc/gzh/env
else
  printf '%s=%s\n' "$KEY" "$Q" >> /etc/gzh/env
fi
chmod 600 /etc/gzh/env; chown gzh:gzh /etc/gzh/env
case "$KEY" in
  FEISHU_*) systemctl try-restart gzh-bot.service 2>/dev/null || true ;;
  GZH_LLM_*) : ;;
esac
echo "已写 $KEY（值不回显）"
SETENV
  chmod 755 /usr/local/bin/gzh-setenv
fi

echo "⑤ 机器人长连接依赖（lark-oapi）"
if [ ! -x /opt/gzh-venv/bin/python ]; then
  python3 -m venv /opt/gzh-venv
  /opt/gzh-venv/bin/pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple 'lark-oapi>=1.4,<2'
fi
/opt/gzh-venv/bin/python -c "import lark_oapi; print('   lark_oapi', lark_oapi.__version__)" 2>/dev/null || \
  /opt/gzh-venv/bin/pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple 'lark-oapi>=1.4,<2'

echo "⑥ systemd 单元"
install -m 644 /opt/gongzhonghao/orchestrator/deploy/gzh-nightly.service /etc/systemd/system/
install -m 644 /opt/gongzhonghao/orchestrator/deploy/gzh-nightly.timer   /etc/systemd/system/
install -m 644 /opt/gongzhonghao/orchestrator/deploy/gzh-push.service    /etc/systemd/system/
install -m 644 /opt/gongzhonghao/orchestrator/deploy/gzh-push.timer      /etc/systemd/system/
install -m 644 /opt/gongzhonghao/orchestrator/deploy/gzh-review.service  /etc/systemd/system/
install -m 644 /opt/gongzhonghao/orchestrator/deploy/gzh-review.timer    /etc/systemd/system/
install -m 644 /opt/gongzhonghao/orchestrator/deploy/gzh-bot.service     /etc/systemd/system/
install -m 644 /opt/gongzhonghao/orchestrator/deploy/gzh-choice.service  /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now gzh-nightly.timer gzh-push.timer gzh-review.timer
# 选择回传服务：有口令就常驻（webhook 通道的按钮靠它记账）
if grep -q '^GZH_CHOICE_TOKEN=' "$ENVF"; then
  systemctl enable --now gzh-choice.service
  systemctl restart gzh-choice.service
else
  echo "   （未配置 GZH_CHOICE_TOKEN，gzh-choice 先不启动）"
fi
# 机器人长连接默认不开：应用被租户策略 21001 拦着，连上也发不出消息（白占 ~200MB）。
# 只有显式写 GZH_BOT_ENABLED=1（且凭证齐）才启动，留给以后权限放开时一键启用的口子。
if grep -q '^GZH_BOT_ENABLED=1' "$ENVF" && grep -q '^FEISHU_APP_ID=' "$ENVF" \
   && grep -q '^FEISHU_APP_SECRET=' "$ENVF"; then
  systemctl enable --now gzh-bot.service
else
  systemctl disable --now gzh-bot.service 2>/dev/null || true
  echo "   （gzh-bot 保持关闭：当前走 webhook 通道；要开长连接就 gzh-setenv GZH_BOT_ENABLED 1）"
fi

echo "⑦ 自检"
/usr/bin/python3 -c "import sys; sys.path.insert(0,'$ROOT'); import nightly, feishu_bot, choice_server; print('   nightly/feishu_bot/choice_server import ok')"
systemctl list-timers 'gzh-*' --all --no-pager | head -5
echo "完成（备份：$ROOT.bak_$TS）"
