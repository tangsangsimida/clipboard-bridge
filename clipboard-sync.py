#!/usr/bin/env python3
"""
X11 ↔ Wayland Clipboard Bridge
Bidirectional sync for text, files, and images between X11 and Wayland clipboards.
"""

import hashlib
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from argparse import ArgumentParser
from enum import Enum
from pathlib import Path

# ─── Configuration (override via environment variables) ──────────────────────

IMG_MIME = "image/png"
GNOME_FILE_MIME = "x-special/gnome-copied-files"
URI_LIST_MIME = "text/uri-list"
HOME = Path.home()
XDG_STATE_HOME = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local" / "state"))


def _env_float(key: str, default: float, min_val: float = 0.0) -> float:
    """Read a float from environment variable with validation."""
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        val = float(raw)
        if val < min_val:
            log.warning("环境变量 %s=%s 小于最小值 %s，使用默认值 %s", key, raw, min_val, default)
            return default
        return val
    except ValueError:
        log.warning("环境变量 %s=%s 不是有效数字，使用默认值 %s", key, raw, default)
        return default


POLL_MIN_INTERVAL = _env_float("CB_POLL_MIN", 0.3, min_val=0.05)
POLL_MAX_INTERVAL = _env_float("CB_POLL_MAX", 2.0, min_val=0.1)
POLL_STEP = _env_float("CB_POLL_STEP", 0.2, min_val=0.01)

if POLL_MIN_INTERVAL > POLL_MAX_INTERVAL:
    log.warning("CB_POLL_MIN (%s) > CB_POLL_MAX (%s)，自动调整", POLL_MIN_INTERVAL, POLL_MAX_INTERVAL)
    POLL_MIN_INTERVAL, POLL_MAX_INTERVAL = POLL_MAX_INTERVAL, POLL_MIN_INTERVAL

# ─── Logging ─────────────────────────────────────────────────────────────────

log = logging.getLogger("clipboard-bridge")


def setup_logging(verbose: bool = False, log_file: str | None = None) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    fmt = "%(asctime)s %(levelname)-5s %(message)s"
    datefmt = "%H:%M:%S"

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)


# ─── Clipboard helpers ───────────────────────────────────────────────────────

def run(cmd: list[str], input_data: bytes | None = None, capture: bool = True) -> bytes:
    """Run a command, return stdout. Log errors at debug level."""
    try:
        r = subprocess.run(cmd, input=input_data, capture_output=capture, timeout=2)
        if r.returncode != 0 and r.stderr:
            log.debug("cmd %s exited %d: %s", cmd[0], r.returncode, r.stderr.decode(errors="replace").strip())
        return r.stdout if capture else b""
    except subprocess.TimeoutExpired:
        log.debug("cmd %s timed out", cmd[0])
        return b""
    except FileNotFoundError:
        log.debug("cmd %s not found", cmd[0])
        return b""
    except OSError as e:
        log.debug("cmd %s error: %s", cmd[0], e)
        return b""


