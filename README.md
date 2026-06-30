# Clipboard Bridge

X11 ↔ Wayland 剪贴板双向同步工具，支持**文本**、**文件**和**图片**。

## 背景

在 Wayland 桌面环境（如 niri）下，X11 应用（终端、IDE 等）和原生 Wayland 应用（微信、Nautilus 等）的剪贴板不互通。本工具通过轮询检测 + 双向同步解决此问题。

## 功能

- **文本同步**：纯文本双向同步
- **文件同步**：自动检测文件路径，转换为 `file://` URI，使用 `x-special/gnome-copied-files` 格式同步
- **图片同步**：`image/png` 等二进制数据双向同步
- **自适应轮询**：无变化时自动降低检测频率，减少 CPU 占用
- **防反馈循环**：基于 hash 的状态管理，避免 X11/Wayland 格式差异导致的无限循环
- **日志支持**：可选 debug 日志输出到文件

## 依赖

- Python 3.10+
- `xclip` — X11 剪贴板操作
- `wl-clipboard` — Wayland 剪贴板操作（`wl-copy` / `wl-paste`）
- `systemd` — 用户服务管理

```bash
# Arch Linux
sudo pacman -S python xclip wl-clipboard

# Debian/Ubuntu
sudo apt install python3 xclip wl-clipboard
```

## 安装

```bash
git clone https://github.com/tangsangsimida/clipboard-bridge.git
cd clipboard-bridge
chmod +x install.sh
./install.sh
```

安装脚本会：
1. 复制 `clipboard-sync.py` 到 `~/.local/bin/`
2. 复制 `clipboard-bridge.service` 到 `~/.config/systemd/user/`
3. 启用并启动 systemd 用户服务

## 卸载

```bash
chmod +x uninstall.sh
./uninstall.sh
```

## 使用

安装后服务自动运行，无需额外操作。

### 命令行参数

```bash
# 启用 debug 日志（输出到 stderr）
clipboard-sync.py -v

# 日志写入文件
clipboard-sync.py -l ~/.local/state/clipboard-bridge.log

# 同时启用 debug 和文件日志
clipboard-sync.py -v -l ~/.local/state/clipboard-bridge.log
```

### 验证

```bash
# 检查服务状态
systemctl --user status clipboard-bridge

# 测试文本同步
echo "test" | xclip -selection clipboard
sleep 1
wl-paste --no-newline  # 应输出 "test"

# 测试文件同步
echo "/home/user/.bashrc" | xclip -selection clipboard
sleep 1
wl-paste -t x-special/gnome-copied-files  # 应输出文件 URI
```

## 自启配置

### niri

在 `~/.config/niri/config.kdl` 中添加：

```kdl
spawn-at-startup "systemctl" "--user" "start" "clipboard-bridge"
```

### 其他 Wayland 合成器

在合成器的自启配置中添加：

```bash
systemctl --user start clipboard-bridge
```

## 工作原理

```
┌─────────────┐                    ┌─────────────┐
│  X11 App    │                    │ Wayland App │
│ (终端/IDE)  │                    │  (微信等)   │
└──────┬──────┘                    └──────┬──────┘
       │                                  │
       ▼                                  ▼
┌──────────────┐  clipboard-sync.py  ┌──────────────┐
│ xclip        │ ◄─────────────────► │ wl-copy      │
│ (X11 剪贴板) │   自适应轮询 + hash │ (WL 剪贴板)  │
└──────────────┘                     └──────────────┘
```

脚本以自适应间隔轮询两端剪贴板，检测变化后根据 MIME 类型选择同步策略：

| MIME 类型 | X11 → Wayland | Wayland → X11 |
|-----------|---------------|---------------|
| 文本 | `text/plain` → `text/plain` | `text/plain` → `text/plain` |
| 文件 | `text/uri-list` → `x-special/gnome-copied-files` | `x-special/gnome-copied-files` → `text/uri-list` |
| 图片 | `image/png` → `image/png` | `image/png` → `image/png` |

### 自适应轮询

- 有变化时：0.3s 间隔（快速响应）
- 无变化时：每次 +0.2s，最大 2.0s（降低 CPU 占用）

## 许可证

MIT
