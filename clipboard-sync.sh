#!/bin/bash
# X11 ↔ Wayland 剪贴板双向同步（支持文本、文件、图片）

TMP_X11_IMG="/tmp/clipboard-bridge-x11-img"
TMP_WL_IMG="/tmp/clipboard-bridge-wl-img"
LAST_X11_HASH=""
LAST_WL_HASH=""
LAST_WL_TYPES_HASH=""
LAST_X11_IMG_HASH=""
LAST_WL_IMG_HASH=""
LOCK=""

cleanup() { rm -f "$TMP_X11_IMG" "$TMP_WL_IMG"; }
trap cleanup EXIT

str_hash() { echo -n "$1" | md5sum | cut -d' ' -f1; }

# 同步文件 URI 到 Wayland（GNOME 格式）
sync_uri_to_wayland() {
    local uris="$1"
    [ -z "$uris" ] && return
    CLEAN_URIS=$(echo "$uris" | tr -d '\r')
    # 计算 Wayland 实际内容的 hash（GNOME 格式）
    WL_CONTENT=$(printf 'copy\n%s\n' "$CLEAN_URIS")
    local hash
    hash=$(str_hash "$WL_CONTENT")
    [ "$hash" = "$LAST_WL_HASH" ] && return
    LOCK="x2w"
    LAST_WL_HASH="$hash"
    # 更新源端 hash（X11 的 URI 格式，带 \r\n）
    X11_URI_CONTENT=$(printf '%s\r\n' "$CLEAN_URIS")
    LAST_X11_HASH=$(str_hash "$X11_URI_CONTENT")
    printf '%s' "$WL_CONTENT" | wl-copy --type x-special/gnome-copied-files 2>/dev/null
    LOCK=""
}

# 同步文本到 Wayland
sync_text_to_wayland() {
    local content="$1"
    [ -z "$content" ] && return
    local hash
    hash=$(str_hash "$content")
    [ "$hash" = "$LAST_WL_HASH" ] && return
    LOCK="x2w"
    LAST_WL_HASH="$hash"
    LAST_X11_HASH="$hash"
    echo -n "$content" | wl-copy 2>/dev/null
    LOCK=""
}

# 同步图片到 Wayland
sync_image_to_wayland() {
    local mime="$1"
    xclip -selection clipboard -t "$mime" -o > "$TMP_X11_IMG" 2>/dev/null
    [ ! -s "$TMP_X11_IMG" ] && return
    local hash
    hash=$(md5sum < "$TMP_X11_IMG" | cut -d' ' -f1)
    [ "$hash" = "$LAST_X11_IMG_HASH" ] && return
    LOCK="x2w"
    LAST_X11_IMG_HASH="$hash"
    cat "$TMP_X11_IMG" | wl-copy --type "$mime" 2>/dev/null
    LOCK=""
}

# 同步文件 URI 到 X11（text/uri-list 格式）
sync_uri_to_x11() {
    local uris="$1"
    [ -z "$uris" ] && return
    CLEAN_URIS=$(echo "$uris" | tr -d '\r')
    # 计算 X11 实际内容的 hash（带 \r\n 格式）
    X11_CONTENT=$(printf '%s\r\n' "$CLEAN_URIS")
    local hash
    hash=$(str_hash "$X11_CONTENT")
    [ "$hash" = "$LAST_X11_HASH" ] && return
    LOCK="w2x"
    LAST_X11_HASH="$hash"
    # 更新源端 hash，防止反馈循环
    LAST_WL_HASH=$(str_hash "$uris")
    printf '%s' "$X11_CONTENT" | xclip -selection clipboard -t text/uri-list 2>/dev/null
    LOCK=""
}

# 同步文本到 X11
sync_text_to_x11() {
    local content="$1"
    [ -z "$content" ] && return
    local hash
    hash=$(str_hash "$content")
    [ "$hash" = "$LAST_X11_HASH" ] && return
    LOCK="w2x"
    LAST_X11_HASH="$hash"
    LAST_WL_HASH="$hash"
    echo -n "$content" | xclip -selection clipboard 2>/dev/null
    LOCK=""
}

# 同步图片到 X11
sync_image_to_x11() {
    local mime="$1"
    wl-paste --no-newline -t "$mime" > "$TMP_WL_IMG" 2>/dev/null
    [ ! -s "$TMP_WL_IMG" ] && return
    local hash
    hash=$(md5sum < "$TMP_WL_IMG" | cut -d' ' -f1)
    [ "$hash" = "$LAST_WL_IMG_HASH" ] && return
    LOCK="w2x"
    LAST_WL_IMG_HASH="$hash"
    cat "$TMP_WL_IMG" | xclip -selection clipboard -t "$mime" 2>/dev/null
    LOCK=""
}

