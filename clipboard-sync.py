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
import tempfile
import time
from argparse import ArgumentParser
from pathlib import Path

# ─── Configuration ───────────────────────────────────────────────────────────

POLL_MIN_INTERVAL = 0.3   # seconds, minimum polling interval
POLL_MAX_INTERVAL = 2.0   # seconds, maximum polling interval when idle
POLL_STEP = 0.2           # seconds, increment per idle cycle
IMG_MIME = "image/png"
GNOME_FILE_MIME = "x-special/gnome-copied-files"
URI_LIST_MIME = "text/uri-list"
HOME = Path.home()

# ─── Logging ─────────────────────────────────────────────────────────────────

log = logging.getLogger("clipboard-bridge")


def setup_logging(verbose: bool = False, log_file: str | None = None):
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
    """Run a command, return stdout. Silently ignore errors."""
    try:
        r = subprocess.run(cmd, input=input_data, capture_output=capture, timeout=2)
        return r.stdout if capture else b""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return b""


def xclip_get_targets() -> list[str]:
    raw = run(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"])
    return raw.decode("utf-8", errors="replace").splitlines()


def xclip_get(mime: str) -> bytes:
    return run(["xclip", "-selection", "clipboard", "-t", mime, "-o"])


def xclip_set(data: bytes, mime: str):
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


def wl_copy(data: bytes, mime: str | None = None):
    cmd = ["wl-copy"]
    if mime:
        cmd.extend(["--type", mime])
    run(cmd, input_data=data, capture=False)


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


# ─── Sync functions ──────────────────────────────────────────────────────────

class ClipState:
    """Tracks clipboard state with hash-based change detection."""

    def __init__(self):
        self.x11_hash = ""
        self.wl_hash = ""
        self.wl_types_hash = ""
        self.x11_img_hash = ""
        self.wl_img_hash = ""
        self.lock = ""  # "x2w" or "w2x" during sync
        self.tmp_x11_img = ""
        self.tmp_wl_img = ""

    def init(self):
        """Read initial clipboard state."""
        x11 = xclip_get("UTF8_STRING")
        wl = wl_paste(no_newline=True)
        wl_types = "\n".join(wl_paste_types())
        self.x11_hash = md5(x11)
        self.wl_hash = md5(wl)
        self.wl_types_hash = md5(wl_types.encode())
        log.debug("Init: x11=%s wl=%s types=%s", self.x11_hash[:8], self.wl_hash[:8], self.wl_types_hash[:8])

    def sync_uri_to_wayland(self, uris: str):
        """Sync file URIs to Wayland as x-special/gnome-copied-files."""
        clean = uris.replace("\r", "")
        wl_content = f"copy\n{clean}\n"
        h = md5(wl_content.encode())
        if h == self.wl_hash:
            return
        log.debug("URI→WL: %s", clean.strip().split("\n")[0])
        self.lock = "x2w"
        self.wl_hash = h
        # Update source hash (X11 side) to prevent feedback loop
        x11_content = clean.replace("\n", "\r\n") + "\r\n"
        self.x11_hash = md5(x11_content.encode())
        wl_copy(wl_content.encode(), GNOME_FILE_MIME)
        self.lock = ""

    def sync_text_to_wayland(self, text: str):
        """Sync plain text to Wayland."""
        h = md5(text.encode())
        if h == self.wl_hash:
            return
        log.debug("Text→WL: %s", text[:50])
        self.lock = "x2w"
        self.wl_hash = h
        self.x11_hash = h
        wl_copy(text.encode())
        self.lock = ""

    def sync_image_to_wayland(self, mime: str):
        """Sync image data from X11 to Wayland."""
        data = xclip_get(mime)
        if not data:
            return
        h = md5(data)
        if h == self.x11_img_hash:
            return
        log.debug("Image→WL: %s (%d bytes)", mime, len(data))
        self.lock = "x2w"
        self.x11_img_hash = h
        self.wl_img_hash = h
        wl_copy(data, mime)
        self.lock = ""

    def sync_uri_to_x11(self, uris: str):
        """Sync file URIs to X11 as text/uri-list."""
        clean = uris.replace("\r", "")
        x11_content = clean.replace("\n", "\r\n") + "\r\n"
        h = md5(x11_content.encode())
        if h == self.x11_hash:
            return
        log.debug("URI→X11: %s", clean.strip().split("\n")[0])
        self.lock = "w2x"
        self.x11_hash = h
        # Update source hash (Wayland side) to prevent feedback loop
        self.wl_hash = md5(uris.encode())
        xclip_set(x11_content.encode(), URI_LIST_MIME)
        self.lock = ""

    def sync_text_to_x11(self, text: str):
        """Sync plain text to X11."""
        h = md5(text.encode())
        if h == self.x11_hash:
            return
        log.debug("Text→X11: %s", text[:50])
        self.lock = "w2x"
        self.x11_hash = h
        self.wl_hash = h
        xclip_set(text.encode(), "UTF8_STRING")
        self.lock = ""

    def sync_image_to_x11(self, mime: str):
        """Sync image data from Wayland to X11."""
        data = wl_paste(mime)
        if not data:
            return
        h = md5(data)
        if h == self.wl_img_hash:
            return
        log.debug("Image→X11: %s (%d bytes)", mime, len(data))
        self.lock = "w2x"
        self.wl_img_hash = h
        self.x11_img_hash = h
        xclip_set(data, mime)
        self.lock = ""


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

def main_loop(state: ClipState):
    interval = POLL_MIN_INTERVAL

    while True:
        if state.lock:
            time.sleep(0.2)
            continue

        idle = True

        # ── X11 detection ──
        x11_raw = xclip_get("UTF8_STRING")
        x11_hash = md5(x11_raw)

        if x11_hash != state.x11_hash:
            targets = xclip_get_targets()
            x11_text = x11_raw.decode("utf-8", errors="replace")

            if URI_LIST_MIME in targets:
                uris = xclip_get(URI_LIST_MIME).decode("utf-8", errors="replace")
                if uris.strip():
                    state.sync_uri_to_wayland(uris)
                    state.x11_hash = x11_hash
                    idle = False
            elif (img_mime := detect_x11_image(targets)):
                state.sync_image_to_wayland(img_mime)
                state.x11_hash = x11_hash
                idle = False
            elif (file_uris := detect_x11_files(x11_text)):
                state.sync_uri_to_wayland(file_uris)
                state.x11_hash = x11_hash
                idle = False
            else:
                state.sync_text_to_wayland(x11_text)
                state.x11_hash = x11_hash
                idle = False

        # ── Wayland detection ──
        wl_types = wl_paste_types()
        wl_types_hash = md5("\n".join(wl_types).encode())
        wl_raw = wl_paste(no_newline=True)
        wl_hash = md5(wl_raw)

        if wl_types_hash != state.wl_types_hash or wl_hash != state.wl_hash:
            if IMG_MIME in wl_types:
                state.sync_image_to_x11(IMG_MIME)
            elif GNOME_FILE_MIME in wl_types:
                raw = wl_paste(GNOME_FILE_MIME).decode("utf-8", errors="replace")
                lines = raw.strip().split("\n")
                uris = "\n".join(lines[1:]) if len(lines) > 1 else ""
                if uris.strip():
                    state.sync_uri_to_x11(uris)
            elif URI_LIST_MIME in wl_types:
                uris = wl_paste(URI_LIST_MIME).decode("utf-8", errors="replace")
                if uris.strip():
                    state.sync_uri_to_x11(uris)
            else:
                text = wl_raw.decode("utf-8", errors="replace")
                state.sync_text_to_x11(text)

            state.wl_hash = wl_hash
            state.wl_types_hash = wl_types_hash
            idle = False

        # ── Adaptive polling ──
        if idle:
            interval = min(interval + POLL_STEP, POLL_MAX_INTERVAL)
        else:
            interval = POLL_MIN_INTERVAL
        time.sleep(interval)


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    parser = ArgumentParser(description="X11 ↔ Wayland Clipboard Bridge")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument("-l", "--log-file", help="Log to file (e.g. ~/.local/state/clipboard-bridge.log)")
    args = parser.parse_args()

    setup_logging(verbose=args.verbose, log_file=args.log_file)
    log.info("Clipboard bridge starting")

    # Graceful shutdown
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    # Cleanup temp files on exit
    import atexit
    for tmp in ["/tmp/clipboard-bridge-x11-img", "/tmp/clipboard-bridge-wl-img"]:
        atexit.register(lambda f=tmp: Path(f).unlink(missing_ok=True))

    state = ClipState()
    state.init()
    main_loop(state)


if __name__ == "__main__":
    main()
