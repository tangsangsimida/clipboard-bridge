#!/bin/bash
# Clipboard Bridge 卸载脚本

set -e

BIN_DIR="$HOME/.local/bin"
SERVICE_DIR="$HOME/.config/systemd/user"

echo "Uninstalling Clipboard Bridge..."

# 停止并禁用服务
systemctl --user stop clipboard-bridge 2>/dev/null || true
systemctl --user disable clipboard-bridge 2>/dev/null || true

# 删除文件
rm -f "$BIN_DIR/clipboard-sync.sh"
rm -f "$SERVICE_DIR/clipboard-bridge.service"

# 重载 systemd
systemctl --user daemon-reload

echo "Uninstalled successfully!"
