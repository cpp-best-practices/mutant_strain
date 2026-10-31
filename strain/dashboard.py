"""The `--ui live` dashboard: a full-screen view of the mutated code, the build log and the mutant queue."""

import collections
import os
import re
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TextIO

from .console import BOLD, CYAN, DIM, GREEN, RED, RESET, STATE_STYLES, STATE_WIDTH, Console, UiMode, is_terminal, paint
from .mutators import Mutant, apply_mutant, describe_mutant
from .results import MutantState, mutation_score

ALTERNATE_SCREEN = "\033[?1049h\033[?25l"
MAIN_SCREEN = "\033[?25h\033[?1049l"
HOME = "\033[H"
CLEAR_TO_END_OF_SCREEN = "\033[J"
ESCAPE_SEQUENCE = re.compile(r"(\033\[[0-9;?]*[A-Za-z])")
ELLIPSIS = "…"
TAB_WIDTH = 4
SNIPPET_CONTEXT = 3
BUILD_LOG_LINES = 200
# Builds can print thousands of lines; redrawing for each one would cost more than the build.
BUILD_OUTPUT_REFRESH_SECONDS = 0.05
# The queue needs this many rows before it can spare two for the "earlier" and "more" markers.
QUEUE_MARKER_ROWS = 3
RUNNING = "running"
PENDING = "pending"

TerminalSize = Callable[[], os.terminal_size]


def visible_length(text: str) -> int:
    return len(ESCAPE_SEQUENCE.sub("", text))


def fit(text: str, width: int) -> str:
    """`text` padded or truncated to exactly `width` visible characters, keeping its escape sequences."""
    if width <= 0:
        return ""
    excess = visible_length(text) - width
    if excess <= 0:
        return text + " " * -excess
    remaining = width - len(ELLIPSIS)
    kept: list[str] = []
    for index, piece in enumerate(ESCAPE_SEQUENCE.split(text)):
        is_escape = index % 2 == 1
        kept.append(piece if is_escape else piece[:remaining])
        remaining -= 0 if is_escape else len(kept[-1])
    reset = RESET if ESCAPE_SEQUENCE.search(text) else ""
    return f"{''.join(kept)}{ELLIPSIS}{reset}"


def code_snippet(source: str, mutant: Mutant, context: int = SNIPPET_CONTEXT) -> list[str]:
    """The lines around `mutant`, with its line shown before (`-`) and after (`+`) the mutation."""
    lines = source.splitlines()
    mutated = apply_mutant(source, mutant).splitlines()[mutant.line - 1]
    first = max(1, mutant.line - context)
    last = min(len(lines), mutant.line + context)
    width = len(str(last))

    def numbered(marker: str, number: int, text: str) -> str:
        return f"{marker} {number:>{width}} │ {text.expandtabs(TAB_WIDTH)}"

    snippet: list[str] = []
    for number in range(first, last + 1):
        if number == mutant.line:
            snippet += [numbered("-", number, lines[number - 1]), numbered("+", number, mutated)]
        else:
            snippet.append(numbered(" ", number, lines[number - 1]))
    return snippet


@dataclass(slots=True)
class QueueEntry:
    description: str
    state: str = PENDING


@dataclass(frozen=True, slots=True)
class Layout:
    snippet: int
    queue: int
    build_log: int


def layout(body_rows: int, snippet_rows: int) -> Layout:
    """Rows for each panel, title included: the snippet first, then the rest split between queue and build log."""
    snippet = min(body_rows, snippet_rows + 1)
    rest = body_rows - snippet
    queue = (rest + 1) // 2
    return Layout(snippet, queue, rest - queue)