# 初始化
INIT_X11=$(xclip -selection clipboard -o 2>/dev/null)
INIT_WL=$(wl-paste --no-newline 2>/dev/null)
INIT_WL_TYPES=$(wl-paste --list-types 2>/dev/null)
LAST_X11_HASH=$(str_hash "$INIT_X11")
LAST_WL_HASH=$(str_hash "$INIT_WL")
LAST_WL_TYPES_HASH=$(str_hash "$INIT_WL_TYPES")

while true; do
    [ -n "$LOCK" ] && { sleep 0.2; continue; }

    # ========== X11 检测 ==========
    CURRENT_X11=$(xclip -selection clipboard -o 2>/dev/null)
    CURRENT_X11_HASH=$(str_hash "$CURRENT_X11")

    if [ "$CURRENT_X11_HASH" != "$LAST_X11_HASH" ]; then
        CURRENT_X11_TARGETS=$(xclip -selection clipboard -t TARGETS -o 2>/dev/null)

        if echo "$CURRENT_X11_TARGETS" | grep -q "text/uri-list"; then
            URIS=$(xclip -selection clipboard -t text/uri-list -o 2>/dev/null)
            if [ -n "$URIS" ]; then
                sync_uri_to_wayland "$URIS"
                LAST_X11_HASH="$CURRENT_X11_HASH"
            fi
        elif echo "$CURRENT_X11_TARGETS" | grep -q "image/png"; then
            sync_image_to_wayland "image/png"
            LAST_X11_HASH="$CURRENT_X11_HASH"
        else
            URI_LIST=""
            ALL_FILES=true
            while IFS= read -r line; do
                [ -z "$line" ] && continue
                RESOLVED=""
                if [ -e "$line" ]; then
                    RESOLVED=$(realpath "$line" 2>/dev/null)
                elif [ -e "$HOME/$line" ]; then
                    RESOLVED=$(realpath "$HOME/$line" 2>/dev/null)
                fi
                if [ -n "$RESOLVED" ] && [ -e "$RESOLVED" ]; then
                    URI_LIST="${URI_LIST}file://${RESOLVED}"$'\n'
                else
                    ALL_FILES=false
                    break
                fi
            done <<< "$CURRENT_X11"
            if [ "$ALL_FILES" = true ] && [ -n "$URI_LIST" ]; then
                sync_uri_to_wayland "$URI_LIST"
            else
                sync_text_to_wayland "$CURRENT_X11"
            fi
            LAST_X11_HASH="$CURRENT_X11_HASH"
        fi
    fi

    # ========== Wayland 检测 ==========
    CURRENT_WL_TYPES=$(wl-paste --list-types 2>/dev/null)
    CURRENT_WL_TYPES_HASH=$(str_hash "$CURRENT_WL_TYPES")
    CURRENT_WL=$(wl-paste --no-newline 2>/dev/null)
    CURRENT_WL_HASH=$(str_hash "$CURRENT_WL")

    if [ "$CURRENT_WL_TYPES_HASH" != "$LAST_WL_TYPES_HASH" ] || [ "$CURRENT_WL_HASH" != "$LAST_WL_HASH" ]; then
        if echo "$CURRENT_WL_TYPES" | grep -q "image/png"; then
            sync_image_to_x11 "image/png"
        elif echo "$CURRENT_WL_TYPES" | grep -q "x-special/gnome-copied-files"; then
            RAW=$(wl-paste --no-newline -t x-special/gnome-copied-files 2>/dev/null)
            URIS=$(echo "$RAW" | tail -n +2)
            if [ -n "$URIS" ]; then
                sync_uri_to_x11 "$URIS"
            fi
        elif echo "$CURRENT_WL_TYPES" | grep -q "text/uri-list"; then
            URIS=$(wl-paste --no-newline -t text/uri-list 2>/dev/null)
            if [ -n "$URIS" ]; then
                sync_uri_to_x11 "$URIS"
            fi
        else
            sync_text_to_x11 "$CURRENT_WL"
        fi
        LAST_WL_HASH="$CURRENT_WL_HASH"
        LAST_WL_TYPES_HASH="$CURRENT_WL_TYPES_HASH"
    fi

    sleep 0.3
done
