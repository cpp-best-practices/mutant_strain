"""The `--ui live` TUI, built on Textual: the run as it happens, the survivors, every mutant, statistics and the log.

Textual must own the main thread, so `TuiConsole.run` starts the runner on a worker thread. The runner's console
hooks update a `LiveState`, which the app redraws from several times a second. Stopping the run (Ctrl-C, or SIGTERM)
raises KeyboardInterrupt from the runner's next console hook, so the runner restores the sources and writes its
reports as it does for a plain Ctrl-C.
"""

import functools
import signal
import threading
from collections.abc import Callable, Sequence
from types import FrameType
from typing import ClassVar, TextIO

from rich.console import Group, RenderableType
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.css.query import NoMatches
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import DataTable, Footer, RichLog, Static, Tab, TabbedContent, TabPane, Tabs

from .config import CONFIG_FILE
from .console import Console, UiMode
from .coverage import LineCoverage
from .live_state import (
    PENDING,
    RUNNING,
    TAB_WIDTH,
    LiveState,
    QueueEntry,
    Tally,
    clock_time,
    execution_summary,
    plural,
    token_offset,
)
from .mutators import Mutant, apply_mutant, describe_mutant
from .report import format_score, hint
from .results import CombinedResult, MutantResult, MutantState
from .safety_nets import Level, RelaxedSafetyNet, SafetyNet

REFRESH_SECONDS = 0.1
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_FRAMES_PER_SECOND = 10
SPARKS = "▁▂▃▄▅▆▇█"
SPARKLINE_WIDTH = 24
SYNTAX_THEME = "one-dark"
REMOVED_LINE = "on #4b1d22"
ADDED_LINE = "on #1b4227"
DETAIL_LABEL_WIDTH = 10
DETAIL_CONTEXT = 12
SNIPPET_CONTEXT = 3
# Survivors are worth a toast, but a burst of them should not bury the screen.
TOAST_SECONDS = 2.0
HIGH_SCORE = 80.0
LOW_SCORE = 60.0
WORKER_POLL_SECONDS = 0.1
EQUIVALENT_HINT = f"equivalent? list it under [ignore] mutants in {CONFIG_FILE}"
STATE_GLYPHS = {
    MutantState.KILLED: "✔",
    MutantState.KILLED_BY_SANITIZER: "✔",
    MutantState.TIMEOUT: "✔",
    MutantState.SURVIVED: "✗",
    MutantState.SKIPPED_UNEVALUATED: "·",
    MutantState.COMPILE_ERROR: "⊘",
    MutantState.IGNORED: "○",
    RUNNING: "▶",
    PENDING: "·",
}
STATE_STYLES = {
    MutantState.KILLED: "green",
    MutantState.KILLED_BY_SANITIZER: "green",
    MutantState.TIMEOUT: "green",
    MutantState.SURVIVED: "bold red",
    MutantState.SKIPPED_UNEVALUATED: "dim",
    MutantState.COMPILE_ERROR: "dim",
    MutantState.IGNORED: "dim",
    RUNNING: "bold cyan",
    PENDING: "dim",
}
# Narrow lists use these labels; the details give the full state.
SHORT_STATES = {
    MutantState.KILLED_BY_SANITIZER: "sanitizer",
    MutantState.COMPILE_ERROR: "no build",
    MutantState.SKIPPED_UNEVALUATED: "skipped",
}
STATE_COLUMN_WIDTH = 2 + max(len(SHORT_STATES.get(state, state)) for state in [*MutantState, RUNNING, PENDING])
MAP_COLORS = {"survived": "red", "detected": "green", "running": "bold cyan", "other": "grey50", "pending": "grey23"}
VIEWS = ("run", "survivors", "mutants", "stats", "log")
MUTANT_COLUMNS = (("state", "state"), ("where", "location"), ("op", "op"), ("change", "change"), ("time", "time"))
TALLY_COLUMNS = (("name", "file"), ("score", "score"), ("bar", ""), ("mutants", "mutants"), ("survived", "survived"))
# Each table's columns, as (key, label), and its title.
TABLES = {
    "queue": ("Mutants", MUTANT_COLUMNS),
    "survivor-table": ("Survivors", (("where", "location"), ("op", "op"), ("change", "change"), ("kind", "survivor"))),
    "survivor-files": ("Files", (("name", "file"), ("score", "score"), ("bar", ""), ("survived", "survived"))),
    "mutant-table": ("Mutants", MUTANT_COLUMNS),
    "stats-files": ("Files", TALLY_COLUMNS),
    "stats-operators": ("Operators", (("name", "operator"), *TALLY_COLUMNS[1:])),
    "stats-tests": (
        "Tests",
        (
            ("name", "test"),
            *TALLY_COLUMNS[1:3],
            ("mutants", "runs"),
            ("detected", "detected"),
            ("survived", "survived"),
        ),
    ),
    "stats-slowest": (
        "Slowest mutants",
        (("time", "time"), ("state", "state"), ("where", "location"), ("change", "change")),
    ),
}
PANEL_TITLES = {
    "#safety-box": "Safety nets",
    "#findings-box": "Findings",
    "#build": "Build log",
    "#transcript": "Log",
    "#code": "Mutant",
}


def score_style(score: float | None) -> str:
    if score is None:
        return "dim"
    percent = score * 100
    if percent >= HIGH_SCORE:
        return "bold green"
    return "bold yellow" if percent >= LOW_SCORE else "bold red"


def score_text(score: float | None) -> Text:
    return Text(format_score(score), style=score_style(score))


def state_text(state: str, glyph_only: bool = False, short: bool = False) -> Text:
    glyph = STATE_GLYPHS.get(state, "·")
    label = SHORT_STATES.get(state, state) if short else state
    return Text(glyph if glyph_only else f"{glyph} {label}", style=STATE_STYLES.get(state, "dim"))


