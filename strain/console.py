"""Human-readable console output: phases, steps, a live status line and colored mutant states."""

import enum
import os
from collections.abc import Mapping, Sequence
from typing import TextIO

from .coverage import LineCoverage
from .mutators import Mutant
from .results import MutantResult, MutantState

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
CLEAR_LINE = "\r\033[K"

STATE_STYLES: Mapping[str, str] = {
    MutantState.KILLED: GREEN,
    MutantState.KILLED_BY_SANITIZER: GREEN,
    MutantState.TIMEOUT: GREEN,
    MutantState.SURVIVED: BOLD + RED,
    MutantState.SKIPPED_UNEVALUATED: DIM,
    MutantState.COMPILE_ERROR: DIM,
    MutantState.IGNORED: DIM,
}
STATE_WIDTH = max(len(state) for state in MutantState)
# Scores at or above these percentages are shown green or yellow, the rest red; they match report.json.
HIGH_SCORE = 80.0
LOW_SCORE = 60.0


class UiMode(enum.StrEnum):
    AUTO = "auto"
    LIVE = "live"
    LOG = "log"
    JSON = "json"


INTERACTIVE_MODES = frozenset({UiMode.AUTO, UiMode.LIVE})


def is_terminal(stream: TextIO) -> bool:
    isatty = getattr(stream, "isatty", None)
    return isatty is not None and isatty()


def wants_color(stream: TextIO, environment: Mapping[str, str] = os.environ) -> bool:
    if "NO_COLOR" in environment:
        return False
    return bool(environment.get("FORCE_COLOR")) or is_terminal(stream)


def paint(text: str, style: str, color: bool) -> str:
    return f"{style}{text}{RESET}" if color and style else text


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def score_style(percent: float | None) -> str:
    if percent is None:
        return DIM
    if percent >= HIGH_SCORE:
        return BOLD + GREEN
    return BOLD + (YELLOW if percent >= LOW_SCORE else RED)


class Console:
    def __init__(self, out: TextIO, err: TextIO, mode: UiMode = UiMode.AUTO) -> None:
        self.out = out
        self.err = err
        self.enabled = mode is not UiMode.JSON
        self.live = mode in INTERACTIVE_MODES and is_terminal(out)
        self.color = mode in INTERACTIVE_MODES and wants_color(out)
        self.err_color = mode is not UiMode.LOG and wants_color(err)
        self._status_shown = False

    def paint(self, text: str, style: str) -> str:
        return paint(text, style, self.color)

    def finish(self) -> None:
        self._clear_status()
        self.out.flush()

    def _clear_status(self) -> None:
        if self._status_shown:
            self.out.write(CLEAR_LINE)
            self._status_shown = False

    def line(self, text: str = "") -> None:
        if self.enabled:
            self._clear_status()
            print(text, file=self.out, flush=True)

    def status(self, text: str) -> None:
        if self.enabled and self.live:
            self.out.write(f"{CLEAR_LINE}{self.paint(text, DIM)}")
            self.out.flush()
            self._status_shown = True

    def heading(self, text: str) -> None:
        self.line()
        self.line(self.paint(f"▸ {text}", BOLD + CYAN))

    def step(self, text: str) -> None:
        self.line(f"  {self.paint('✓', GREEN)} {text}")

    def note(self, text: str) -> None:
        self.line(f"  {self.paint('·', DIM)} {text}")

    def field(self, name: str, value: str, width: int) -> None:
        self.line(f"  {self.paint(f'{name:<{width}}', DIM)}  {value}")

    def state(self, label: str) -> str:
        return self.paint(f"{label:<{STATE_WIDTH}}", STATE_STYLES.get(label, DIM))

    def score(self, percent: float | None) -> str:
        return self.paint("n/a" if percent is None else f"{percent:.1f}%", score_style(percent))

    def warn(self, message: str) -> None:
        self.finish()
        print(f"strain: {paint('warning:', BOLD + YELLOW, self.err_color)} {message}", file=self.err, flush=True)

    # Progress hooks for the live dashboard; plain console output shows the same progress as lines.
    def test_started(self, name: str, number: int, total: int) -> None:
        pass

    def plan(self, mutants: Sequence[Mutant]) -> None:
        pass

    def mutant_started(self, index: int, source: str, mutant: Mutant) -> None:
        pass

    def mutant_finished(self, index: int, state: str) -> None:
        pass

    def mutant_evaluated(self, result: MutantResult, source: str, line: LineCoverage | None) -> None:
        pass

    def build_started(self, command: str) -> None:
        pass

    def build_output(self, line: str) -> None:
        pass

    def run_finished(self) -> None:
        pass
