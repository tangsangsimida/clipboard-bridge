#!/bin/bash
# Clipboard Bridge 安装脚本

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/.local/bin"
SERVICE_DIR="$HOME/.config/systemd/user"

echo "Installing Clipboard Bridge..."

# 创建目录
mkdir -p "$BIN_DIR" "$SERVICE_DIR"

# 复制脚本
cp "$SCRIPT_DIR/clipboard-sync.sh" "$BIN_DIR/clipboard-sync.sh"
chmod +x "$BIN_DIR/clipboard-sync.sh"

# 复制服务文件
cp "$SCRIPT_DIR/clipboard-bridge.service" "$SERVICE_DIR/clipboard-bridge.service"

# 启用并启动服务
systemctl --user daemon-reload
systemctl --user enable clipboard-bridge
systemctl --user start clipboard-bridge

echo "Installed successfully!"
echo "Service status: $(systemctl --user is-active clipboard-bridge)"