class Dashboard:
    def __init__(self, color: bool) -> None:
        self.color = color
        self.test = ""
        self.queue: list[QueueEntry] = []
        self.current: int | None = None
        self.mutant_title = "waiting for the first mutant"
        self.snippet: list[str] = []
        self.build_command = ""
        self.build_log: collections.deque[str] = collections.deque(maxlen=BUILD_LOG_LINES)
        self.status = ""
        self.finished: list[MutantState] = []

    def paint(self, text: str, style: str) -> str:
        return paint(text, style, self.color)

    def set_test(self, name: str, number: int, total: int) -> None:
        self.test = f"Test {number}/{total}: {name}"

    def set_plan(self, descriptions: Sequence[str]) -> None:
        self.queue = [QueueEntry(description) for description in descriptions]
        self.current = None

    def start_mutant(self, index: int, source: str, mutant: Mutant) -> None:
        self.current = index
        self.queue[index].state = RUNNING
        self.mutant_title = describe_mutant(mutant)
        self.snippet = code_snippet(source, mutant)

    def finish_mutant(self, index: int, state: str) -> None:
        self.queue[index].state = state
        if isinstance(state, MutantState):
            self.finished.append(state)

    def start_build(self, command: str) -> None:
        self.build_command = command
        self.build_log.clear()

    def build_output(self, line: str) -> None:
        self.build_log.append(line.rstrip().expandtabs(TAB_WIDTH))

    def set_status(self, text: str) -> None:
        self.status = text.strip()

    def title(self) -> str:
        parts = [self.paint("strain live", BOLD + CYAN), self.test]
        if self.current is not None:
            parts.append(f"mutant {self.current + 1}/{len(self.queue)}")
        return "  ·  ".join(part for part in parts if part)

    def tally(self) -> str:
        counts = [
            self.paint(f"{state} {self.finished.count(state)}", STATE_STYLES[state])
            for state in MutantState
            if state in self.finished
        ]
        score = mutation_score(self.finished)
        score_text = "n/a" if score is None else f"{score * 100:.1f}%"
        return "  ".join([*counts, f"score {score_text}"])

    def rule(self, title: str, width: int) -> str:
        heading = f"─ {title} "
        return self.paint(fit(heading + "─" * max(0, width - len(heading)), width), BOLD + CYAN)

    def snippet_lines(self) -> list[str]:
        styles = {"-": RED, "+": GREEN}
        return [self.paint(line, styles.get(line[0], "")) for line in self.snippet]

    def queue_line(self, index: int, entry: QueueEntry) -> str:
        marker = "▶" if index == self.current else " "
        state = self.paint(f"{entry.state:<{STATE_WIDTH}}", STATE_STYLES.get(entry.state, DIM))
        return f"{marker} {state} {entry.description}"

    def queue_lines(self, rows: int) -> list[str]:
        """A window over the queue that keeps the current mutant in the middle."""
        if rows <= 0:
            return []
        if not self.queue:
            return [self.paint("  no mutants planned yet", DIM)]
        start = max(0, min((self.current or 0) - rows // 2, len(self.queue) - rows))
        lines = [self.queue_line(index, self.queue[index]) for index in range(start, min(len(self.queue), start + rows))]
        if rows < QUEUE_MARKER_ROWS:
            return lines
        hidden_after = len(self.queue) - (start + rows)
        if start > 0:
            lines[0] = self.paint(f"  ↑ {start + 1} earlier", DIM)
        if hidden_after > 0:
            lines[-1] = self.paint(f"  ↓ {hidden_after + 1} more", DIM)
        return lines

    def build_log_lines(self, rows: int) -> list[str]:
        if rows <= 0:
            return []
        return list(self.build_log)[-rows:]

    def panel(self, title: str, rows: int, content: Callable[[int], list[str]], width: int) -> list[str]:
        if rows <= 0:
            return []
        body = content(rows - 1)
        return [self.rule(title, width), *body, *[""] * (rows - 1 - len(body))]

    def render(self, width: int, height: int) -> list[str]:
        header = [self.title(), self.tally()]
        footer = [self.paint(self.status, DIM)]
        rows = layout(max(0, height - len(header) - len(footer)), len(self.snippet))
        build_title = f"Build log: {self.build_command}" if self.build_command else "Build log"
        lines = [
            *header,
            *self.panel(f"Mutant: {self.mutant_title}", rows.snippet, lambda count: self.snippet_lines()[:count], width),
            *self.panel("Mutants", rows.queue, self.queue_lines, width),
            *self.panel(build_title, rows.build_log, self.build_log_lines, width),
            *footer,
        ][:height]
        return [fit(line, width) for line in lines]


class LiveConsole(Console):
    """Draws the dashboard on the alternate screen, then prints the plain transcript once the run is over."""

    def __init__(self, out: TextIO, err: TextIO, terminal_size: TerminalSize = shutil.get_terminal_size) -> None:
        super().__init__(out, err, UiMode.LIVE)
        self.dashboard = Dashboard(self.color)
        self.terminal_size = terminal_size
        self.transcript: list[str] = []
        self.warnings: list[str] = []
        self.active = True
        self.last_refresh = 0.0
        out.write(ALTERNATE_SCREEN)
        self.refresh()

    def refresh(self) -> None:
        size = self.terminal_size()
        # One column short of the edge, so a full line never wraps and scrolls the screen.
        frame = "\n".join(self.dashboard.render(max(0, size.columns - 1), size.lines))
        self.out.write(f"{HOME}{frame}{CLEAR_TO_END_OF_SCREEN}")
        self.out.flush()
        self.last_refresh = time.monotonic()

    def line(self, text: str = "") -> None:
        if not self.active:
            super().line(text)
            return
        self.transcript.append(text)
        if text:
            self.status(text)

    def status(self, text: str) -> None:
        if self.active:
            self.dashboard.set_status(text)
            self.refresh()

    def warn(self, message: str) -> None:
        if not self.active:
            super().warn(message)
            return
        self.warnings.append(message)
        self.status(f"warning: {message}")

    def finish(self) -> None:
        if self.active:
            self.active = False
            self.out.write(MAIN_SCREEN)
            for text in self.transcript:
                super().line(text)
            for message in self.warnings:
                super().warn(message)
        super().finish()

    def test_started(self, name: str, number: int, total: int) -> None:
        self.dashboard.set_test(name, number, total)
        self.refresh()

    def plan(self, mutants: Sequence[Mutant]) -> None:
        self.dashboard.set_plan([describe_mutant(mutant) for mutant in mutants])
        self.refresh()

    def mutant_started(self, index: int, source: str, mutant: Mutant) -> None:
        self.dashboard.start_mutant(index, source, mutant)
        self.refresh()

    def mutant_finished(self, index: int, state: str) -> None:
        self.dashboard.finish_mutant(index, state)
        self.refresh()

    def build_started(self, command: str) -> None:
        self.dashboard.start_build(command)
        self.refresh()

    def build_output(self, line: str) -> None:
        self.dashboard.build_output(line)
        if time.monotonic() - self.last_refresh >= BUILD_OUTPUT_REFRESH_SECONDS:
            self.refresh()


def create_console(out: TextIO, err: TextIO, mode: UiMode) -> Console:
    """The dashboard for `--ui live` on a terminal; plain console output everywhere else."""
    if mode is UiMode.LIVE and is_terminal(out):
        return LiveConsole(out, err)
    return Console(out, err, mode)
