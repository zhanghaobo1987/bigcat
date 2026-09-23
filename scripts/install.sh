#!/usr/bin/env bash
# bigcat 一键安装脚本（Debian / Ubuntu）
#
# 服务端（主控）:
#   curl -fsSL https://raw.githubusercontent.com/zhanghaobo1987/bigcat/main/scripts/install.sh | sudo bash -s -- server
#   # 指定端口: ... | sudo bash -s -- server --port 8080
#
# 被控端（agent，需先在主控注册拿到 token）:
#   curl -fsSL https://raw.githubusercontent.com/zhanghaobo1987/bigcat/main/scripts/install.sh | sudo bash -s -- agent http://主控IP:25774 <token>
#
# 也支持 git clone 后本地运行: sudo bash scripts/install.sh server
set -euo pipefail

REPO_URL="https://github.com/zhanghaobo1987/bigcat"
RAW_BASE="https://raw.githubusercontent.com/zhanghaobo1987/bigcat/main"
INSTALL_DIR="/opt/bigcat"
VENV="$INSTALL_DIR/venv"
DEFAULT_PORT=25774

MODE="${1:-}"
shift || true
PORT="$DEFAULT_PORT"
SERVER_URL=""
TOKEN=""

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:?--port 需要一个端口号}"; shift 2 ;;
    *) if [ -z "$SERVER_URL" ]; then SERVER_URL="$1"; else TOKEN="$1"; fi; shift ;;
  esac
done

log() { echo "[bigcat] $*"; }
die() { echo "[bigcat] 错误: $*" >&2; exit 1; }

need_root() {
  [ "$(id -u)" -eq 0 ] || die "请用 root 运行此脚本（加 sudo）"
}

# 脚本所在目录是否就是仓库（git clone 模式）还是 curl 管道模式
ensure_sources() {
  local script_dir="$1"
  if [ -d "$script_dir/../server" ] && [ -f "$script_dir/../agent/agent.py" ]; then
    echo "$script_dir/.."
    return
  fi
  log "未检测到本地仓库源码，正在从 GitHub 下载..."
  command -v git >/dev/null || { apt-get update -qq && apt-get install -y -qq git curl; }
  local tmp
  tmp="$(mktemp -d)"
  if git clone --depth 1 "$REPO_URL" "$tmp/bigcat" 2>/dev/null; then
    echo "$tmp/bigcat"
  else
    # git 不可用时的兜底：下载 tarball
    curl -fsSL "$REPO_URL/archive/refs/heads/main.tar.gz" -o "$tmp/bigcat.tgz"
    tar -xzf "$tmp/bigcat.tgz" -C "$tmp"
    echo "$tmp/bigcat-main"
  fi
}

install_python() {
  if ! command -v python3 >/dev/null; then
    log "安装 Python3..."
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv python3-pip
  fi
  command -v python3 >/dev/null || die "安装 python3 失败"
}

setup_venv() {
  if [ ! -x "$VENV/bin/python" ]; then
    log "创建虚拟环境 $VENV ..."
    python3 -m venv "$VENV"
  fi
  log "安装 Python 依赖..."
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q Flask flask-cors flask-sock psutil requests
}

open_firewall() {
  local port="$1"
  if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    log "放行防火墙端口 $port (ufw)"
    ufw allow "$port/tcp" >/dev/null || true
  fi
}

install_server() {
  need_root
  local src
  src="$(ensure_sources "$(cd "$(dirname "$0")" && pwd)")"
  log "安装服务端（端口 $PORT）..."
  install_python
  mkdir -p "$INSTALL_DIR/data"
  cp -r "$src/server" "$INSTALL_DIR/"
  cp -r "$src/agent" "$INSTALL_DIR/" 2>/dev/null || true
  setup_venv

  # 管理密码：环境变量优先，否则交互式询问
  local admin_pw="${BIGCAT_ADMIN_PASSWORD:-}"
  if [ -z "$admin_pw" ] && [ -t 0 ]; then
    read -rsp "设置管理密码（留空则跳过，可稍后设置）: " admin_pw; echo
  fi
  if [ -n "$admin_pw" ]; then
    (cd "$INSTALL_DIR/server" && "$VENV/bin/python" app.py --db "$INSTALL_DIR/data/bigcat.db" --set-admin "$admin_pw" >/dev/null)
    log "管理密码已设置"
  else
    log "未设置管理密码，稍后可用以下命令设置："
    log "  $VENV/bin/python $INSTALL_DIR/server/app.py --db $INSTALL_DIR/data/bigcat.db --set-admin \"你的强密码\""
  fi

  cat >/etc/systemd/system/bigcat.service <<EOF
[Unit]
Description=bigcat monitoring server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR/server
ExecStart=$VENV/bin/python app.py --port $PORT --db $INSTALL_DIR/data/bigcat.db
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now bigcat
  open_firewall "$PORT"
  local ip
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  log "服务端已启动: http://${ip:-本机IP}:$PORT"
  systemctl --no-pager --lines=3 status bigcat | head -8 || true
}

install_agent() {
  need_root
  [ -n "$SERVER_URL" ] || die "用法: bash install.sh agent http://主控IP:25774 <token>"
  [ -n "$TOKEN" ] || die "用法: bash install.sh agent http://主控IP:25774 <token>"
  local src
  src="$(ensure_sources "$(cd "$(dirname "$0")" && pwd)")"
  log "安装 agent，上报目标 $SERVER_URL ..."
  install_python
  mkdir -p "$INSTALL_DIR"
  cp "$src/agent/agent.py" "$INSTALL_DIR/"
  setup_venv

  cat >/etc/systemd/system/bigcat-agent.service <<EOF
[Unit]
Description=bigcat monitoring agent
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV/bin/python $INSTALL_DIR/agent.py --server $SERVER_URL --token $TOKEN --interval 2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now bigcat-agent
  log "agent 已启动，正在向 $SERVER_URL 上报"
}

case "$MODE" in
  server) install_server ;;
  agent)  install_agent ;;
  *)
    echo "用法:" >&2
    echo "  sudo bash install.sh server [--port 端口]            # 安装主控端（默认 25774）" >&2
    echo "  sudo bash install.sh agent http://主控IP:25774 <token>  # 安装被控端" >&2
    echo "" >&2
    echo "环境变量: BIGCAT_ADMIN_PASSWORD=xxx  可在安装主控时预设管理密码" >&2
    exit 1 ;;
esac