def sparkline(values: list[float], width: int = SPARKLINE_WIDTH) -> str:
    """The last `width` values between 0 and 1, one block each, taller for higher values."""
    return "".join(SPARKS[min(len(SPARKS) - 1, int(value * len(SPARKS)))] for value in values[-width:])


def bar(done: int, total: int, width: int = 12) -> Text:
    filled = 0 if total <= 0 else min(width, round(done * width / total))
    return Text.assemble(("█" * filled, "green"), ("█" * (width - filled), "red" if total else "grey23"))


def location(mutant: Mutant, short: bool = False) -> str:
    """Where `mutant` is; `short` keeps only the file name, for lists whose details give the whole path."""
    file = mutant.file.rpartition("/")[2] if short else mutant.file
    return f"{file}:{mutant.line}:{mutant.column}"


def table(table_id: str, classes: str = "panel") -> DataTable:
    """A row-selecting table whose cells keep their own colors on the cursor row."""
    return DataTable(
        id=table_id, classes=classes, cursor_type="row", zebra_stripes=True, cursor_foreground_priority="renderable"
    )


def change(mutant: Mutant) -> Text:
    return Text.assemble((mutant.original, "red"), " → ", (mutant.replacement, "green"))


def map_category(state: str) -> str:
    if state == RUNNING:
        return "running"
    if state == PENDING:
        return "pending"
    if state == MutantState.SURVIVED:
        return "survived"
    if state in (MutantState.KILLED, MutantState.KILLED_BY_SANITIZER, MutantState.TIMEOUT):
        return "detected"
    return "other"


@functools.lru_cache(maxsize=32)
def highlighted_lines(source: str) -> list[Text]:
    """`source` with C++ syntax highlighting, one Text per line, tabs expanded."""
    syntax = Syntax("", "cpp", theme=SYNTAX_THEME, background_color="default")
    return syntax.highlight(source.expandtabs(TAB_WIDTH)).split("\n", allow_blank=True)


def diff_view(source: str, mutant: Mutant, context: int) -> Text:
    """The lines around `mutant` with syntax highlighting, its line shown before (`-`) and after (`+`) the mutation."""
    lines = highlighted_lines(source)
    mutated = highlighted_lines(apply_mutant(source, mutant))[mutant.line - 1]
    first = max(1, mutant.line - context)
    # The highlighter ends with an empty line of its own, so count the source's lines.
    last = min(len(source.splitlines()), mutant.line + context)
    width = len(str(last))
    offset = token_offset(source, mutant)
    rows: list[Text] = []
    for number in range(first, last + 1):
        if number != mutant.line:
            rows.append(Text.assemble((f"  {number:>{width}} │ ", "grey42"), lines[number - 1]))
            continue
        for marker, text, token, style in (
            ("-", lines[number - 1], mutant.original, REMOVED_LINE),
            ("+", mutated, mutant.replacement, ADDED_LINE),
        ):
            line = text.copy()
            line.stylize(style)
            line.stylize("bold reverse", offset, offset + len(token))
            color = "red" if marker == "-" else "green"
            rows.append(Text.assemble((f"{marker} {number:>{width}} │ ", f"bold {color} {style}"), line))
    view = Text("\n").join(rows)
    view.no_wrap = True
    view.overflow = "ellipsis"
    return view


def detail_row(table: Table, label: str, value: str | Text) -> None:
    table.add_row(Text(f"{label:<{DETAIL_LABEL_WIDTH}}", style="dim"), value)


def mutant_details(state: LiveState, entry: CombinedResult) -> RenderableType:
    """Everything known about `entry`: its id, function, coverage, which tests it survived or died to, and its source."""
    mutant = entry.mutant
    context = state.contexts[mutant.id]
    facts = Table.grid(padding=(0, 1))
    facts.add_column(min_width=DETAIL_LABEL_WIDTH, no_wrap=True)
    facts.add_column()
    survived = entry.state is MutantState.SURVIVED
    mutant_id = Text.assemble((mutant.id, "bold"), ("  " + EQUIVALENT_HINT, "dim")) if survived else mutant.id
    detail_row(facts, "id", mutant_id)
    detail_row(facts, "state", state_text(entry.state))
    detail_row(facts, "operator", Text.assemble(f"{mutant.operator}  ", change(mutant)))
    detail_row(facts, "function", entry.function or "unknown")
    detail_row(facts, "coverage", execution_summary(mutant.line, context.executions))
    hiding = state.bug_hiding_nets()
    if survived and hiding:
        names = ", ".join(failure.net for failure in hiding)
        caution = f"{names} off: this mutant may survive only because of that; 4 Stats explains"
        detail_row(facts, "caution", Text(caution, style="bold red"))
    if survived:
        kind = "" if entry.survivor is None else f"{entry.survivor} "
        detail_row(facts, str(MutantState.SURVIVED), f"{kind}under {', '.join(entry.covered_by)}")
        detail_row(facts, "hint", Text(hint(entry), style="yellow"))
    elif entry.killed_by:
        detail_row(facts, "killed by", ", ".join(entry.killed_by))
    duration = state.durations.get(mutant.id)
    if duration is not None:
        detail_row(facts, "time", f"{duration:.2f}s")
    return Group(facts, Text(""), diff_view(context.source, mutant, DETAIL_CONTEXT))


def safety_badge(state: LiveState) -> Text:
    """How many failing safety nets the run went on without; red when one of them lets broken mutants survive."""
    relaxed = state.relaxed_nets or []
    if not relaxed:
        return Text("")
    style = "bold white on red" if state.bug_hiding_nets() else "bold black on yellow"
    return Text(f" ! {plural(len(relaxed), 'safety net')} off ", style=style)


