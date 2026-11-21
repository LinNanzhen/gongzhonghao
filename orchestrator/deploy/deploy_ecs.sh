#!/bin/sh
# 从 Mac 一次性把代码 + STT 包装器 + systemd 单元推到 ECS，并装好夜间/推送/机器人。
# 用法：sh gongzhonghao/orchestrator/deploy/deploy_ecs.sh
# 前提：本机 workbench CLI 已登录（凭证在 .tools/wb-home/.workbench/config.json）
set -eu
INSTANCE=${GZH_INSTANCE:-i-bp1gio4o2xsyssfqml8x}
WS=${GZH_LOCAL_WORKSPACE:-$HOME/Desktop/公众号}
WB="$WS/.tools/wb-home"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

[ -f "$WB/.workbench/config.json" ] || { echo "缺 workbench 凭证：$WB/.workbench/config.json"; exit 1; }

echo "① 打包代码（排除 pycache / runs / inbox / .env）"
mkdir -p "$STAGE/pkg/stt"
tar czf "$STAGE/gzh.tgz" -C "$WS" \
  --exclude '__pycache__' --exclude '*.pyc' --exclude '.env' \
  --exclude 'gongzhonghao/.git' --exclude '.git' \
  --exclude 'orchestrator/runs' --exclude 'orchestrator/inbox' \
  gongzhonghao
tar xzf "$STAGE/gzh.tgz" -C "$STAGE/pkg"

echo "② 打包 STT 入口（ECS 用 cpu/int8 + /opt/stt-models）"
# stt.py 来源优先级：$GZH_STT_SRC → 仓库内 tools/stt → 工作区 .tools/stt
STT_SRC=${GZH_STT_SRC:-}
if [ -z "$STT_SRC" ]; then
  for c in "$WS/gongzhonghao/tools/stt/stt.py" "$WS/.tools/stt/stt.py"; do
    [ -f "$c" ] && STT_SRC=$c && break
  done
fi
[ -n "$STT_SRC" ] || { echo "找不到 stt.py：设 GZH_STT_SRC 指向它"; exit 1; }
echo "   STT 源：$STT_SRC"
cp "$STT_SRC" "$STAGE/pkg/stt/stt.py"
cat > "$STAGE/pkg/stt/stt" <<'WRAP'
#!/bin/sh
# ECS 版入口：与本地 wrapper 同接口，解释器换成 /opt/stt-venv
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec /opt/stt-venv/bin/python "$DIR/stt.py" "$@"
WRAP
chmod +x "$STAGE/pkg/stt/stt"
tar czf "$STAGE/gzh-deploy.tgz" -C "$STAGE/pkg" .

echo "③ 上传并执行远端 setup"
HOME="$WB" workbench upload "$STAGE/gzh-deploy.tgz" /tmp/ -i "$INSTANCE" -f
# setup_ecs.sh 本身在包里：先解到 /tmp/gzh-stage，再执行它（它负责覆盖 /opt）
HOME="$WB" workbench exec -i "$INSTANCE" --command "set -e
  rm -rf /tmp/gzh-stage && mkdir -p /tmp/gzh-stage
  tar xzf /tmp/gzh-deploy.tgz -C /tmp/gzh-stage
  sh /tmp/gzh-stage/gongzhonghao/orchestrator/deploy/setup_ecs.sh"