def xclip_get_targets() -> list[str]:
    raw = run(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"])
    return raw.decode("utf-8", errors="replace").splitlines()


def xclip_get(mime: str) -> bytes:
    return run(["xclip", "-selection", "clipboard", "-t", mime, "-o"])


def xclip_set(data: bytes, mime: str) -> None:
    run(["xclip", "-selection", "clipboard", "-t", mime], input_data=data, capture=False)


def wl_paste_types() -> list[str]:
    raw = run(["wl-paste", "--list-types"])
    return raw.decode("utf-8", errors="replace").splitlines()


def wl_paste(mime: str | None = None, no_newline: bool = False) -> bytes:
    cmd = ["wl-paste"]
    if no_newline:
        cmd.append("--no-newline")
    if mime:
        cmd.extend(["-t", mime])
    return run(cmd)


def wl_copy(data: bytes, mime: str | None = None) -> None:
    cmd = ["wl-copy"]
    if mime:
        cmd.extend(["--type", mime])
    run(cmd, input_data=data, capture=False)


def wl_paste_types_and_text() -> tuple[list[str], bytes]:
    """Read Wayland types and text content in one shell call (2 subprocess → 1)."""
    combined = run(["sh", "-c", r"wl-paste --list-types && printf '\0' && wl-paste --no-newline"])
    parts = combined.split(b"\x00", 1)
    types = parts[0].decode("utf-8", errors="replace").splitlines()
    content = parts[1] if len(parts) > 1 else b""
    return types, content


def fast_hash(data: bytes) -> str:
    """Fast hash: length prefix + md5. Length difference alone can short-circuit comparison."""
    return f"{len(data)}:{hashlib.md5(data).hexdigest()}"


# ─── Sync functions ──────────────────────────────────────────────────────────

class SyncDirection(Enum):
    NONE = ""
    X11_TO_WL = "x2w"
    WL_TO_X11 = "w2x"


class ClipState:
    """Tracks clipboard state with hash-based change detection."""

    def __init__(self):
        self.x11_hash = ""
        self.wl_hash = ""
        self.wl_types_hash = ""
        self.x11_img_hash = ""
        self.wl_img_hash = ""
        self.lock = SyncDirection.NONE

    def init(self):
        """Read initial clipboard state."""
        x11 = xclip_get("UTF8_STRING")
        wl = wl_paste(no_newline=True)
        wl_types = "\n".join(wl_paste_types())
        self.x11_hash = fast_hash(x11)
        self.wl_hash = fast_hash(wl)
        self.wl_types_hash = fast_hash(wl_types.encode())
        log.debug("Init: x11=%s wl=%s types=%s", self.x11_hash[:8], self.wl_hash[:8], self.wl_types_hash[:8])

    def sync_uri_to_wayland(self, uris: str):
        """Sync file URIs to Wayland as x-special/gnome-copied-files."""
        clean = uris.replace("\r", "")
        wl_content = f"copy\n{clean}\n"
        h = fast_hash(wl_content.encode())
        if h == self.wl_hash:
            return
        log.debug("URI→WL: %s", clean.strip().split("\n")[0])
        self.lock = SyncDirection.X11_TO_WL
        self.wl_hash = h
        # Update source hash (X11 side) to prevent feedback loop
        x11_content = clean.replace("\n", "\r\n") + "\r\n"
        self.x11_hash = fast_hash(x11_content.encode())
        wl_copy(wl_content.encode(), GNOME_FILE_MIME)
        self.lock = SyncDirection.NONE

    def sync_text_to_wayland(self, text: str):
        """Sync plain text to Wayland."""
        h = fast_hash(text.encode())
        if h == self.wl_hash:
            return
        log.debug("Text→WL: %s", text[:50])
        self.lock = SyncDirection.X11_TO_WL
        self.wl_hash = h
        self.x11_hash = h
        wl_copy(text.encode())
        self.lock = SyncDirection.NONE

    def sync_image_to_wayland(self, mime: str):
        """Sync image data from X11 to Wayland."""
        data = xclip_get(mime)
        if not data:
            return
        h = fast_hash(data)
        if h == self.x11_img_hash:
            return
        log.debug("Image→WL: %s (%d bytes)", mime, len(data))
        self.lock = SyncDirection.X11_TO_WL
        self.x11_img_hash = h
        self.wl_img_hash = h
        wl_copy(data, mime)
        self.lock = SyncDirection.NONE

    def sync_uri_to_x11(self, uris: str):
        """Sync file URIs to X11 as text/uri-list."""
        clean = uris.replace("\r", "")
        x11_content = clean.replace("\n", "\r\n") + "\r\n"
        h = fast_hash(x11_content.encode())
        if h == self.x11_hash:
            return
        log.debug("URI→X11: %s", clean.strip().split("\n")[0])
        self.lock = SyncDirection.WL_TO_X11
        self.x11_hash = h
        # Update source hash (Wayland side) to prevent feedback loop
        self.wl_hash = fast_hash(uris.encode())
        xclip_set(x11_content.encode(), URI_LIST_MIME)
        self.lock = SyncDirection.NONE

    def sync_text_to_x11(self, text: str):
        """Sync plain text to X11."""
        h = fast_hash(text.encode())
        if h == self.x11_hash:
            return
        log.debug("Text→X11: %s", text[:50])
        self.lock = SyncDirection.WL_TO_X11
        self.x11_hash = h
        self.wl_hash = h
        xclip_set(text.encode(), "UTF8_STRING")
        self.lock = SyncDirection.NONE

    def sync_image_to_x11(self, mime: str):
        """Sync image data from Wayland to X11."""
        data = wl_paste(mime)
        if not data:
            return
        h = fast_hash(data)
        if h == self.wl_img_hash:
            return
        log.debug("Image→X11: %s (%d bytes)", mime, len(data))
        self.lock = SyncDirection.WL_TO_X11
        self.wl_img_hash = h
        self.x11_img_hash = h
        xclip_set(data, mime)
        self.lock = SyncDirection.NONE


def resolve_file_path(path: str) -> str | None:
    """Resolve a path to an absolute file:// URI. Returns None if not a file."""
    p = Path(path)
    if p.exists():
        return p.resolve().as_uri()
    p = HOME / path
    if p.exists():
        return p.resolve().as_uri()
    return None


def detect_x11_files(text: str) -> str | None:
    """If all lines in text are file paths, return file:// URI list. Otherwise None."""
    lines = [l for l in text.split("\n") if l.strip()]
    if not lines:
        return None
    uris = []
    for line in lines:
        uri = resolve_file_path(line)
        if uri is None:
            return None
        uris.append(uri)
    return "\n".join(uris)


def detect_x11_image(targets: list[str]) -> str | None:
    """Return the first image MIME type found in targets, or None."""
    for t in targets:
        if t.startswith("image/"):
            return t
    return None


# ─── Main loop ───────────────────────────────────────────────────────────────

def detect_x11(state: ClipState) -> bool:
    """Detect X11 clipboard changes and sync to Wayland. Returns True if changed."""
    x11_raw = xclip_get("UTF8_STRING")
    x11_hash = fast_hash(x11_raw)

    if x11_hash == state.x11_hash:
        return False

    targets = xclip_get_targets()
    x11_text = x11_raw.decode("utf-8", errors="replace")

    if URI_LIST_MIME in targets:
        uris = xclip_get(URI_LIST_MIME).decode("utf-8", errors="replace")
        if uris.strip():
            state.sync_uri_to_wayland(uris)
    elif img_mime := detect_x11_image(targets):
        state.sync_image_to_wayland(img_mime)
    elif file_uris := detect_x11_files(x11_text):
        state.sync_uri_to_wayland(file_uris)
    else:
        state.sync_text_to_wayland(x11_text)

    state.x11_hash = x11_hash
    return True


def detect_wayland(state: ClipState) -> bool:
    """Detect Wayland clipboard changes and sync to X11. Returns True if changed."""
    wl_types, wl_raw = wl_paste_types_and_text()
    wl_types_hash = fast_hash("\n".join(wl_types).encode())
    wl_hash = fast_hash(wl_raw)

    if wl_types_hash == state.wl_types_hash and wl_hash == state.wl_hash:
        return False

    if IMG_MIME in wl_types:
        state.sync_image_to_x11(IMG_MIME)
    elif GNOME_FILE_MIME in wl_types:
        raw = wl_paste(GNOME_FILE_MIME).decode("utf-8", errors="replace")
        lines = raw.strip().split("\n")
        if lines and lines[0].strip() in ("copy", "cut"):
            uris = "\n".join(lines[1:])
            if uris.strip():
                state.sync_uri_to_x11(uris)
        else:
            log.debug("Invalid GNOME format: %s", raw[:50])
    elif URI_LIST_MIME in wl_types:
        uris = wl_paste(URI_LIST_MIME).decode("utf-8", errors="replace")
        if uris.strip():
            state.sync_uri_to_x11(uris)
    else:
        state.sync_text_to_x11(wl_raw.decode("utf-8", errors="replace"))

    state.wl_hash = wl_hash
    state.wl_types_hash = wl_types_hash
    return True


def main_loop(state: ClipState, shutdown_event: threading.Event | None = None) -> None:
    interval = POLL_MIN_INTERVAL

    while not (shutdown_event and shutdown_event.is_set()):
        if state.lock != SyncDirection.NONE:
            time.sleep(0.2)
            continue

        x11_changed = detect_x11(state)
        wl_changed = detect_wayland(state)

        if x11_changed or wl_changed:
            interval = POLL_MIN_INTERVAL
        else:
            interval = min(interval + POLL_STEP, POLL_MAX_INTERVAL)

        # Use shorter sleep so we can respond to shutdown quickly
        if shutdown_event:
            shutdown_event.wait(timeout=interval)
        else:
            time.sleep(interval)


# ─── Entry point ─────────────────────────────────────────────────────────────

def check_dependencies() -> None:
    """Check that required external tools are installed."""
    missing = []
    for cmd in ("xclip", "wl-copy", "wl-paste"):
        if subprocess.run(["which", cmd], capture_output=True).returncode != 0:
            missing.append(cmd)
    if missing:
        print(f"错误: 缺少依赖: {', '.join(missing)}", file=sys.stderr)
        print("请安装: sudo pacman -S xclip wl-clipboard  (Arch)", file=sys.stderr)
        print("        sudo apt install xclip wl-clipboard  (Debian/Ubuntu)", file=sys.stderr)
        sys.exit(1)


def main():
    parser = ArgumentParser(description="X11 ↔ Wayland Clipboard Bridge")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument("-l", "--log-file", nargs="?", const=str(XDG_STATE_HOME / "clipboard-bridge.log"), help="Log to file (default: ~/.local/state/clipboard-bridge.log when flag is given without value)")
    args = parser.parse_args()

    check_dependencies()
    setup_logging(verbose=args.verbose, log_file=args.log_file)
    log.info("Clipboard bridge starting")

    # Graceful shutdown: set flag and let main_loop exit after current sync
    shutdown_event = threading.Event()
    def _shutdown(*_):
        log.info("Shutting down...")
        shutdown_event.set()
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    state = ClipState()
    state.init()
    main_loop(state, shutdown_event)


if __name__ == "__main__":
    main()
