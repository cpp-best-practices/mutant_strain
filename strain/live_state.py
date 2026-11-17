"""What the live TUI shows: the state of the run, updated by the runner's console hooks from the runner's thread.

The TUI reads it under `lock` on its own thread, and redraws whatever `version` says has changed.
"""

import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TypeVar

from .coverage import LineCoverage
from .mutators import Mutant
from .report import FileScore, action_items, file_scores
from .results import DETECTED_STATES, CombinedResult, MutantResult, MutantState, combine, mutation_score
from .safety_nets import RelaxedSafetyNet, SafetyNet

RUNNING = "running"
PENDING = "pending"
SKIPPED = "skipped"
ELLIPSIS = "…"
TAB_WIDTH = 4
BUILD_LOG_LINES = 2000
SLOWEST_MUTANTS = 10

Clock = Callable[[], float]
T = TypeVar("T")


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def token_offset(source: str, mutant: Mutant) -> int:
    """Where the mutated token starts in the tab-expanded text of its line."""
    text = source.splitlines()[mutant.line - 1]
    return len(text[: mutant.column - 1].expandtabs(TAB_WIDTH))


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


def status_phase(status: str) -> str:
    """What the run is doing, from a status line such as `[3/6] src/a.cpp:7:13 [ror] `<` → `>=`  building…`."""
    if not status.endswith(ELLIPSIS):
        return ""
    return status.rpartition("  ")[2].removesuffix(ELLIPSIS)


def clock_time(seconds: float) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


@dataclass(slots=True)
class QueueEntry:
    mutant: Mutant
    state: str = PENDING
    duration: float | None = None


@dataclass(slots=True)
class MutantContext:
    """What the browsers show beyond the result: the source and each test's coverage of the line."""

    source: str
    executions: dict[str, LineCoverage] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Tally:
    """How many mutants a group has and how they fared, for the statistics tables."""

    name: str
    mutants: int
    detected: int
    survived: int

    @property
    def score(self) -> float | None:
        scored = self.detected + self.survived
        return None if scored == 0 else self.detected / scored


def tallies(groups: dict[str, list[MutantState]]) -> list[Tally]:
    return [
        Tally(name, len(states), sum(state in DETECTED_STATES for state in states), states.count(MutantState.SURVIVED))
        for name, states in sorted(groups.items())
    ]


