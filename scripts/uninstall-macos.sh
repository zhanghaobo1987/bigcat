#!/usr/bin/env bash
# BigCat 卸载脚本（macOS）
#
#   sudo bash uninstall-macos.sh server   # 只卸载主控端
#   sudo bash uninstall-macos.sh agent    # 只卸载被控端
#   sudo bash uninstall-macos.sh all     # 全部卸载
#   sudo bash uninstall-macos.sh all --purge  # 全部卸载并删除数据
set -euo pipefail

MODE="${1:-}"
PURGE="no"
[ "${2:-}" = "--purge" ] && PURGE="yes"

INSTALL_DIR="/usr/local/bigcat"
PLIST_DIR="/Library/LaunchDaemons"

log() { echo "[BigCat] $*"; }
die() { echo "[BigCat] 错误: $*" >&2; exit 1; }

[ "$(uname)" = "Darwin" ] || die "此脚本仅适用于 macOS"
[ "$(id -u)" -eq 0 ] || die "请用 root 运行此脚本（加 sudo）"

remove_service() {
  local label="$1"
  local plist="$PLIST_DIR/$label.plist"
  if [ -f "$plist" ]; then
    log "卸载 launchd 服务 $label ..."
    launchctl bootout system "$plist" 2>/dev/null || true
    rm -f "$plist"
  fi
  rm -f "/var/log/bigcat-$label.log"
}

case "$MODE" in
  server)
    remove_service "com.bigcat.server"
    log "服务端已卸载" ;;
  agent)
    remove_service "com.bigcat.agent"
    log "agent 已卸载" ;;
  all)
    remove_service "com.bigcat.server"
    remove_service "com.bigcat.agent"
    if [ -d "$INSTALL_DIR" ]; then
      if [ "$PURGE" = "yes" ]; then
        log "删除 $INSTALL_DIR（含监控数据）..."
        rm -rf "$INSTALL_DIR"
      else
        log "保留监控数据 $INSTALL_DIR/data，如需彻底删除请加 --purge"
        rm -rf "$INSTALL_DIR/server" "$INSTALL_DIR/venv" "$INSTALL_DIR/agent.py" 2>/dev/null || true
      fi
    fi
    log "卸载完成" ;;
  *)
    echo "用法: sudo bash uninstall-macos.sh server|agent|all [--purge]" >&2
    exit 1 ;;
esac
