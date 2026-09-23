#!/usr/bin/env bash
# bigcat 一键安装脚本（macOS）
#
# 服务端（主控）:
#   curl -fsSL https://raw.githubusercontent.com/zhanghaobo1987/bigcat/main/scripts/install-macos.sh | sudo bash -s -- server
#   # 指定端口: ... | sudo bash -s -- server --port 8080
#
# 被控端（agent，需先在主控注册拿到 token）:
#   curl -fsSL https://raw.githubusercontent.com/zhanghaobo1987/bigcat/main/scripts/install-macos.sh | sudo bash -s -- agent http://主控IP:25774 <token>
#
# 也支持 git clone 后本地运行: sudo bash scripts/install-macos.sh server
set -euo pipefail

REPO_URL="https://github.com/zhanghaobo1987/bigcat"
INSTALL_DIR="/usr/local/bigcat"
VENV="$INSTALL_DIR/venv"
DEFAULT_PORT=25774
PLIST_DIR="/Library/LaunchDaemons"

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

[ "$(uname)" = "Darwin" ] || die "此脚本仅适用于 macOS"
[ "$(id -u)" -eq 0 ] || die "请用 root 运行此脚本（加 sudo），以便注册系统级 launchd 服务"

ensure_sources() {
  local script_dir="$1"
  if [ -d "$script_dir/../server" ] && [ -f "$script_dir/../agent/agent.py" ]; then
    echo "$script_dir/.."
    return
  fi
  log "未检测到本地仓库源码，正在从 GitHub 下载..."
  command -v git >/dev/null || die "请先安装 git（xcode-select --install 或 brew install git）"
  local tmp
  tmp="$(mktemp -d)"
  git clone --depth 1 "$REPO_URL" "$tmp/bigcat"
  echo "$tmp/bigcat"
}

install_python() {
  if command -v python3 >/dev/null; then
    return
  fi
  if command -v brew >/dev/null; then
    log "通过 Homebrew 安装 Python3..."
    brew install python
  else
    die "未找到 python3，请先安装：brew install python（或从 python.org 下载安装包）"
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

write_plist() {
  local label="$1"       # com.bigcat.server
  local prog_args="$2"    # plist ProgramArguments 片段
  local plist="$PLIST_DIR/$label.plist"
  if [ -f "$plist" ]; then
    launchctl bootout system "$plist" 2>/dev/null || true
  fi
  cat >"$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
$prog_args
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/var/log/bigcat-$label.log</string>
  <key>StandardErrorPath</key><string>/var/log/bigcat-$label.log</string>
</dict>
</plist>
EOF
  chmod 644 "$plist"
  launchctl bootstrap system "$plist"
  log "launchd 服务 $label 已加载（开机自启）"
}

install_server() {
  local src
  src="$(ensure_sources "$(cd "$(dirname "$0")" && pwd)")"
  log "安装服务端（端口 $PORT）..."
  install_python
  mkdir -p "$INSTALL_DIR/data"
  cp -r "$src/server" "$INSTALL_DIR/"
  setup_venv

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

  write_plist "com.bigcat.server" "\
    <string>$VENV/bin/python</string>
    <string>$INSTALL_DIR/server/app.py</string>
    <string>--port</string>
    <string>$PORT</string>
    <string>--db</string>
    <string>$INSTALL_DIR/data/bigcat.db</string>"
  log "服务端已启动: http://本机IP:$PORT"
}

install_agent() {
  [ -n "$SERVER_URL" ] || die "用法: bash install-macos.sh agent http://主控IP:25774 <token>"
  [ -n "$TOKEN" ] || die "用法: bash install-macos.sh agent http://主控IP:25774 <token>"
  local src
  src="$(ensure_sources "$(cd "$(dirname "$0")" && pwd)")"
  log "安装 agent，上报目标 $SERVER_URL ..."
  install_python
  mkdir -p "$INSTALL_DIR"
  cp "$src/agent/agent.py" "$INSTALL_DIR/"
  setup_venv

  write_plist "com.bigcat.agent" "\
    <string>$VENV/bin/python</string>
    <string>$INSTALL_DIR/agent.py</string>
    <string>--server</string>
    <string>$SERVER_URL</string>
    <string>--token</string>
    <string>$TOKEN</string>
    <string>--interval</string>
    <string>2</string>"
  log "agent 已启动，正在向 $SERVER_URL 上报"
}

case "$MODE" in
  server) install_server ;;
  agent)  install_agent ;;
  *)
    echo "用法:" >&2
    echo "  sudo bash install-macos.sh server [--port 端口]              # 安装主控端（默认 25774）" >&2
    echo "  sudo bash install-macos.sh agent http://主控IP:25774 <token>  # 安装被控端" >&2
    echo "" >&2
    echo "环境变量: BIGCAT_ADMIN_PASSWORD=xxx  可在安装主控时预设管理密码" >&2
    exit 1 ;;
esac
