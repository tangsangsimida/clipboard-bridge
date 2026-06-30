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
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
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
            print(f"警告: 环境变量 {key}={raw} 小于最小值 {min_val}，使用默认值 {default}", file=sys.stderr)
            return default
        return val
    except ValueError:
        print(f"警告: 环境变量 {key}={raw} 不是有效数字，使用默认值 {default}", file=sys.stderr)
        return default


POLL_MIN_INTERVAL = _env_float("CB_POLL_MIN", 0.3, min_val=0.05)
POLL_MAX_INTERVAL = _env_float("CB_POLL_MAX", 2.0, min_val=0.1)
POLL_STEP = _env_float("CB_POLL_STEP", 0.2, min_val=0.01)

if POLL_MIN_INTERVAL > POLL_MAX_INTERVAL:
    print(f"警告: CB_POLL_MIN ({POLL_MIN_INTERVAL}) > CB_POLL_MAX ({POLL_MAX_INTERVAL})，自动调整", file=sys.stderr)
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

def run(cmd: list[str], input_data: bytes | None = None, capture: bool = True, retries: int = 0) -> bytes:
    """Run a command, return stdout. Log errors at debug level. Retries on failure."""
    for attempt in range(retries + 1):
        try:
            r = subprocess.run(cmd, input=input_data, capture_output=capture, timeout=2)
            if r.returncode == 0:
                return r.stdout if capture else b""
            if attempt < retries:
                log.debug("cmd %s exited %d, retrying (%d/%d)", cmd[0], r.returncode, attempt + 1, retries)
                time.sleep(0.05)
                continue
            if r.stderr:
                log.debug("cmd %s exited %d: %s", cmd[0], r.returncode, r.stderr.decode(errors="replace").strip())
            return r.stdout if capture else b""
        except subprocess.TimeoutExpired:
            if attempt < retries:
                log.debug("cmd %s timed out, retrying (%d/%d)", cmd[0], attempt + 1, retries)
                time.sleep(0.05)
                continue
            log.debug("cmd %s timed out", cmd[0])
            return b""
        except FileNotFoundError:
            log.debug("cmd %s not found", cmd[0])
            return b""
        except OSError as e:
            log.debug("cmd %s error: %s", cmd[0], e)
            return b""
    return b""