class LiveState:
    """The run as the TUI shows it. Every change bumps `version`; `plan_version` and `build_version` mark resets."""

    def __init__(self, clock: Clock = time.monotonic) -> None:
        self.lock = threading.RLock()
        self.clock = clock
        self.started = clock()
        self.finished_at: float | None = None
        self.version = 0
        self.results_version = 0
        self.plan_version = 0
        self.build_version = 0
        self.status = ""
        self.paused = False
        self.waiting = False
        self.stopping = False
        self.done = False
        # None until the safety nets are checked; then the failing nets the run went on without, and the rest.
        self.relaxed_nets: list[RelaxedSafetyNet] | None = None
        self.holding_nets: list[SafetyNet] = []
        self._init_progress()

    def _init_progress(self) -> None:
        self.test = ""
        self.test_name = ""
        self.queue: list[QueueEntry] = []
        self.current: int | None = None
        self.mutant_started_at: float | None = None
        self.results: list[MutantResult] = []
        self.contexts: dict[str, MutantContext] = {}
        self.sources: dict[str, str] = {}
        self.durations: dict[str, float] = {}
        self.score_history: list[float] = []
        self.build_command = ""
        self.build_log: deque[str] = deque(maxlen=BUILD_LOG_LINES)
        # How many build lines arrived in all, so a reader can tell which ones it has not shown yet.
        self.build_lines_seen = 0
        # What the Log view shows: the console lines and the warnings, in the order they came.
        self.log: list[str] = []
        self._combined: list[CombinedResult] | None = None
        self._derived: dict[str, object] = {}

    def _changed(self) -> None:
        self.version += 1

    # Updates from the run.

    def set_test(self, name: str, number: int, total: int) -> None:
        """Starts a test with an empty queue, so the title never pairs it with the previous test's mutants."""
        with self.lock:
            self.test = f"Test {number}/{total}: {name}"
            self.test_name = name
            self._set_queue([])

    def plan(self, mutants: Sequence[Mutant]) -> None:
        with self.lock:
            self._set_queue([QueueEntry(mutant) for mutant in mutants])

    def _set_queue(self, queue: list[QueueEntry]) -> None:
        self.queue = queue
        self.current = None
        self.plan_version += 1
        self._changed()

    def start_mutant(self, index: int, source: str, mutant: Mutant) -> None:
        with self.lock:
            self.current = index
            self.queue[index].state = RUNNING
            self.sources[mutant.file] = source
            self.mutant_started_at = self.clock()
            self._changed()

    def finish_mutant(self, index: int, state: str) -> None:
        with self.lock:
            entry = self.queue[index]
            entry.state = state
            if self.mutant_started_at is not None and index == self.current:
                entry.duration = self.clock() - self.mutant_started_at
                self.durations[entry.mutant.id] = self.durations.get(entry.mutant.id, 0.0) + entry.duration
                self.mutant_started_at = None
            self._changed()

    def record_result(self, result: MutantResult, source: str, line: LineCoverage | None) -> None:
        with self.lock:
            self.results.append(result)
            self.sources.setdefault(result.mutant.file, source)
            context = self.contexts.setdefault(result.mutant.id, MutantContext(source))
            if line is not None:
                context.executions[result.test] = line
            self._combined = None
            self._derived.clear()
            score = mutation_score(entry.state for entry in self.combined())
            if score is not None:
                self.score_history.append(score)
            self.results_version += 1
            self._changed()

    def finish_run(self) -> None:
        """Ends the run with every mutant in the queue, in its state combined over all tests."""
        with self.lock:
            self.done = True
            self.paused = False
            self.finished_at = self.clock()
            self.test_name = "all tests"
            self._set_queue(
                [
                    QueueEntry(entry.mutant, entry.state, self.durations.get(entry.mutant.id))
                    for entry in self.combined()
                ]
            )

    def start_build(self, command: str) -> None:
        with self.lock:
            self.build_command = command
            self.build_log.clear()
            self.build_version += 1
            self._changed()

    def build_output(self, line: str) -> None:
        with self.lock:
            self.build_log.append(line.rstrip().expandtabs(TAB_WIDTH))
            self.build_lines_seen += 1
            self._changed()

    def add_line(self, text: str) -> None:
        with self.lock:
            self.log.append(text)
            if text.strip():
                self.status = text.strip()
            self._changed()

    def set_status(self, text: str) -> None:
        with self.lock:
            self.status = text.strip()
            self._changed()

    def set_safety_nets(self, relaxed: Sequence[RelaxedSafetyNet], holding: Sequence[SafetyNet]) -> None:
        with self.lock:
            self.relaxed_nets = list(relaxed)
            self.holding_nets = list(holding)
            self._changed()

    def bug_hiding_nets(self) -> list[RelaxedSafetyNet]:
        """The relaxed nets without which a mutant that breaks the code can survive."""
        return [failure for failure in self.relaxed_nets or [] if failure.hides_bugs]

    def set_flag(self, name: str, value: bool) -> None:
        with self.lock:
            setattr(self, name, value)
            self._changed()

    # What the results add up to; callers hold `lock`.

    def combined(self) -> list[CombinedResult]:
        if self._combined is None:
            self._combined = combine(self.results)
        return self._combined

    def _remember(self, name: str, compute: Callable[[], T]) -> T:
        """`compute()`, kept until the next result arrives; the TUI redraws far more often than results come."""
        if name not in self._derived:
            self._derived[name] = compute()
        return self._derived[name]  # type: ignore[return-value]

    def survivors(self) -> list[CombinedResult]:
        return self._remember(
            "survivors", lambda: [entry for entry in self.combined() if entry.state is MutantState.SURVIVED]
        )

    def findings(self) -> list[str]:
        return self._remember("findings", lambda: action_items(self.combined(), limit=None))

    def file_scores(self) -> list[FileScore]:
        return self._remember("files", lambda: file_scores(self.combined()))

    def operator_tallies(self) -> list[Tally]:
        def compute() -> list[Tally]:
            groups: dict[str, list[MutantState]] = {}
            for entry in self.combined():
                groups.setdefault(entry.mutant.operator, []).append(entry.state)
            return tallies(groups)

        return self._remember("operators", compute)

    def test_tallies(self) -> list[Tally]:
        """How each test fared against the mutants it ran on; a mutant counts once for every test that ran it."""

        def compute() -> list[Tally]:
            groups: dict[str, list[MutantState]] = {}
            for result in self.results:
                groups.setdefault(result.test, []).append(result.state)
            return tallies(groups)

        return self._remember("tests", compute)

    def slowest(self) -> list[tuple[float, CombinedResult]]:
        by_id = {entry.mutant.id: entry for entry in self.combined()}
        timed = sorted(
            ((duration, by_id[mutant_id]) for mutant_id, duration in self.durations.items() if mutant_id in by_id),
            key=lambda pair: -pair[0],
        )
        return timed[:SLOWEST_MUTANTS]

    def counts(self) -> dict[MutantState, int]:
        states = [entry.state for entry in self.combined()]
        return {state: states.count(state) for state in MutantState if state in states}

    def score(self) -> float | None:
        return mutation_score(entry.state for entry in self.combined())

    def elapsed(self) -> float:
        return (self.finished_at if self.finished_at is not None else self.clock()) - self.started

    def done_count(self) -> int:
        return sum(entry.state not in (PENDING, RUNNING) for entry in self.queue)

    def mean_duration(self) -> float | None:
        timed = [entry.duration for entry in self.queue if entry.duration is not None]
        return sum(timed) / len(timed) if timed else None

    def remaining_seconds(self) -> float | None:
        """How long the queue of the current test will take, from the average time per mutant so far."""
        mean = self.mean_duration()
        if mean is None or self.done:
            return None
        return mean * (len(self.queue) - self.done_count())

    def phase(self) -> str:
        return status_phase(self.status)

    def current_elapsed(self) -> float | None:
        return None if self.mutant_started_at is None else self.clock() - self.mutant_started_at

    def mutant_index(self, mutant_id: str) -> int | None:
        for index, entry in enumerate(self.queue):
            if entry.mutant.id == mutant_id:
                return index
        return None
