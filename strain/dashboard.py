"""The `--ui live` dashboard: the mutated code, the mutant queue, live findings, the build log and a survivor browser."""

import collections
import os
import re
import shutil
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TextIO

from .config import CONFIG_FILE
from .console import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    RED,
    RESET,
    STATE_STYLES,
    STATE_WIDTH,
    Console,
    UiMode,
    is_terminal,
    paint,
    plural,
)
from .coverage import LineCoverage
from .keys import KeyHandler, KeyReader, stdin_key_reader
from .mutators import Mutant, apply_mutant, describe_mutant
from .report import action_items, hint
from .results import CombinedResult, MutantResult, MutantState, combine, mutation_score

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
RUN_VIEW = "run"
SURVIVORS_VIEW = "survivors"
PAGE_ROWS = 10
DETAIL_LABEL_WIDTH = 10
# How often the finished run checks whether the user pressed q.
QUIT_POLL_SECONDS = 0.1

TerminalSize = Callable[[], os.terminal_size]
KeyReaderFactory = Callable[[KeyHandler], KeyReader | None]


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


def window_start(focus: int, count: int, rows: int) -> int:
    """The first of `rows` consecutive items out of `count` that keeps `focus` in the middle."""
    return max(0, min(focus - rows // 2, count - rows))


def execution_summary(line: int, executions: dict[str, LineCoverage]) -> str:
    """How often each test ran `line`, and how many of its branches they took."""
    if not executions:
        return f"no coverage data for line {line}"
    total = sum(coverage.count for coverage in executions.values())
    if len(executions) == 1:
        summary = f"line {line} executed {plural(total, 'time')} by {next(iter(executions))}"
    else:
        per_test = ", ".join(f"{test} {coverage.count}" for test, coverage in executions.items())
        summary = f"line {line} executed {plural(total, 'time')}: {per_test}"
    branches = max(coverage.branches for coverage in executions.values())
    if branches == 0:
        return summary
    taken = max(coverage.branches_taken for coverage in executions.values())
    return f"{summary}; {taken}/{branches} branches taken"


@dataclass(slots=True)
class QueueEntry:
    description: str
    state: str = PENDING


@dataclass(slots=True)
class MutantContext:
    """What the survivor browser shows beyond the result: the source and each test's coverage of the line."""

    source: str
    executions: dict[str, LineCoverage] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Layout:
    snippet: int
    queue: int
    findings: int
    build_log: int


def layout(body_rows: int, snippet_rows: int) -> Layout:
    """Rows for each panel, title included: the snippet first, then the rest split over queue, findings and build log."""
    snippet = min(body_rows, snippet_rows + 1)
    rest = body_rows - snippet
    queue = (rest + 2) // 3
    findings = (rest - queue + 1) // 2
    return Layout(snippet, queue, findings, rest - queue - findings)


def survivor_layout(body_rows: int, survivors: int) -> tuple[int, int]:
    """Rows for the survivor list and the details of the selected survivor, titles included."""
    survivor_list = min(body_rows, max(1, survivors) + 1, max(2, body_rows // 3))
    return survivor_list, body_rows - survivor_list


class Dashboard:
    def __init__(self, color: bool, interactive: bool = False) -> None:
        self.color = color
        self.interactive = interactive
        self.view = RUN_VIEW
        self.run_done = False
        self.selected = 0
        self.results: list[MutantResult] = []
        self.contexts: dict[str, MutantContext] = {}
        self._combined: list[CombinedResult] | None = None
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

    def record_result(self, result: MutantResult, source: str, line: LineCoverage | None) -> None:
        self.results.append(result)
        context = self.contexts.setdefault(result.mutant.id, MutantContext(source))
        if line is not None:
            context.executions[result.test] = line
        self._combined = None

    def combined(self) -> list[CombinedResult]:
        if self._combined is None:
            self._combined = combine(self.results)
        return self._combined

    def survivors(self) -> list[CombinedResult]:
        return [entry for entry in self.combined() if entry.state is MutantState.SURVIVED]

    def selected_survivor(self, survivors: Sequence[CombinedResult]) -> int:
        return max(0, min(self.selected, len(survivors) - 1))

    def press(self, key: str) -> None:
        if key == "tab":
            self.view = SURVIVORS_VIEW if self.view == RUN_VIEW else RUN_VIEW
            return
        steps = {"up": -1, "down": 1, "pageup": -PAGE_ROWS, "pagedown": PAGE_ROWS}
        if self.view == SURVIVORS_VIEW and key in steps:
            survivors = self.survivors()
            self.selected = max(0, min(self.selected_survivor(survivors) + steps[key], len(survivors) - 1))

    def finish_run(self) -> None:
        self.run_done = True
        if self.survivors():
            self.view = SURVIVORS_VIEW

    def start_build(self, command: str) -> None:
        self.build_command = command
        self.build_log.clear()

    def build_output(self, line: str) -> None:
        self.build_log.append(line.rstrip().expandtabs(TAB_WIDTH))

    def set_status(self, text: str) -> None:
        self.status = text.strip()

    def key_hints(self) -> str:
        if not self.interactive:
            return ""
        hints = ["tab survivors"] if self.view == RUN_VIEW else ["tab run", "↑↓ select"]
        if self.run_done:
            hints.append("q quit")
        return self.paint("  ".join(hints), DIM)

    def title(self) -> str:
        parts = [self.paint("strain live", BOLD + CYAN), self.test]
        if self.run_done:
            parts.append("finished")
        elif self.current is not None:
            parts.append(f"mutant {self.current + 1}/{len(self.queue)}")
        parts.append(self.key_hints())
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

    def snippet_lines(self, snippet: Sequence[str]) -> list[str]:
        styles = {"-": RED, "+": GREEN}
        return [self.paint(line, styles.get(line[0], "")) for line in snippet]

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
        start = window_start(self.current or 0, len(self.queue), rows)
        lines = [self.queue_line(index, self.queue[index]) for index in range(start, min(len(self.queue), start + rows))]
        if rows < QUEUE_MARKER_ROWS:
            return lines
        hidden_after = len(self.queue) - (start + rows)
        if start > 0:
            lines[0] = self.paint(f"  ↑ {start + 1} earlier", DIM)
        if hidden_after > 0:
            lines[-1] = self.paint(f"  ↓ {hidden_after + 1} more", DIM)
        return lines

    def findings_lines(self, rows: int) -> list[str]:
        if rows <= 0:
            return []
        items = action_items(self.combined(), limit=None)
        if not items:
            return [self.paint("  no findings yet", DIM)]
        lines = [f"  {item}" for item in items]
        if len(lines) > rows:
            return [*lines[: rows - 1], self.paint(f"  … {len(lines) - rows + 1} more; tab shows the survivors", DIM)]
        return lines

    def survivor_list_lines(self, rows: int) -> list[str]:
        survivors = self.survivors()
        if rows <= 0:
            return []
        if not survivors:
            return [self.paint("  no surviving mutants", DIM)]
        selected = self.selected_survivor(survivors)
        start = window_start(selected, len(survivors), rows)
        return [
            f"{'▶' if index == selected else ' '} {describe_mutant(survivors[index].mutant)}"
            for index in range(start, min(len(survivors), start + rows))
        ]

    def detail(self, label: str, value: str) -> str:
        return f"{self.paint(f'{label:<{DETAIL_LABEL_WIDTH}}', DIM)} {value}"

    def survivor_details(self, entry: CombinedResult) -> list[str]:
        mutant = entry.mutant
        context = self.contexts[mutant.id]
        kind = "" if entry.survivor is None else f"{entry.survivor} "
        return [
            f"id {mutant.id}  {self.paint(f'equivalent? list it under [ignore] mutants in {CONFIG_FILE}', DIM)}",
            self.detail("function", entry.function or "unknown"),
            self.detail("coverage", execution_summary(mutant.line, context.executions)),
            self.detail(str(MutantState.SURVIVED), f"{kind}under {', '.join(entry.covered_by)}"),
            self.detail("hint", hint(entry)),
            "",
            *self.snippet_lines(code_snippet(context.source, mutant)),
        ]

    def survivor_detail_lines(self, rows: int) -> list[str]:
        survivors = self.survivors()
        if rows <= 0 or not survivors:
            return []
        return self.survivor_details(survivors[self.selected_survivor(survivors)])[:rows]

    def build_log_lines(self, rows: int) -> list[str]:
        if rows <= 0:
            return []
        return list(self.build_log)[-rows:]

    def panel(self, title: str, rows: int, content: Callable[[int], list[str]], width: int) -> list[str]:
        if rows <= 0:
            return []
        body = content(rows - 1)
        return [self.rule(title, width), *body, *[""] * (rows - 1 - len(body))]

    def run_panels(self, body_rows: int, width: int) -> list[str]:
        rows = layout(body_rows, len(self.snippet))
        build_title = f"Build log: {self.build_command}" if self.build_command else "Build log"
        return [
            *self.panel(
                f"Mutant: {self.mutant_title}", rows.snippet, lambda count: self.snippet_lines(self.snippet)[:count], width
            ),
            *self.panel("Mutants", rows.queue, self.queue_lines, width),
            *self.panel("Findings", rows.findings, self.findings_lines, width),
            *self.panel(build_title, rows.build_log, self.build_log_lines, width),
        ]

    def survivor_panels(self, body_rows: int, width: int) -> list[str]:
        survivors = self.survivors()
        list_rows, detail_rows = survivor_layout(body_rows, len(survivors))
        detail_title = "Survivor"
        if survivors:
            detail_title = f"Survivor: {describe_mutant(survivors[self.selected_survivor(survivors)].mutant)}"
        return [
            *self.panel(f"Survivors ({len(survivors)})", list_rows, self.survivor_list_lines, width),
            *self.panel(detail_title, detail_rows, self.survivor_detail_lines, width),
        ]

    def render(self, width: int, height: int) -> list[str]:
        header = [self.title(), self.tally()]
        footer = [self.paint(self.status, DIM)]
        body_rows = max(0, height - len(header) - len(footer))
        panels = self.run_panels if self.view == RUN_VIEW else self.survivor_panels
        lines = [*header, *panels(body_rows, width), *footer][:height]
        return [fit(line, width) for line in lines]


class LiveConsole(Console):
    """Draws the dashboard on the alternate screen, then prints the plain transcript once the run is over."""

    def __init__(
        self,
        out: TextIO,
        err: TextIO,
        terminal_size: TerminalSize = shutil.get_terminal_size,
        key_reader: KeyReaderFactory = stdin_key_reader,
    ) -> None:
        super().__init__(out, err, UiMode.LIVE)
        # Keys arrive on their own thread; the lock keeps them from redrawing in the middle of an update.
        self.lock = threading.RLock()
        self.quit = threading.Event()
        self.dashboard = Dashboard(self.color)
        self.terminal_size = terminal_size
        self.transcript: list[str] = []
        self.warnings: list[str] = []
        self.active = True
        self.last_refresh = 0.0
        out.write(ALTERNATE_SCREEN)
        self.refresh()
        self.keys = key_reader(self.key_pressed)
        self.dashboard.interactive = self.keys is not None

    def refresh(self) -> None:
        with self.lock:
            size = self.terminal_size()
            # One column short of the edge, so a full line never wraps and scrolls the screen.
            frame = "\n".join(self.dashboard.render(max(0, size.columns - 1), size.lines))
            self.out.write(f"{HOME}{frame}{CLEAR_TO_END_OF_SCREEN}")
            self.out.flush()
            self.last_refresh = time.monotonic()

    def key_pressed(self, key: str) -> None:
        with self.lock:
            if not self.active:
                return
            if key == "q" and self.dashboard.run_done:
                self.quit.set()
                return
            self.dashboard.press(key)
            self.refresh()

    def line(self, text: str = "") -> None:
        if not self.active:
            super().line(text)
            return
        self.transcript.append(text)
        if text:
            self.status(text)

    def status(self, text: str) -> None:
        with self.lock:
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
        # Outside the lock: the key thread may be waiting for it, and closing waits for that thread.
        if self.keys is not None:
            self.keys.close()
        with self.lock:
            if self.active:
                self.active = False
                self.out.write(MAIN_SCREEN)
                for text in self.transcript:
                    super().line(text)
                for message in self.warnings:
                    super().warn(message)
            super().finish()

    def run_finished(self) -> None:
        """Leaves the results on screen to browse until the user presses q."""
        with self.lock:
            self.dashboard.finish_run()
            self.dashboard.set_status("finished; browse the survivors, then press q for the full transcript")
            self.refresh()
        if self.keys is not None:
            while not self.quit.wait(QUIT_POLL_SECONDS):
                pass

    def test_started(self, name: str, number: int, total: int) -> None:
        with self.lock:
            self.dashboard.set_test(name, number, total)
            self.refresh()

    def plan(self, mutants: Sequence[Mutant]) -> None:
        with self.lock:
            self.dashboard.set_plan([describe_mutant(mutant) for mutant in mutants])
            self.refresh()

    def mutant_started(self, index: int, source: str, mutant: Mutant) -> None:
        with self.lock:
            self.dashboard.start_mutant(index, source, mutant)
            self.refresh()

    def mutant_finished(self, index: int, state: str) -> None:
        with self.lock:
            self.dashboard.finish_mutant(index, state)
            self.refresh()

    def mutant_evaluated(self, result: MutantResult, source: str, line: LineCoverage | None) -> None:
        with self.lock:
            self.dashboard.record_result(result, source, line)
            self.refresh()

    def build_started(self, command: str) -> None:
        with self.lock:
            self.dashboard.start_build(command)
            self.refresh()

    def build_output(self, line: str) -> None:
        with self.lock:
            self.dashboard.build_output(line)
            if time.monotonic() - self.last_refresh >= BUILD_OUTPUT_REFRESH_SECONDS:
                self.refresh()


def create_console(out: TextIO, err: TextIO, mode: UiMode) -> Console:
    """The dashboard for `--ui live` on a terminal; plain console output everywhere else."""
    if mode is UiMode.LIVE and is_terminal(out):
        return LiveConsole(out, err)
    return Console(out, err, mode)