def relaxed_net_view(failure: RelaxedSafetyNet) -> RenderableType:
    """One relaxed net: how the configuration let it pass, where it fails, why that matters and how to fix it."""
    went_on = "with a warning" if failure.level is Level.WARN else "without a warning"
    heading = Text.assemble(
        ("✗ ", "bold red" if failure.hides_bugs else "bold yellow"),
        (str(failure.net), "bold"),
        f"  set to {failure.level}, so the run went on {went_on}",
    )
    if failure.hides_bugs:
        heading.append("; by default it stops the run", style="red")
    facts = Table.grid(padding=(0, 1))
    facts.add_column(min_width=DETAIL_LABEL_WIDTH, no_wrap=True)
    facts.add_column()
    detail_row(facts, "fails", failure.where.removeprefix("fails "))
    detail_row(facts, "why", Text(failure.why, style="yellow" if failure.hides_bugs else ""))
    detail_row(facts, "fix", Text(failure.fix, style="cyan"))
    return Group(heading, facts)


def safety_report(state: LiveState) -> RenderableType:
    """The safety nets the configuration let fail, what that means for the results, and the nets that hold."""
    if state.relaxed_nets is None:
        return Text("strain checks the safety nets while it prepares the run", style="dim")
    holding = Text.assemble(("✔ hold: ", "green"), ", ".join(state.holding_nets) or "none")
    if not state.relaxed_nets:
        return Text.assemble(("✔ every safety net holds: ", "bold green"), ", ".join(state.holding_nets))
    hiding = state.bug_hiding_nets()
    if hiding:
        names = ", ".join(failure.net for failure in hiding)
        summary = Text(
            f"{names} {'is' if len(hiding) == 1 else 'are'} off, so a mutant that breaks the code can still pass the "
            "tests: some survivors may point at the build rather than at a weak test. Fix the build before writing "
            "tests for them.",
            style="bold red",
        )
    else:
        summary = Text(
            "These nets cost coverage precision or build speed, not correctness: survivors still point at weak tests.",
            style="yellow",
        )
    views: list[RenderableType] = [summary]
    for failure in state.relaxed_nets:
        views += [Text(""), relaxed_net_view(failure)]
    return Group(*views, Text(""), holding)


def run_badge(state: LiveState, paused: bool) -> Text:
    """What the run is doing, as a colored badge: its phase with a spinner, paused, stopping or finished."""
    if state.done:
        return Text(" ✔ finished ", style="bold black on green")
    if state.stopping:
        return Text(" ■ stopping… ", style="bold white on red")
    if paused:
        return Text(" ‖ paused " if state.waiting else " ‖ pausing after this mutant ", style="bold black on yellow")
    spinner = SPINNER[int(state.clock() * SPINNER_FRAMES_PER_SECOND) % len(SPINNER)]
    return Text(f" {spinner} {state.phase() or 'preparing'} ", style="bold black on cyan")


def run_position(state: LiveState) -> Text:
    where = Text.assemble(
        (" ◆ strain ", "bold black on dark_cyan"), "  ", (state.test_name if state.done else state.test, "bold")
    )
    if state.current is not None and not state.done:
        where.append(f"  ·  mutant {state.current + 1}/{len(state.queue)}")
    return where


def run_timing(state: LiveState) -> Text:
    timing = Text(f"elapsed {clock_time(state.elapsed())}", style="grey62")
    remaining = state.remaining_seconds()
    if remaining is not None:
        timing.append(f"  ·  eta {clock_time(remaining)}")
    mean = state.mean_duration()
    if mean is not None:
        timing.append(f"  ·  {mean:.1f}s/mutant")
    return timing


def run_scores(state: LiveState) -> Text:
    """The score and its history, then the totals over every test, counting each mutant once.

    The score comes first, so a narrow terminal cuts the counts before it.
    """
    scores = Text.assemble("score ", score_text(state.score()))
    if len(state.score_history) > 1:
        scores.append(f" {sparkline(state.score_history)}", style="cyan")
    for kind, count in state.counts().items():
        scores.append("   ")
        scores.append_text(state_text(kind, glyph_only=True))
        scores.append(f" {kind} {count}", style=STATE_STYLES[kind])
    return scores


class RunHeader(Static):
    """The run's position, its state, the score and its history, and how long the run has taken and will take.

    Clicking the safety net badge shows why the nets it counts matter.
    """

    def __init__(self) -> None:
        super().__init__()
        self.safety_columns = range(0)

    def render_state(self, state: LiveState, paused: bool) -> RenderableType:
        safety = safety_badge(state)
        badge = run_badge(state, paused)
        # The badges sit at the right edge, inside one column of padding.
        end = self.size.width - 1 - len(badge) - (1 if safety else 0)
        self.safety_columns = range(end - len(safety), end)
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        grid.add_column(justify="right", no_wrap=True)
        grid.add_row(run_position(state), Text.assemble(safety, " " if safety else "", badge))
        grid.add_row(run_scores(state), run_timing(state))
        return grid

    def on_click(self, event: events.Click) -> None:
        if event.y == 0 and event.x in self.safety_columns:
            self.app.show_safety_nets()  # type: ignore[attr-defined]


