"""Runs `strain run --ui live` in a pseudo-terminal and saves what the screen shows as text and PNG frames.

    pip install -e '.[ui-snapshot]'
    python tools/ui_snapshot.py tests/fixtures/sample_project --size 120x36 --keys down,tab,q

Frames go to `ui-snapshots/` by default: `frame-NNN.txt` every `--interval` seconds, and
`key-NN-<key>.txt` after each key, each with a matching `.png` when Pillow is installed.
The keys are sent once the run has finished, which is when the title shows `finished`.
"""

import argparse
import os
import pty
import select
import signal
import struct
import sys
import termios
import time
from collections.abc import Sequence
from fcntl import ioctl
from pathlib import Path

import pyte

KEYS = {"up": "\033[A", "down": "\033[B", "pageup": "\033[5~", "pagedown": "\033[6~", "tab": "\t"}
FINISHED_MARKER = "·  finished"
READ_SIZE = 65536
SETTLE_SECONDS = 0.5
CELL_WIDTH = 9
CELL_HEIGHT = 18
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
BOLD_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
BACKGROUND = "#1e1e1e"
FOREGROUND = "#d4d4d4"
# pyte names the 16 standard colors; anything else arrives as a hex string.
PALETTE = {
    "black": "#000000",
    "red": "#cd3131",
    "green": "#0dbc79",
    "brown": "#e5e510",
    "blue": "#2472c8",
    "magenta": "#bc3fbc",
    "cyan": "#11a8cd",
    "white": "#e5e5e5",
    "brightblack": "#666666",
    "brightred": "#f14c4c",
    "brightgreen": "#23d18b",
    "brightbrown": "#f5f543",
    "brightblue": "#3b8eea",
    "brightmagenta": "#d670d6",
    "brightcyan": "#29b8db",
    "brightwhite": "#ffffff",
}


def parse_size(text: str) -> tuple[int, int]:
    columns, _, lines = text.partition("x")
    return int(columns), int(lines)


def color(name: str, default: str) -> str:
    if name == "default":
        return default
    return PALETTE.get(name, f"#{name}")


def screen_text(screen: pyte.Screen) -> str:
    return "\n".join(line.rstrip() for line in screen.display) + "\n"


def save_png(screen: pyte.Screen, path: Path) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return
    image = Image.new("RGB", (screen.columns * CELL_WIDTH, screen.lines * CELL_HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    regular = ImageFont.truetype(FONT, 15)
    bold = ImageFont.truetype(BOLD_FONT, 15)
    for row in range(screen.lines):
        line = screen.buffer[row]
        for column in range(screen.columns):
            cell = line[column]
            foreground = color(cell.fg, FOREGROUND)
            background = color(cell.bg, BACKGROUND)
            if cell.reverse:
                foreground, background = background, foreground
            left, top = column * CELL_WIDTH, row * CELL_HEIGHT
            if background != BACKGROUND:
                draw.rectangle([left, top, left + CELL_WIDTH - 1, top + CELL_HEIGHT - 1], fill=background)
            if cell.data.strip():
                draw.text((left, top), cell.data, fill=foreground, font=bold if cell.bold else regular)
    image.save(path)


def save_frame(screen: pyte.Screen, directory: Path, name: str) -> None:
    (directory / f"{name}.txt").write_text(screen_text(screen), encoding="utf-8")
    save_png(screen, directory / f"{name}.png")


def spawn(project: Path, columns: int, lines: int, extra: Sequence[str]) -> tuple[int, int]:
    pid, descriptor = pty.fork()
    if pid == 0:
        os.chdir(project)
        os.environ["TERM"] = "xterm-256color"
        os.execvp(sys.executable, [sys.executable, "-m", "strain", "run", "--ui", "live", *extra])  # noqa: S606 - replaces the forked child with strain, by design
    ioctl(descriptor, termios.TIOCSWINSZ, struct.pack("HHHH", lines, columns, 0, 0))
    os.kill(pid, signal.SIGWINCH)
    return pid, descriptor


def pump(descriptor: int, stream: pyte.ByteStream, seconds: float) -> bool:
    """Feeds the terminal output to `stream` for `seconds`; false once the program has exited."""
    deadline = time.monotonic() + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        ready, _, _ = select.select([descriptor], [], [], remaining)
        if not ready:
            continue
        try:
            data = os.read(descriptor, READ_SIZE)
        except OSError:
            return False
        if not data:
            return False
        stream.feed(data)
    return True


def parse_arguments(parser: argparse.ArgumentParser, argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """Parses our own options, which may follow the project, and returns what comes after `--` for `strain run`."""
    separator = argv.index("--") if "--" in argv else len(argv)
    return parser.parse_args(argv[:separator]), argv[separator + 1 :]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("project", type=Path)
    parser.add_argument("--size", type=parse_size, default=(120, 36), help="COLUMNSxLINES (default 120x36)")
    parser.add_argument("--keys", default="down,tab,q", help="keys to send once the run has finished")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between frames during the run")
    parser.add_argument("--out", type=Path, default=Path("ui-snapshots"))
    parser.add_argument("--timeout", type=float, default=600.0)
    options, extra = parse_arguments(parser, sys.argv[1:] if argv is None else list(argv))
    columns, lines = options.size
    options.out.mkdir(parents=True, exist_ok=True)
    screen = pyte.Screen(columns, lines)
    stream = pyte.ByteStream(screen)
    pid, descriptor = spawn(options.project.resolve(), columns, lines, extra)
    deadline = time.monotonic() + options.timeout
    frame = 0
    running = True
    while running and FINISHED_MARKER not in screen.display[0] and time.monotonic() < deadline:
        running = pump(descriptor, stream, options.interval)
        save_frame(screen, options.out, f"frame-{frame:03}")
        frame += 1
    keys = [key for key in options.keys.split(",") if key]
    sent = 0
    for number, key in enumerate(keys, start=1):
        if not running:
            break
        os.write(descriptor, KEYS.get(key, key).encode())
        running = pump(descriptor, stream, SETTLE_SECONDS)
        save_frame(screen, options.out, f"key-{number:02}-{key}")
        sent = number
    if running:
        pump(descriptor, stream, SETTLE_SECONDS)
        os.kill(pid, signal.SIGTERM)
    _, status = os.waitpid(pid, 0)
    os.close(descriptor)
    print(f"saved {frame} run frames and {sent} key frames to {options.out}")
    return os.waitstatus_to_exitcode(status)


if __name__ == "__main__":
    sys.exit(main())