def xclip_get_targets() -> list[str]:
    raw = run(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"])
    return raw.decode("utf-8", errors="replace").splitlines()


def xclip_get(mime: str) -> bytes:
    return run(["xclip", "-selection", "clipboard", "-t", mime, "-o"])


def xclip_set(data: bytes, mime: str) -> None:
    run(["xclip", "-selection", "clipboard", "-t", mime], input_data=data, capture=False, retries=2)


def xclip_targets_and_text() -> tuple[list[str], bytes]:
    """Read X11 targets and UTF8_STRING in one shell call (2 subprocess -> 1)."""
    combined = run(["sh", "-c", "xclip -selection clipboard -t TARGETS -o && echo '---SEP---' && xclip -selection clipboard -o"])
    parts = combined.split(b"---SEP---\n", 1)
    targets = parts[0].decode("utf-8", errors="replace").splitlines()
    content = parts[1] if len(parts) > 1 else b""
    return targets, content


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
    run(cmd, input_data=data, capture=False, retries=2)


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


# ─── Clipboard watcher abstraction ───────────────────────────────────────────

@dataclass
class ClipboardContent:
    """Unified clipboard content representation."""
    text: bytes = b""
    types: list[str] = field(default_factory=list)
    uris: str = ""
    image_data: bytes = b""
    image_mime: str = ""

    @property
    def hash(self) -> str:
        return f"{len(self.text)}:{hashlib.md5(self.text).hexdigest()}"

    @property
    def types_hash(self) -> str:
        return f"{len(self.types)}:{hashlib.md5(chr(10).join(self.types).encode()).hexdigest()}"


class ClipboardWatcher(ABC):
    """Abstract clipboard interface."""

    @abstractmethod
    def read(self) -> ClipboardContent: ...

    @abstractmethod
    def write_text(self, text: bytes) -> None: ...

    @abstractmethod
    def write_uri(self, uris: bytes) -> None: ...

    @abstractmethod
    def write_image(self, data: bytes, mime: str) -> None: ...


class X11ClipboardWatcher(ClipboardWatcher):
    """X11 clipboard via xclip."""

    def read(self) -> ClipboardContent:
        targets, text = xclip_targets_and_text()
        c = ClipboardContent(text=text, types=targets)
        if URI_LIST_MIME in targets:
            c.uris = xclip_get(URI_LIST_MIME).decode("utf-8", errors="replace")
        elif img := detect_x11_image(targets):
            c.image_data = xclip_get(img)
            c.image_mime = img
        elif uris := detect_x11_files(text.decode("utf-8", errors="replace")):
            c.uris = uris
        return c

    def write_text(self, text: bytes) -> None:
        xclip_set(text, "UTF8_STRING")

    def write_uri(self, uris: bytes) -> None:
        xclip_set(uris, URI_LIST_MIME)

    def write_image(self, data: bytes, mime: str) -> None:
        xclip_set(data, mime)


class WaylandClipboardWatcher(ClipboardWatcher):
    """Wayland clipboard via wl-copy/wl-paste."""

    def read(self) -> ClipboardContent:
        types, text = wl_paste_types_and_text()
        c = ClipboardContent(text=text, types=types)
        img = next((t for t in types if t.startswith("image/")), None)
        if img:
            c.image_data = wl_paste(img)
            c.image_mime = img
        elif GNOME_FILE_MIME in types:
            raw = wl_paste(GNOME_FILE_MIME).decode("utf-8", errors="replace")
            lines = raw.strip().split("\n")
            if lines and lines[0].strip() in ("copy", "cut"):
                c.uris = "\n".join(lines[1:])
        elif URI_LIST_MIME in types:
            c.uris = wl_paste(URI_LIST_MIME).decode("utf-8", errors="replace")
        return c

    def write_text(self, text: bytes) -> None:
        wl_copy(text)

    def write_uri(self, uris: bytes) -> None:
        wl_copy(uris, GNOME_FILE_MIME)

    def write_image(self, data: bytes, mime: str) -> None:
        wl_copy(data, mime)


# ─── Sync functions ──────────────────────────────────────────────────────────

class SyncDirection(Enum):
    NONE = ""
    X11_TO_WL = "x2w"
    WL_TO_X11 = "w2x"


class ClipState:
    """Tracks clipboard state with hash-based change detection."""

    def __init__(self, x11: X11ClipboardWatcher, wl: WaylandClipboardWatcher):
        self.x11 = x11
        self.wl = wl
        self.x11_hash = ""
        self.wl_hash = ""
        self.wl_types_hash = ""
        self.x11_img_hash = ""
        self.wl_img_hash = ""
        self.lock = SyncDirection.NONE

    def init(self):
        """Read initial clipboard state."""
        x11_c = self.x11.read()
        wl_c = self.wl.read()
        self.x11_hash = x11_c.hash
        self.wl_hash = wl_c.hash
        self.wl_types_hash = wl_c.types_hash
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
        self.wl.write_uri(wl_content.encode())
        self.wl_hash = h
        # Update source hash (X11 side) to prevent feedback loop
        x11_content = clean.replace("\n", "\r\n") + "\r\n"
        self.x11_hash = fast_hash(x11_content.encode())
        self.lock = SyncDirection.NONE

    def sync_text_to_wayland(self, text: str):
        """Sync plain text to Wayland."""
        h = fast_hash(text.encode())
        if h == self.wl_hash:
            return
        log.debug("Text→WL: %s", text[:50])
        self.lock = SyncDirection.X11_TO_WL
        self.wl.write_text(text.encode())
        self.wl_hash = h
        self.x11_hash = h
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
        wl_copy(data, mime)
        self.x11_img_hash = h
        self.wl_img_hash = h
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
        self.x11.write_uri(x11_content.encode())
        self.x11_hash = h
        # Update source hash (Wayland side) to prevent feedback loop
        self.wl_hash = fast_hash(uris.encode())
        self.lock = SyncDirection.NONE

    def sync_text_to_x11(self, text: str):
        """Sync plain text to X11."""
        h = fast_hash(text.encode())
        if h == self.x11_hash:
            return
        log.debug("Text→X11: %s", text[:50])
        self.lock = SyncDirection.WL_TO_X11
        self.x11.write_text(text.encode())
        self.x11_hash = h
        self.wl_hash = h
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
        xclip_set(data, mime)
        self.wl_img_hash = h
        self.x11_img_hash = h
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
    """If all lines are file paths, return file:// URI list. Otherwise None.

    Heuristic: lines must contain '/' and look like real paths (not random text).
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        return None
    # Require at least one '/' per line to avoid mis-detecting plain words
    if not all("/" in line for line in lines):
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
    c = state.x11.read()
    if c.hash == state.x11_hash:
        return False
    if c.uris:
        state.sync_uri_to_wayland(c.uris)
    elif c.image_data:
        state.sync_image_to_wayland(c.image_mime)
    elif uris := detect_x11_files(c.text.decode("utf-8", errors="replace")):
        state.sync_uri_to_wayland(uris)
    else:
        state.sync_text_to_wayland(c.text.decode("utf-8", errors="replace"))
    state.x11_hash = c.hash
    return True


def detect_wayland(state: ClipState) -> bool:
    """Detect Wayland clipboard changes and sync to X11. Returns True if changed."""
    c = state.wl.read()
    if c.types_hash == state.wl_types_hash and c.hash == state.wl_hash:
        return False
    if c.image_data:
        img = next((t for t in c.types if t.startswith("image/")), "image/png")
        state.sync_image_to_x11(img)
    elif c.uris:
        state.sync_uri_to_x11(c.uris)
    else:
        state.sync_text_to_x11(c.text.decode("utf-8", errors="replace"))
    state.wl_hash = c.hash
    state.wl_types_hash = c.types_hash
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

    state = ClipState(X11ClipboardWatcher(), WaylandClipboardWatcher())
    state.init()
    main_loop(state, shutdown_event)


if __name__ == "__main__":
    main()
