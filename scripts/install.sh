#!/usr/bin/env bash
# bigcat 一键安装脚本
#
# 服务端（主控）:
#   curl -fsSL https://raw.githubusercontent.com/<你>/bigcat/main/scripts/install.sh | bash -s -- server
#
# 被控端（agent，需先在主控注册拿到 token）:
#   curl -fsSL https://raw.githubusercontent.com/<你>/bigcat/main/scripts/install.sh | bash -s -- agent http://主控IP:25774 <token>
set -euo pipefail

MODE="${1:-}"
SERVER_URL="${2:-}"
TOKEN="${3:-}"

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "请用 root 运行此脚本" >&2
    exit 1
  fi
}

install_python_deps() {
  if ! command -v python3 >/dev/null; then
    apt-get update && apt-get install -y python3 python3-pip
  fi
  pip3 install --break-system-packages -q Flask flask-cors psutil requests || \
    pip3 install -q Flask flask-cors psutil requests
}

install_server() {
  need_root
  echo "[bigcat] 安装服务端..."
  install_python_deps
  mkdir -p /opt/bigcat
  # 假设脚本与仓库一起分发；curl 管道模式下请先 git clone
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  if [ -d "$SCRIPT_DIR/../server" ]; then
    cp -r "$SCRIPT_DIR/../server" /opt/bigcat/
  fi
  cat >/etc/systemd/system/bigcat.service <<'EOF'
[Unit]
Description=bigcat monitoring server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/bigcat/server
ExecStart=/usr/bin/python3 app.py --port 25774 --db /opt/bigcat/data/bigcat.db
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  mkdir -p /opt/bigcat/data
  systemctl daemon-reload
  systemctl enable --now bigcat
  echo "[bigcat] 服务端已启动: http://$(hostname -I | awk '{print $1}'):25774"
  echo "[bigcat] 别忘了设置管理密码:"
  echo "  curl -X POST http://127.0.0.1:25774/api/admin/setup -H 'Content-Type: application/json' -d '{\"password\":\"你的强密码\"}'"
}

install_agent() {
  need_root
  if [ -z "$SERVER_URL" ] || [ -z "$TOKEN" ]; then
    echo "用法: bash install.sh agent http://主控IP:25774 <token>" >&2
    exit 1
  fi
  echo "[bigcat] 安装 agent..."
  install_python_deps
  mkdir -p /opt/bigcat
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  if [ -f "$SCRIPT_DIR/../agent/agent.py" ]; then
    cp "$SCRIPT_DIR/../agent/agent.py" /opt/bigcat/
  else
    echo "未找到 agent.py，请先 git clone 完整仓库" >&2
    exit 1
  fi
  cat >/etc/systemd/system/bigcat-agent.service <<EOF
[Unit]
Description=bigcat monitoring agent
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/bigcat
ExecStart=/usr/bin/python3 /opt/bigcat/agent.py --server $SERVER_URL --token $TOKEN --interval 2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now bigcat-agent
  echo "[bigcat] agent 已启动，正在向 $SERVER_URL 上报"
}

case "$MODE" in
  server) install_server ;;
  agent)  install_agent ;;
  *)
    echo "用法:" >&2
    echo "  bash install.sh server                              # 安装主控端" >&2
    echo "  bash install.sh agent http://主控IP:25774 <token>    # 安装被控端" >&2
    exit 1 ;;
esac