class MutationMap(Widget):
    """One cell per mutant (or per group of them on a long queue), colored by its result: the run at a glance.

    Hovering names the mutant under the pointer; clicking shows it in the queue.
    """

    DEFAULT_CSS = "MutationMap { height: 1; }"

    def __init__(self, state: LiveState, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.state = state

    def label_width(self) -> int:
        return len(f" {len(self.state.queue)}/{len(self.state.queue)}") + 1

    def cells(self) -> int:
        return max(1, self.size.width - self.label_width())

    def index_at(self, x: int) -> int | None:
        count = len(self.state.queue)
        if count == 0 or x >= self.cells():
            return None
        return min(count - 1, x * count // self.cells())

    def render(self) -> RenderableType:
        with self.state.lock:
            queue = self.state.queue
            width = self.cells()
            text = Text()
            if not queue:
                text.append("░" * width, style=MAP_COLORS["pending"])
            for cell in range(width if queue else 0):
                first = cell * len(queue) // width
                last = max(first + 1, (cell + 1) * len(queue) // width)
                categories = {map_category(entry.state) for entry in queue[first:last]}
                worst = next(
                    name for name in ("running", "survived", "detected", "other", "pending") if name in categories
                )
                text.append("░" if worst == "pending" else "█", style=MAP_COLORS[worst])
            text.append(f" {self.state.done_count()}/{len(queue)}", style="bold")
            return text

    def on_mouse_move(self, event: events.MouseMove) -> None:
        index = self.index_at(event.x)
        with self.state.lock:
            if index is None:
                self.tooltip = None
            else:
                entry = self.state.queue[index]
                self.tooltip = f"{index + 1}. {entry.state}: {describe_mutant(entry.mutant)}"

    def on_click(self, event: events.Click) -> None:
        index = self.index_at(event.x)
        if index is not None:
            self.app.show_queue_entry(index)  # type: ignore[attr-defined]


class CodeView(Static):
    """The mutant under the queue cursor, as a highlighted diff that grows with the panel."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.shown: tuple[str, Mutant] | None = None

    def show(self, source: str | None, mutant: Mutant | None) -> None:
        shown = None if source is None or mutant is None else (source, mutant)
        if shown == self.shown:
            return
        self.shown = shown
        self.redraw()

    def redraw(self) -> None:
        if self.shown is None:
            self.update(Text("waiting for the first mutant", style="dim"))
            return
        source, mutant = self.shown
        context = max(SNIPPET_CONTEXT, (self.content_size.height - 2) // 2)
        self.update(diff_view(source, mutant, context))

    def on_resize(self, event: events.Resize) -> None:
        self.redraw()


class StrainApp(App[None]):
    TITLE = "strain"
    CSS = """
    Screen { layout: vertical; }
    RunHeader { height: 2; padding: 0 1; }
    MutationMap { margin: 0 1; }
    TabbedContent { height: 1fr; }
    TabPane { padding: 0; }
    .panel { border: round $panel-lighten-2; border-title-color: $accent; border-title-style: bold; }
    .panel:focus-within { border: round $accent; }
    #run-left { width: 5fr; }
    #run-right { width: 4fr; }
    #code { height: 3fr; padding: 0 1; text-wrap: nowrap; text-overflow: ellipsis; }
    #build { height: 2fr; }
    /* The time column comes last, so a narrow terminal loses it rather than scrolling sideways. */
    #queue { height: 3fr; overflow-x: hidden; }
    #findings-box { height: 2fr; }
    #findings { padding: 0 1; }
    .list { width: 2fr; }
    .list DataTable { height: 1fr; }
    .list #survivor-files { height: auto; max-height: 40%; }
    .details { width: 3fr; padding: 0 1; }
    #filters { height: 2; }
    #safety-box { height: auto; max-height: 60%; padding: 0 1; }
    #stats-tables { height: 1fr; }
    .stats-column { width: 1fr; }
    .stats-column DataTable { height: auto; max-height: 50%; }
    DataTable { scrollbar-size-vertical: 1; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+c", "stop", "Stop / quit", priority=True),
        Binding("q", "quit_when_done", "Quit"),
        Binding("p", "pause", "Pause"),
        Binding("f", "follow", "Follow"),
        Binding("?", "show_help_panel", "Keys"),
        *(
            Binding(str(number), f"show_view('{view}')", view.capitalize(), show=False)
            for number, view in enumerate(VIEWS, start=1)
        ),
        Binding("tab", "next_view", "Next view", show=False, priority=True),
        Binding("shift+tab", "previous_view", "Previous view", show=False, priority=True),
    ]

    def __init__(self, console: "TuiConsole") -> None:
        super().__init__()
        self.runner_console = console
        self.state = console.live
        self.following = True
        self.seen_version = -1
        self.seen_results = -1
        self.seen_plan = -1
        self.seen_build = -1
        self.build_lines_shown = 0
        self.transcript_shown = 0
        self.queue_rows: list[tuple[str, float | None]] = []
        self.survivors_seen: set[str] = set()
        self.pending_toasts: list[str] = []
        self.last_toast = 0.0
        self.announced_finish = False
        # The safety net report last shown: None before the check, then the list the console reported.
        self.shown_safety: list[RelaxedSafetyNet] | None = None
        self.state_filter: str | None = None
        self.dirty_tables: set[str] = set()

    def compose(self) -> ComposeResult:
        yield RunHeader()
        yield MutationMap(self.state)
        with TabbedContent(initial="run"):
            yield from self.compose_run()
            yield from self.compose_browsers()
            yield from self.compose_stats()
            with TabPane("5 Log", id="log"):
                yield RichLog(id="transcript", classes="panel", wrap=True, markup=False)
        yield Footer()

    def compose_run(self) -> ComposeResult:
        with TabPane("1 Run", id="run"), Horizontal():
            with Vertical(id="run-left"):
                yield CodeView(id="code", classes="panel")
                yield RichLog(id="build", classes="panel", max_lines=2000, wrap=True, markup=False, min_width=20)
            with Vertical(id="run-right"):
                yield table("queue")
                with VerticalScroll(id="findings-box", classes="panel"):
                    yield Static(id="findings")

    def compose_browsers(self) -> ComposeResult:
        with TabPane("2 Survivors", id="survivors"), Horizontal():
            with Vertical(classes="list"):
                yield table("survivor-table")
                yield table("survivor-files")
            with VerticalScroll(id="survivor-box", classes="panel details"):
                yield Static(id="survivor-detail")
        with TabPane("3 Mutants", id="mutants"), Horizontal():
            with Vertical(classes="list"):
                yield Tabs(Tab("all", id="filter-all"), id="filters")
                yield table("mutant-table")
            with VerticalScroll(id="mutant-box", classes="panel details"):
                yield Static(id="mutant-detail")

    def compose_stats(self) -> ComposeResult:
        with TabPane("4 Stats", id="stats"), Vertical():
            with VerticalScroll(id="safety-box", classes="panel"):
                yield Static(id="safety")
            with Horizontal(id="stats-tables"):
                with Vertical(classes="stats-column"):
                    yield table("stats-files")
                    yield table("stats-operators")
                with Vertical(classes="stats-column"):
                    yield table("stats-tests")
                    yield table("stats-slowest")

    def on_mount(self) -> None:
        for table_id, (title, columns) in TABLES.items():
            data_table = self.query_one(f"#{table_id}", DataTable)
            data_table.border_title = title
            for key, label in columns:
                # A fixed width, so a longer state arriving later never shifts the other columns.
                data_table.add_column(label, key=key, width=STATE_COLUMN_WIDTH if key == "state" else None)
        for selector, title in PANEL_TITLES.items():
            self.query_one(selector).border_title = title
        self.set_interval(REFRESH_SECONDS, self.refresh_from_state)
        self.refresh_from_state()
        # Only now can the runner's thread rely on the app to be running.
        self.runner_console.start_worker()

    # Keeping the widgets up to date.

    def refresh_from_state(self) -> None:
        try:
            self.redraw()
        except NoMatches:
            # The app is closing and its widgets are already gone; there is nothing left to draw.
            return
        # The run failed, or the user asked to stop it, and the runner has wound down: nothing left to show.
        if self.runner_console.worker_failed() or (self.state.stopping and self.runner_console.worker_finished()):
            self.exit()

    def redraw(self) -> None:
        with self.state.lock:
            self.query_one(RunHeader).update(
                self.query_one(RunHeader).render_state(self.state, self.runner_console.paused)
            )
            self.query_one(MutationMap).refresh()
            self.refresh_code_title()
            if self.state.version != self.seen_version:
                self.seen_version = self.state.version
                self.refresh_queue()
                self.refresh_build_log()
                self.refresh_transcript()
                self.refresh_safety_nets()
                if self.state.results_version != self.seen_results:
                    self.seen_results = self.state.results_version
                    self.refresh_results()
                self.refresh_code()
            self.flush_toasts()

    def refresh_queue(self) -> None:
        table = self.query_one("#queue", DataTable)
        queue = self.state.queue
        if self.state.plan_version != self.seen_plan:
            self.seen_plan = self.state.plan_version
            table.clear()
            self.queue_rows = []
            for index, entry in enumerate(queue):
                table.add_row(*self.queue_cells(entry), key=str(index))
                self.queue_rows.append((entry.state, entry.duration))
            self.following = True
        for index, entry in enumerate(queue):
            if self.queue_rows[index] != (entry.state, entry.duration):
                self.queue_rows[index] = (entry.state, entry.duration)
                cells = self.queue_cells(entry)
                table.update_cell(str(index), "state", cells[0])
                table.update_cell(str(index), "time", cells[-1], update_width=True)
        done = self.state.done_count()
        table.border_title = f"Mutants {done}/{len(queue)}" if queue else "Mutants"
        table.border_subtitle = "" if self.following else "browsing · f follows the run"
        if self.following and self.state.current is not None and table.cursor_row != self.state.current:
            table.move_cursor(row=self.state.current)

    def queue_cells(self, entry: QueueEntry) -> tuple[Text, ...]:
        time = Text("" if entry.duration is None else f"{entry.duration:.1f}s", style="grey62", justify="right")
        return (
            state_text(entry.state, short=True),
            Text(location(entry.mutant, short=True)),
            Text(entry.mutant.operator, style="magenta"),
            change(entry.mutant),
            time,
        )

    def cursor_entry(self) -> tuple[int, QueueEntry] | None:
        table = self.query_one("#queue", DataTable)
        queue = self.state.queue
        if not queue:
            return None
        index = self.state.current if self.following and self.state.current is not None else table.cursor_row
        index = max(0, min(index, len(queue) - 1))
        return index, queue[index]

    def refresh_code(self) -> None:
        shown = self.cursor_entry()
        code = self.query_one(CodeView)
        if shown is None:
            code.show(None, None)
            return
        _, entry = shown
        code.show(self.state.sources.get(entry.mutant.file), entry.mutant)

    def refresh_code_title(self) -> None:
        code = self.query_one(CodeView)
        shown = self.cursor_entry()
        if shown is None:
            return
        index, entry = shown
        code.border_title = f"Mutant {index + 1}: {describe_mutant(entry.mutant)}"
        elapsed = self.state.current_elapsed()
        if index == self.state.current and elapsed is not None:
            code.border_subtitle = f"{self.state.phase() or RUNNING} {elapsed:.1f}s"
        else:
            code.border_subtitle = str(entry.state)

    def refresh_build_log(self) -> None:
        log = self.query_one("#build", RichLog)
        if self.state.build_version != self.seen_build:
            self.seen_build = self.state.build_version
            log.clear()
            log.border_title = f"Build log: {self.state.build_command}"
            self.build_lines_shown = self.state.build_lines_seen - len(self.state.build_log)
        new = self.state.build_lines_seen - self.build_lines_shown
        if new > 0:
            for line in list(self.state.build_log)[-min(new, len(self.state.build_log)) :]:
                log.write(Text.from_ansi(line, style="grey70"))
            self.build_lines_shown = self.state.build_lines_seen

    def refresh_transcript(self) -> None:
        log = self.query_one("#transcript", RichLog)
        for line in self.state.log[self.transcript_shown :]:
            log.write(Text.from_ansi(line))
        self.transcript_shown = len(self.state.log)

    def refresh_results(self) -> None:
        findings = self.state.findings()
        box = self.query_one("#findings-box")
        box.border_title = f"Findings {len(findings)}"
        bullets = [Text.assemble((" • ", "yellow"), item) for item in findings]
        self.query_one("#findings", Static).update(
            Text("\n").join(bullets) if bullets else Text("no findings yet", style="dim")
        )
        survivors = self.state.survivors()
        tabs = self.query_one(TabbedContent)
        tabs.get_tab("survivors").label = f"2 Survivors {len(survivors)}"
        tabs.get_tab("mutants").label = f"3 Mutants {len(self.state.combined())}"
        for entry in survivors:
            if entry.mutant.id not in self.survivors_seen:
                self.survivors_seen.add(entry.mutant.id)
                self.pending_toasts.append(describe_mutant(entry.mutant))
        self.refresh_filters()
        self.dirty_tables = {"survivors", "mutants", "stats"}
        self.refresh_active_tables()

    def refresh_filters(self) -> None:
        filters = self.query_one("#filters", Tabs)
        counts = self.state.counts()
        filters.query_one("#filter-all", Tab).label = f"all {sum(counts.values())}"
        for state, count in counts.items():
            tab_id = f"filter-{state}"
            existing = filters.query(f"#{tab_id}")
            label = f"{state} {count}"
            if existing:
                existing.first(Tab).label = label
            else:
                filters.add_tab(Tab(label, id=tab_id))

    def refresh_active_tables(self) -> None:
        active = self.query_one(TabbedContent).active
        if active in self.dirty_tables:
            self.dirty_tables.discard(active)
            {"survivors": self.refresh_survivors, "mutants": self.refresh_mutants, "stats": self.refresh_stats}[
                active
            ]()

    def fill_browser(
        self,
        table: DataTable,
        entries: list[CombinedResult],
        cells: Callable[[CombinedResult], tuple[RenderableType, ...]],
    ) -> None:
        """Refills `table` with `entries`, keeping the cursor on the same mutant."""
        selected = None
        if table.row_count and 0 <= table.cursor_row < table.row_count:
            selected = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value
        table.clear()
        for entry in entries:
            table.add_row(*cells(entry), key=entry.mutant.id)
        keys = [entry.mutant.id for entry in entries]
        if selected in keys:
            table.move_cursor(row=keys.index(selected), animate=False)

    def refresh_survivors(self) -> None:
        table = self.query_one("#survivor-table", DataTable)
        survivors = self.state.survivors()
        table.border_title = f"Survivors {len(survivors)}"
        self.fill_browser(
            table,
            survivors,
            lambda entry: (
                Text(location(entry.mutant, short=True)),
                Text(entry.mutant.operator, style="magenta"),
                change(entry.mutant),
                Text(str(entry.survivor or ""), style="yellow"),
            ),
        )
        self.show_selected("survivor-table", "survivor-detail", survivors)
        files = self.query_one("#survivor-files", DataTable)
        files.clear()
        for row in self.state.file_scores():
            files.add_row(
                Text(row.file.rpartition("/")[2]),
                score_text(row.score),
                bar(row.detected, row.detected + row.survived, 8),
                Text(str(row.survived), style="bold red" if row.survived else "dim", justify="right"),
            )

    def fill_file_scores(self, files: DataTable) -> None:
        files.clear()
        for row in self.state.file_scores():
            files.add_row(*self.tally_row(Tally(row.file, row.mutants, row.detected, row.survived)))

    def filtered_mutants(self) -> list[CombinedResult]:
        combined = self.state.combined()
        return (
            combined if self.state_filter is None else [entry for entry in combined if entry.state == self.state_filter]
        )

    def refresh_mutants(self) -> None:
        table = self.query_one("#mutant-table", DataTable)
        entries = self.filtered_mutants()
        table.border_title = f"Mutants {len(entries)}" + (
            "" if self.state_filter is None else f" · {self.state_filter}"
        )
        self.fill_browser(
            table,
            entries,
            lambda entry: (
                state_text(entry.state, short=True),
                Text(location(entry.mutant, short=True)),
                Text(entry.mutant.operator, style="magenta"),
                change(entry.mutant),
                Text(
                    f"{self.state.durations[entry.mutant.id]:.1f}s" if entry.mutant.id in self.state.durations else "",
                    style="grey62",
                    justify="right",
                ),
            ),
        )
        self.show_selected("mutant-table", "mutant-detail", entries)

    def tally_row(self, tally: Tally, *counts: Text) -> tuple[RenderableType, ...]:
        return (
            Text(tally.name),
            score_text(tally.score),
            bar(tally.detected, tally.detected + tally.survived, 8),
            Text(str(tally.mutants), justify="right"),
            *counts,
            Text(str(tally.survived), style="bold red" if tally.survived else "dim", justify="right"),
        )

    def refresh_stats(self) -> None:
        self.fill_file_scores(self.query_one("#stats-files", DataTable))
        operators = self.query_one("#stats-operators", DataTable)
        operators.clear()
        for tally in self.state.operator_tallies():
            operators.add_row(*self.tally_row(tally))
        tests = self.query_one("#stats-tests", DataTable)
        tests.clear()
        for tally in self.state.test_tallies():
            tests.add_row(*self.tally_row(tally, Text(str(tally.detected), style="green", justify="right")))
        slowest = self.query_one("#stats-slowest", DataTable)
        slowest.clear()
        for duration, entry in self.state.slowest():
            slowest.add_row(
                Text(f"{duration:.2f}s", style="yellow", justify="right"),
                state_text(entry.state, short=True),
                location(entry.mutant, short=True),
                change(entry.mutant),
            )

    def show_selected(self, table_id: str, detail_id: str, entries: list[CombinedResult]) -> None:
        table = self.query_one(f"#{table_id}", DataTable)
        detail = self.query_one(f"#{detail_id}", Static)
        box = detail.parent
        if not entries:
            detail.update(
                Text("no surviving mutants" if table_id == "survivor-table" else "no mutants yet", style="dim")
            )
            if isinstance(box, Widget):
                box.border_title = "Details"
            return
        entry = entries[max(0, min(table.cursor_row, len(entries) - 1))]
        detail.update(mutant_details(self.state, entry))
        if isinstance(box, Widget):
            box.border_title = describe_mutant(entry.mutant)

    def flush_toasts(self) -> None:
        if self.state.done and not self.announced_finish:
            self.announced_finish = True
            self.pending_toasts.clear()
            survivors = len(self.state.survivors())
            self.notify(
                f"score {format_score(self.state.score())}, {plural(survivors, 'survivor')} to look at",
                title="Run finished",
                severity="information" if survivors == 0 else "warning",
                timeout=8,
            )
            return
        if not self.pending_toasts or self.state.clock() - self.last_toast < TOAST_SECONDS:
            return
        self.last_toast = self.state.clock()
        if len(self.pending_toasts) == 1:
            self.notify(self.pending_toasts[0], title="✗ Survivor", severity="warning", timeout=4)
        else:
            self.notify(f"{len(self.pending_toasts)} new survivors", title="✗ Survivors", severity="warning", timeout=4)
        self.pending_toasts.clear()

    # Events.

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        table_id = event.data_table.id
        with self.state.lock:
            if table_id == "queue":
                if event.cursor_row != self.state.current:
                    self.following = False
                    event.data_table.border_subtitle = "browsing · f follows the run"
                self.refresh_code()
                self.refresh_code_title()
            elif table_id == "survivor-table":
                self.show_selected("survivor-table", "survivor-detail", self.state.survivors())
            elif table_id == "mutant-table":
                self.show_selected("mutant-table", "mutant-detail", self.filtered_mutants())

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter, or a click on the highlighted row, opens a queue entry in the mutant browser."""
        if event.data_table.id != "queue":
            return
        with self.state.lock:
            if event.cursor_row >= len(self.state.queue):
                return
            mutant_id = self.state.queue[event.cursor_row].mutant.id
            if mutant_id not in self.state.contexts:
                self.notify("this mutant has no result yet", timeout=2)
                return
        self.open_mutant(mutant_id)

    def open_mutant(self, mutant_id: str) -> None:
        self.state_filter = None
        self.query_one("#filters", Tabs).active = "filter-all"
        self.query_one(TabbedContent).active = "mutants"
        with self.state.lock:
            self.refresh_mutants()
            keys = [entry.mutant.id for entry in self.filtered_mutants()]
            if mutant_id in keys:
                self.query_one("#mutant-table", DataTable).move_cursor(row=keys.index(mutant_id))
        self.query_one("#mutant-table", DataTable).focus()

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        if event.tabs.id != "filters" or event.tab is None:
            return
        name = (event.tab.id or "").removeprefix("filter-")
        self.state_filter = None if name == "all" else name
        with self.state.lock:
            self.refresh_mutants()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        with self.state.lock:
            self.refresh_active_tables()
        focus = {
            "run": "#queue",
            "survivors": "#survivor-table",
            "mutants": "#mutant-table",
            "stats": "#stats-files",
            "log": "#transcript",
        }
        self.query_one(focus[event.pane.id or "run"]).focus()

    def refresh_safety_nets(self) -> None:
        relaxed = self.state.relaxed_nets
        if relaxed is self.shown_safety:
            return
        self.shown_safety = relaxed
        self.query_one("#safety", Static).update(safety_report(self.state))
        box = self.query_one("#safety-box")
        box.border_title = "Safety nets" if not relaxed else f"Safety nets: {len(relaxed)} off"
        if relaxed:
            hiding = self.state.bug_hiding_nets()
            names = ", ".join(failure.net for failure in relaxed)
            self.notify(
                f"{names} off; 4 Stats explains why it matters",
                title="! Safety nets",
                severity="error" if hiding else "warning",
                timeout=6,
            )

    def show_safety_nets(self) -> None:
        self.query_one(TabbedContent).active = "stats"
        self.query_one("#safety-box").scroll_home(animate=False)

    def show_queue_entry(self, index: int) -> None:
        self.query_one(TabbedContent).active = "run"
        self.following = False
        table = self.query_one("#queue", DataTable)
        table.move_cursor(row=index)
        table.focus()

    # Actions.

    def action_show_view(self, view: str) -> None:
        self.query_one(TabbedContent).active = view

    def action_next_view(self) -> None:
        self.cycle_view(1)

    def action_previous_view(self) -> None:
        self.cycle_view(-1)

    def cycle_view(self, step: int) -> None:
        tabs = self.query_one(TabbedContent)
        tabs.active = VIEWS[(VIEWS.index(tabs.active) + step) % len(VIEWS)]

    def action_follow(self) -> None:
        if self.query_one(TabbedContent).active == "mutants":
            filters = self.query_one("#filters", Tabs)
            filters.action_next_tab()
            return
        self.following = True
        with self.state.lock:
            self.refresh_queue()
            self.refresh_code()

    def action_pause(self) -> None:
        if self.state.done:
            return
        self.runner_console.set_paused(not self.runner_console.paused)
        self.notify("pausing after this mutant" if self.runner_console.paused else "resumed", timeout=2)

    def action_quit_when_done(self) -> None:
        if self.state.done:
            self.exit()
        else:
            self.notify("the run is still going: Ctrl-C stops it and restores the sources", timeout=3)

    def action_stop(self) -> None:
        if self.state.done or self.runner_console.worker_finished():
            self.exit()
            return
        if self.state.stopping:
            self.notify("still stopping: waiting for the current build or test to end", timeout=3)
            return
        self.runner_console.request_stop()
        self.notify("stopping after the current step; the sources will be restored", title="Stopping", severity="error")


class TuiConsole(Console):
    """Collects the run's progress for the TUI, then prints the plain transcript once the TUI has closed."""

    def __init__(self, out: TextIO, err: TextIO, headless: bool = False) -> None:
        super().__init__(out, err, UiMode.LIVE)
        self.live = LiveState()
        self.headless = headless
        self.active = True
        self.paused = False
        self.transcript: list[str] = []
        self.warnings: list[str] = []
        self.resume = threading.Event()
        self.resume.set()
        self.stop_requested = threading.Event()
        self.stop_raised = False
        self.worker: threading.Thread | None = None
        self.worker_started = False
        self.worker_error: BaseException | None = None
        self.app: StrainApp | None = None

    # Running the TUI.

    def run(self, work: Callable[[], int]) -> int:
        """Runs `work` on a worker thread while the TUI owns the terminal, and returns what `work` returns."""
        result: list[int] = []

        def target() -> None:
            try:
                result.append(work())
            except BaseException as error:  # noqa: BLE001 - re-raised on the main thread below
                self.worker_error = error

        self.app = StrainApp(self)
        self.worker = threading.Thread(target=target, name="strain-runner", daemon=True)
        previous = signal.signal(signal.SIGTERM, self._stop_on_signal)
        try:
            self.app.run(headless=self.headless)
        finally:
            signal.signal(signal.SIGTERM, previous)
            self.close()
        if self.worker_error is not None:
            raise self.worker_error
        if not result:
            raise RuntimeError("the live TUI closed before the run started; try --ui auto")
        return result[0]

    def start_worker(self) -> None:
        """Starts the run, once the app is up; called from the app's first mount."""
        if self.worker is not None and not self.worker_started:
            self.worker_started = True
            self.worker.start()

    def _stop_on_signal(self, signal_number: int, frame: FrameType | None) -> None:
        self.request_stop()

    def close(self) -> None:
        """Stops a run the user left early and waits for it, so the sources are restored before strain exits."""
        self.active = False
        if self.worker is not None and self.worker.is_alive():
            self.request_stop()
            self.err.write("strain: stopping the run and restoring the sources…\n")
            self.err.flush()
            while self.worker.is_alive():
                self.worker.join(WORKER_POLL_SECONDS)

    def worker_finished(self) -> bool:
        return self.worker is not None and self.worker_started and not self.worker.is_alive()

    def worker_failed(self) -> bool:
        return self.worker_finished() and self.worker_error is not None

    def request_stop(self) -> None:
        self.live.set_flag("stopping", True)
        self.stop_requested.set()
        self.resume.set()

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self.live.set_flag("paused", paused)
        if paused:
            self.resume.clear()
        else:
            self.resume.set()

    def check_stop(self) -> None:
        """Raises KeyboardInterrupt in the runner, once, after the user asked to stop."""
        if self.stop_requested.is_set() and not self.stop_raised:
            self.stop_raised = True
            raise KeyboardInterrupt

    # Console output, kept for the log view and the transcript.

    def line(self, text: str = "") -> None:
        if not self.active:
            super().line(text)
            return
        self.check_stop()
        self.transcript.append(text)
        self.live.add_line(text)

    def status(self, text: str) -> None:
        if self.active:
            self.check_stop()
            self.live.set_status(text)

    def warn(self, message: str) -> None:
        if not self.active:
            super().warn(message)
            return
        self.warnings.append(message)
        self.live.add_line(f"warning: {message}")

    def finish(self) -> None:
        # Taken before printing: Console.warn calls finish() again.
        transcript, self.transcript = self.transcript, []
        warnings, self.warnings = self.warnings, []
        self.active = False
        for text in transcript:
            super().line(text)
        for message in warnings:
            super().warn(message)
        super().finish()

    # Progress hooks.

    def test_started(self, name: str, number: int, total: int) -> None:
        self.check_stop()
        self.live.set_test(name, number, total)

    def plan(self, mutants: Sequence[Mutant]) -> None:
        self.live.plan(mutants)

    def mutant_started(self, index: int, source: str, mutant: Mutant) -> None:
        self.check_stop()
        if not self.resume.is_set():
            self.live.set_flag("waiting", True)
            while not self.resume.wait(WORKER_POLL_SECONDS):
                pass
            self.live.set_flag("waiting", False)
            self.check_stop()
        self.live.start_mutant(index, source, mutant)

    def mutant_finished(self, index: int, state: str) -> None:
        self.live.finish_mutant(index, state)

    def mutant_evaluated(self, result: MutantResult, source: str, line: LineCoverage | None) -> None:
        self.live.record_result(result, source, line)

    def safety_nets_checked(self, relaxed: Sequence[RelaxedSafetyNet], holding: Sequence[SafetyNet]) -> None:
        self.live.set_safety_nets(relaxed, holding)

    def build_started(self, command: str) -> None:
        self.check_stop()
        self.live.start_build(command)

    def build_output(self, line: str) -> None:
        self.check_stop()
        self.live.build_output(line)

    def run_finished(self) -> None:
        self.set_paused(False)
        self.live.finish_run()
