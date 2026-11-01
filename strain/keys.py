"""Key presses on the controlling terminal, read on a background thread so the dashboard reacts during builds."""

import os
import re
import sys
import threading
import time
from collections.abc import Callable, Mapping
from typing import TextIO

from .console import is_terminal

KeyHandler = Callable[[str], None]

# How long one read waits for a key, which bounds how long closing the reader takes.
POLL_SECONDS = 0.1
READ_SIZE = 64
ESCAPE_SEQUENCES: Mapping[str, str] = {
    "\033[A": "up",
    "\033OA": "up",
    "\033[B": "down",
    "\033OB": "down",
    "\033[5~": "pageup",
    "\033[6~": "pagedown",
}
ALIASES: Mapping[str, str] = {"j": "down", "k": "up", "\t": "tab"}
# A CSI sequence (parameters, then a final byte), an SS3 sequence, or a single character.
KEY_TOKEN = re.compile(r"\033\[[0-9;?]*[@-~]|\033O.|.", re.DOTALL)
# Windows reports special keys as one of these prefixes followed by a scan code.
WINDOWS_PREFIXES = frozenset({"\x00", "\xe0"})
WINDOWS_SCAN_CODES: Mapping[str, str] = {"H": "\033[A", "P": "\033[B", "I": "\033[5~", "Q": "\033[6~"}


def decode_key(token: str) -> str | None:
    if token.startswith("\033"):
        return ESCAPE_SEQUENCES.get(token)
    return ALIASES.get(token, token)


def decode_keys(text: str) -> list[str]:
    keys = (decode_key(match.group()) for match in KEY_TOKEN.finditer(text))
    return [key for key in keys if key is not None]


class KeyReader:
    """Calls `on_key` from a background thread for every key read, until closed."""

    def __init__(self, read: Callable[[], str], restore: Callable[[], None], on_key: KeyHandler) -> None:
        self._read = read
        self._restore = restore
        self._on_key = on_key
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="strain-keys", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stopped.is_set():
            for key in decode_keys(self._read()):
                self._on_key(key)

    def close(self) -> None:
        if not self._stopped.is_set():
            self._stopped.set()
            self._thread.join()
            self._restore()


def _windows_reader(on_key: KeyHandler) -> KeyReader | None:
    if sys.platform != "win32":
        return None
    import msvcrt

    def read() -> str:
        deadline = time.monotonic() + POLL_SECONDS
        while not msvcrt.kbhit():
            if time.monotonic() >= deadline:
                return ""
            time.sleep(POLL_SECONDS / 10)
        key = msvcrt.getwch()
        return WINDOWS_SCAN_CODES.get(msvcrt.getwch(), "") if key in WINDOWS_PREFIXES else key

    return KeyReader(read, lambda: None, on_key)


def _posix_reader(stream: TextIO, on_key: KeyHandler) -> KeyReader | None:
    if sys.platform == "win32":
        return None
    import select
    import termios
    import tty

    descriptor = stream.fileno()
    try:
        saved = termios.tcgetattr(descriptor)
        # cbreak keeps signals, so Ctrl-C still interrupts the run and restores the sources.
        tty.setcbreak(descriptor)
    except termios.error:
        return None

    def read() -> str:
        ready, _, _ = select.select([descriptor], [], [], POLL_SECONDS)
        return os.read(descriptor, READ_SIZE).decode(errors="replace") if ready else ""

    return KeyReader(read, lambda: termios.tcsetattr(descriptor, termios.TCSADRAIN, saved), on_key)


def open_key_reader(stream: TextIO, on_key: KeyHandler) -> KeyReader | None:
    """A reader of the keys typed on `stream`, or None when `stream` is not an interactive terminal."""
    if not is_terminal(stream):
        return None
    return _windows_reader(on_key) if sys.platform == "win32" else _posix_reader(stream, on_key)


def stdin_key_reader(on_key: KeyHandler) -> KeyReader | None:
    return open_key_reader(sys.stdin, on_key)
