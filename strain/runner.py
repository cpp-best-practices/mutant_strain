"""The strategies: baseline, generate mutants on covered lines, mutate, rebuild, run, restore."""

import enum
import json
import os
import shlex
import time
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from . import NAME, ctest, git
from .build import (
    BuildClock,
    CompilationDatabase,
    CompileCommand,
    Target,
    build,
    build_command,
    generator_warning,
    parallel_jobs,
    read_cmake_cache,
    read_cxx_compiler,
    target_artifacts,
)
from .build_cache import BuildFailureCache, cache_path
from .compile_time import NOT_COMPILE_TIME, compile_time_only_lines, error_messages, is_constant_evaluation_failure
from .config import CONFIG_FILE, Config, load_config
from .console import BOLD, DIM, RED, STATE_STYLES, YELLOW, UiMode, create_console, plural, score_style
from .coverage import FileCoverage, GcovCoverage
from .estimate import BuildTimes, Eta, Order, format_eta
from .journal import Journal
from .mutators import (
    Engine,
    Mutant,
    apply_mutant,
    describe_mutant,
    fallback_engine,
    mutants_at,
    operator_sites,
    resolve_engine,
    suppressed_lines,
)
from .operator_sites import EngineError, OperatorSite
from .html_report import html_report
from .patterns import pattern_lines
from .report import (
    NO_MUTANTS,
    EventLog,
    FileScore,
    action_items,
    compiler_warning,
    file_scores,
    format_score,
    markdown_summary,
    mutation_testing_elements,
    source_excerpt,
    summary_line,
)
from .results import (
    DETECTED_STATES,
    CombinedResult,
    MutantResult,
    MutantState,
    SurvivorKind,
    baseline_problem,
    classify_run,
    combine,
    mutation_score,
)
from .prerequisites import gcov_version_text, no_coverage_data, prerequisite_problems
from .safety_nets import (
    Level,
    SafetyNet,
    build_wide_problems,
    classify_failures,
    where_build_wide,
    where_missing,
    missing_safety_nets,
    resolve_levels,
)

BASELINE_TIMEOUT_SECONDS = 600.0
MINIMUM_TIMEOUT_SECONDS = 10.0
TIMEOUT_FACTOR = 3.0
DEFAULT_ACTION_ITEMS = 10
SOURCE_ENCODING = "utf-8"
SOURCE_ERRORS = "surrogateescape"


class StrainError(Exception):
    """A problem that stops the run before or between tests."""


class Strategy(enum.StrEnum):
    """per-test builds every mutant once per test; per-mutant builds it once and runs its tests until one kills it."""

    PER_TEST = "per-test"
    PER_MUTANT = "per-mutant"


@dataclass(frozen=True, slots=True)
class RunOptions:
    build_dir: Path = Path(".")
    include: str | None = None
    exclude: str | None = None
    since: str | None = None
    skip_killed: bool = False
    strategy: Strategy = Strategy.PER_TEST
    order: Order = Order.COST
    build_cache: bool = True
    compile_time: bool = False
    engine: Engine = Engine.AUTO
    safety_nets: Level | None = None
    threshold: float | None = None
    strict: bool = False
    max_per_line: int | None = None
    resume: bool = False
    show_all: bool = False
    ui: UiMode = UiMode.AUTO
    report_dir: Path = Path("strain-report")
    jobs: int = 1


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: Path
    relative: str
    text: str
    coverage: FileCoverage


def cmake_source_dir(build_dir: Path) -> Path:
    if not (build_dir / "CMakeCache.txt").is_file():
        raise StrainError(f"{build_dir} is not a CMake build directory (no CMakeCache.txt)")
    source_dir = read_cmake_cache(build_dir).get("CMAKE_HOME_DIRECTORY")
    if not source_dir:
        raise StrainError(f"{build_dir / 'CMakeCache.txt'} does not name a source directory")
    return Path(source_dir).resolve()


def short_path(path: Path) -> str:
    """`path` relative to the working directory when that is shorter, for titles that have little room."""
    try:
        relative = os.path.relpath(path)
    except ValueError:
        return str(path)
    return min(relative, str(path), key=len)


@dataclass(frozen=True, slots=True)
class PlannedMutant:
    source: SourceFile
    suppressed: frozenset[int]
    mutant: Mutant


def mutant_timeout(baseline_seconds: float) -> float:
    """How long a test may run with a mutant in place, from how long it took without one."""
    return max(MINIMUM_TIMEOUT_SECONDS, baseline_seconds * TIMEOUT_FACTOR)


@dataclass(frozen=True, slots=True)
class BaselinedTest:
    name: str
    duration: float
    sources: list[SourceFile]


@dataclass(frozen=True, slots=True)
class CoveringTest:
    name: str
    duration: float
    source: SourceFile

    @property
    def timeout(self) -> float:
        return mutant_timeout(self.duration)


@dataclass(slots=True)
class CoveredMutant:
    planned: PlannedMutant
    tests: list[CoveringTest] = field(default_factory=list)


def by_source(entry: PlannedMutant) -> tuple[str, int]:
    return entry.mutant.file, entry.mutant.offset


def mutant_cost(entry: CoveredMutant, build_times: BuildTimes) -> float:
    """At most what evaluating `entry` takes: building it, then running every test that covers it."""
    return build_times.estimate(entry.planned.mutant.file) + sum(test.duration for test in entry.tests)


def order_mutants(covered: Sequence[CoveredMutant], order: Order, build_times: BuildTimes) -> list[CoveredMutant]:
    """By file and offset, then for `Order.COST` the cheapest first, so an interrupted run has covered the most."""
    ordered = sorted(covered, key=lambda entry: by_source(entry.planned))
    if order is Order.SOURCE:
        return ordered
    return sorted(ordered, key=lambda entry: mutant_cost(entry, build_times))


def order_planned(planned: Sequence[PlannedMutant], order: Order, build_times: BuildTimes) -> list[PlannedMutant]:
    """The per-test order: every mutant runs the same test, so the cheapest are those of the fastest files to build."""
    ordered = sorted(planned, key=by_source)
    if order is Order.SOURCE:
        return ordered
    return sorted(ordered, key=lambda entry: build_times.estimate(entry.mutant.file))


@dataclass(frozen=True, slots=True)
class Outcome:
    state: MutantState
    survivor: SurvivorKind | None = None
    detail: str = ""


def batches(tests: Sequence[CoveringTest], size: int) -> Iterator[Sequence[CoveringTest]]:
    for start in range(0, len(tests), size):
        yield tests[start : start + size]


def survivor_kind(coverage: FileCoverage, line: int) -> SurvivorKind | None:
    line_coverage = coverage.lines.get(line)
    if line_coverage is None or line_coverage.branches == 0:
        return None
    if line_coverage.branches_taken < line_coverage.branches:
        return SurvivorKind.NOT_REACHED
    return SurvivorKind.NOT_DETECTED


class Runner:
    def __init__(self, options: RunOptions, out: TextIO, err: TextIO) -> None:
        self.options = options
        self.out = out
        self.console = create_console(out, err, options.ui)
        self.build_dir = options.build_dir.resolve()
        self.source_dir = cmake_source_dir(self.build_dir)
        self.build_clock = BuildClock()
        self.journal = Journal(self.build_dir / "strain-journal", self.build_clock.wait_until_writes_outdate_the_build)
        self.coverage = GcovCoverage(self.build_dir)
        self.config = Config()
        self.database = CompilationDatabase([])
        self.since: dict[str, set[int]] | None = None
        self.killed: set[str] = set()
        self.build_failures = BuildFailureCache()
        self.results: list[MutantResult] = []
        self.engine = Engine.LEXER
        self.checked = False
        self.sites: dict[str, list[OperatorSite]] = {}
        self.test_targets: dict[str, Target] = {}
        self.build_is_mutated = False
        self.coverage_verified = False
        self.build_jobs: int | None = None
        self.build_times = BuildTimes()
        self.eta: Eta | None = None
        self.mutant_started_at = 0.0
        self.mutant_build_seconds = 0.0
        self.build_output: list[str] = []
        self.compile_time_candidates: dict[str, set[int]] = {}
        self.runtime_lines: dict[str, set[int]] = {}

    def show_banner(self) -> None:
        console = self.console
        console.line(f"{console.paint(NAME, BOLD)} {console.paint('mutation testing for CMake/CTest', DIM)}")
        console.field("source", str(self.source_dir), 7)
        console.field("build", str(self.build_dir), 7)
        console.field("reports", str(self.options.report_dir), 7)

    def check_jobs(self) -> None:
        """Only the tests of one mutant can share its build, so --jobs needs the strategy that runs them together."""
        jobs = self.options.jobs
        if jobs == 1:
            return
        if self.options.strategy is not Strategy.PER_MUTANT:
            raise StrainError(
                f"--jobs {jobs} runs the covering tests of a mutant together; it needs --strategy per-mutant"
            )
        self.console.step(f"--jobs {jobs}: up to {jobs} covering tests of each mutant run at a time")

    def build(self, target: str | None = None) -> bool:
        self.console.build_started(shlex.join(build_command(Path(short_path(self.build_dir)), target, self.build_jobs)))
        self.build_output = []
        try:
            return build(self.build_dir, target, self.keep_build_output, self.build_jobs)
        finally:
            self.build_clock.build_finished()

    def keep_build_output(self, line: str) -> None:
        self.build_output.append(line)
        self.console.build_output(line)

    def build_or_fail(self, activity: str, success: str, failure: str) -> None:
        self.console.status(f"  {activity}…")
        start = time.monotonic()
        if not self.build():
            raise StrainError(failure)
        self.console.step(f"{success} ({time.monotonic() - start:.1f}s)")

    def check(self) -> None:
        """Everything `prepare` checks before it builds, so a failure stops the run before the live TUI starts."""
        self.show_banner()
        self.console.heading("Preparing")
        for path in self.journal.recover():
            self.console.warn(f"restored {path} from an interrupted run")
        if not git.is_clean(self.source_dir):
            raise StrainError(f"{NAME} needs a clean working tree: commit or stash the changes in {self.source_dir}")
        self.console.step("working tree is clean")
        self.config = load_config(self.source_dir)
        self.console.step(self.describe_config())
        self.database = CompilationDatabase.load(self.build_dir)
        commands = self.project_commands()
        self.console.step(f"compilation database: {plural(len(commands), 'project translation unit')}")
        self.check_prerequisites(commands)
        self.check_safety_nets(commands)
        self.check_generator()
        self.check_jobs()
        self.engine = self.resolve_engine()
        self.checked = True

    def prepare(self) -> None:
        if not self.checked:
            self.check()
        self.build_or_fail(
            f"building {self.build_dir}",
            "unmutated build is up to date",
            f"`cmake --build {self.build_dir}` fails before any mutation",
        )
        self.test_targets = self.find_test_targets()
        self.load_build_cache()
        if self.options.since is not None:
            self.since = git.changed_lines(self.source_dir, self.options.since)
            changed = sum(len(lines) for lines in self.since.values())
            self.console.step(
                f"--since {self.options.since}: {plural(changed, 'changed line')} in {plural(len(self.since), 'file')}"
            )

    def check_generator(self) -> None:
        cache = read_cmake_cache(self.build_dir)
        warning = generator_warning(cache)
        if warning is not None:
            self.console.warn(warning)
        self.build_jobs = parallel_jobs(cache, os.environ, os.cpu_count())
        if self.build_jobs is not None:
            self.console.step(f"building with --parallel {self.build_jobs}")

    def load_build_cache(self) -> None:
        """Loads the mutants earlier runs found to break the build at this commit, with this build configuration."""
        if not self.options.build_cache:
            self.console.note("--no-build-cache: build failures are remembered for this run only")
            return
        compile_database = (self.build_dir / "compile_commands.json").read_bytes()
        path = cache_path(self.build_dir, git.head_commit(self.source_dir), compile_database)
        self.build_failures = BuildFailureCache.load(path)
        known = plural(len(self.build_failures), "mutant")
        self.console.step(f"build cache: {known} known to break the build at this commit")

    def resolve_engine(self) -> Engine:
        try:
            engine = resolve_engine(self.options.engine)
        except EngineError as error:
            raise StrainError(f"{error}, or pass --engine lexer") from error
        self.console.step(f"mutation engine: {engine}")
        return engine

    def find_test_targets(self) -> dict[str, Target]:
        artifacts = target_artifacts(self.build_dir)
        return {
            test: artifacts[executable]
            for test, executable in ctest.executables_by_test(self.build_dir).items()
            if executable in artifacts
        }

    def target_for(self, test: str) -> str | None:
        """The CMake target that builds the program `test` runs; None rebuilds everything."""
        target = self.test_targets.get(test)
        return None if target is None else target.name

    def test_code(self, test: str) -> frozenset[Path]:
        """The sources of the program `test` runs: they do the testing, so mutating them tests nothing."""
        target = self.test_targets.get(test)
        return frozenset() if target is None else target.sources

    def describe_config(self) -> str:
        if not (self.source_dir / CONFIG_FILE).is_file():
            return f"no {CONFIG_FILE}; using defaults"
        config = self.config
        return (
            f"loaded {CONFIG_FILE}: {plural(len(config.exclude), 'exclude pattern')}, "
            f"{plural(len(config.arid), 'arid pattern')}, {plural(len(config.ignored_mutants), 'ignored mutant')}"
        )

    def is_project_source(self, path: Path) -> bool:
        return path.is_relative_to(self.source_dir) and not path.is_relative_to(self.build_dir)

    def project_commands(self) -> list[CompileCommand]:
        return [command for command in self.database.commands if self.is_project_source(command.file)]

    def check_prerequisites(self, commands: Sequence[CompileCommand]) -> None:
        problems = prerequisite_problems(commands, read_cxx_compiler(self.build_dir), gcov_version_text())
        if problems:
            raise StrainError("\n".join([*problems, "these prerequisites cannot be ignored; fix the build folder"]))
        self.console.step("coverage instrumentation is enabled and gcov matches the compiler")

    def safety_net_failures(self, commands: Sequence[CompileCommand]) -> dict[SafetyNet, str]:
        """Each failing safety net and where it fails."""
        build_wide = build_wide_problems(read_cmake_cache(self.build_dir), self.database.commands)
        failures = {net: where_build_wide(evidence) for net, evidence in build_wide.items()}
        failures.update((net, where_missing(files)) for net, files in missing_safety_nets(commands).items())
        return failures

    def check_safety_nets(self, commands: Sequence[CompileCommand]) -> None:
        levels = resolve_levels(self.config.safety_nets, self.options.safety_nets)
        missing = self.safety_net_failures(commands)
        errors, relaxed = classify_failures(missing, levels)
        for failure in relaxed:
            if failure.level is Level.WARN:
                self.console.warn(failure.message)
            else:
                self.console.note(f"safety net {failure.net} is missing but ignored by configuration")
        if errors:
            raise StrainError("\n".join([*errors, "rebuild with the flags from the README or pass --safety-nets=warn"]))
        present = [net for net in SafetyNet if net not in missing]
        self.console.safety_nets_checked(relaxed, present)
        if present:
            self.console.step(f"safety nets that hold for the whole build: {', '.join(present)}")

    def source_files(self, coverage: dict[Path, FileCoverage], test_code: frozenset[Path]) -> list[SourceFile]:
        files: list[SourceFile] = []
        for path, file_coverage in coverage.items():
            resolved = path.resolve()
            if not self.is_project_source(resolved) or resolved in test_code or not resolved.is_file():
                continue
            relative = resolved.relative_to(self.source_dir).as_posix()
            if self.config.is_excluded(relative):
                continue
            text = resolved.read_bytes().decode(SOURCE_ENCODING, SOURCE_ERRORS)
            files.append(SourceFile(resolved, relative, text, file_coverage))
        return sorted(files, key=lambda file: file.relative)

    def sites_for(self, source: SourceFile) -> list[OperatorSite]:
        """The operators the engine finds in `source`, found once per run since every test restores the sources."""
        if source.relative not in self.sites:
            self.sites[source.relative] = self.find_sites(source)
        return self.sites[source.relative]

    def find_sites(self, source: SourceFile) -> list[OperatorSite]:
        """The engine's sites; under `--engine auto`, a source it cannot parse goes to the next installed engine."""
        try:
            return operator_sites(self.engine, source.text)
        except EngineError as error:
            fallback = fallback_engine(self.engine) if self.options.engine is Engine.AUTO else None
            if fallback is None:
                raise StrainError(f"{source.relative}: {error}, or pass --engine lexer") from error
            self.console.warn(f"{source.relative}: {error}; using the {fallback} engine for this file")
            return operator_sites(fallback, source.text)

    def in_scope(self, source: SourceFile, lines: set[int]) -> set[int]:
        """`lines` restricted to the lines changed since `--since`, if given."""
        return lines if self.since is None else lines & self.since.get(source.relative, set())

    def compile_time_lines(self, source: SourceFile) -> set[int]:
        """The lines of `source` only evaluated at compile time, remembered so the reports can list them."""
        site_lines = {site.line for site in self.sites_for(source)}
        lines = self.in_scope(source, compile_time_only_lines(source.text, source.coverage, site_lines))
        self.compile_time_candidates.setdefault(source.relative, set()).update(lines)
        self.runtime_lines.setdefault(source.relative, set()).update(source.coverage.executed_lines())
        return lines

    def mutants_for(self, source: SourceFile) -> list[Mutant]:
        lines = self.in_scope(source, source.coverage.executed_lines())
        compile_time = self.compile_time_lines(source)
        if self.options.compile_time:
            lines |= compile_time
        return mutants_at(source.relative, self.sites_for(source), lines, self.options.max_per_line)

    def not_mutated_at_compile_time(self) -> list[tuple[str, int]]:
        """The compile-time only lines no selected test executes, unless --compile-time mutated them."""
        if self.options.compile_time:
            return []
        return [
            (file, line)
            for file, candidates in sorted(self.compile_time_candidates.items())
            for line in sorted(candidates - self.runtime_lines.get(file, set()))
        ]

    def plan_mutants(self, sources: Sequence[SourceFile]) -> list[PlannedMutant]:
        executed = sum(len(source.coverage.executed_lines()) for source in sources)
        self.console.step(f"coverage: {plural(executed, 'executed line')} in {plural(len(sources), 'project file')}")
        planned: list[PlannedMutant] = []
        for source in sources:
            suppressed = frozenset(suppressed_lines(source.text, self.config.arid))
            mutants = self.mutants_for(source)
            lines = {mutant.line for mutant in mutants}
            self.console.note(f"{source.relative}: {plural(len(mutants), 'mutant')} on {plural(len(lines), 'line')}")
            planned += [PlannedMutant(source, suppressed, mutant) for mutant in mutants]
        if not planned:
            self.console.note("no mutants on the lines this test executes")
        return planned

    @contextmanager
    def mutated(self, planned: PlannedMutant) -> Iterator[None]:
        source = planned.source
        mutated = apply_mutant(source.text, planned.mutant).encode(SOURCE_ENCODING, SOURCE_ERRORS)
        self.build_is_mutated = True
        with self.journal.mutated(source.path, mutated):
            yield

    def targets_for(self, tests: Sequence[str]) -> list[str | None]:
        """The targets to rebuild for `tests`, or just None to rebuild everything when one of them has no target."""
        targets = [self.target_for(test) for test in tests]
        return [None] if None in targets else list(dict.fromkeys(targets))

    def build_failure(
        self, planned: PlannedMutant, targets: Sequence[str | None], label: str
    ) -> tuple[str, str] | None:
        """Compiles the mutated source; returns why it does not build and the diagnostics, or None when it does."""
        command = self.database.command_for(planned.source.path)
        self.console.status(f"{label}  checking syntax…")
        syntax_errors = None if command is None else command.syntax_errors()
        if syntax_errors is not None:
            return "rejected by -fsyntax-only", syntax_errors
        self.console.status(f"{label}  building…")
        if not all(self.build(target) for target in targets):
            return "build failed", "\n".join(self.build_output)
        return None

    def build_mutant(self, planned: PlannedMutant, targets: Sequence[str | None], label: str) -> Outcome | None:
        """Compiles the mutated source; returns, and remembers, the outcome of a mutant that does not build."""
        start = time.monotonic()
        failure = self.build_failure(planned, targets, label)
        self.observe_build(planned.mutant.file, time.monotonic() - start)
        if failure is None:
            return None
        detail, diagnostics = failure
        if is_constant_evaluation_failure(diagnostics):
            reason = error_messages(diagnostics)[0]
            self.build_failures.add_compile_time_kill(planned.mutant, reason)
            return Outcome(MutantState.KILLED_AT_COMPILE_TIME, detail=reason)
        self.build_failures.add(planned.mutant, detail)
        return Outcome(MutantState.COMPILE_ERROR, detail=detail)

    def observe_build(self, file: str, seconds: float) -> None:
        self.build_times.observe(file, seconds)
        self.mutant_build_seconds += seconds

    def start_estimate(self, test_seconds: Sequence[float], files: Sequence[str]) -> None:
        self.eta = Eta(test_seconds, files, self.build_times)

    def start_mutant(self, index: int, source: str, mutant: Mutant) -> None:
        self.mutant_started_at = time.monotonic()
        self.mutant_build_seconds = 0.0
        self.console.mutant_started(index, source, mutant)

    def finish_estimate(self, index: int) -> None:
        """Counts mutant `index` as evaluated, so the ETA the console and the events show includes it."""
        if self.eta is None:
            return
        self.eta.finish(index, time.monotonic() - self.mutant_started_at, self.mutant_build_seconds)
        self.console.eta_estimated(self.eta.remaining())

    def remaining_estimate(self) -> float | None:
        return None if self.eta is None else self.eta.remaining()

    def known_build_failure(self, planned: PlannedMutant) -> Outcome | None:
        """The outcome a mutant known to break the build gets without mutating or building anything."""
        kill = self.build_failures.compile_time_kill(planned.mutant)
        if kill is not None:
            return Outcome(MutantState.KILLED_AT_COMPILE_TIME, detail=f"{kill}, known from the cache")
        failure = self.build_failures.failure(planned.mutant)
        return (
            None if failure is None else Outcome(MutantState.COMPILE_ERROR, detail=f"{failure}, known from the cache")
        )

    def run_mutated(self, test: str, timeout: float, coverage: FileCoverage, line: int, label: str) -> Outcome:
        self.console.status(f"{label}  running {test}…")
        return self.classify(ctest.run_test(self.build_dir, test, timeout), coverage, line)

    @staticmethod
    def classify(run: ctest.RunResult, coverage: FileCoverage, line: int) -> Outcome:
        state = classify_run(run)
        survivor = survivor_kind(coverage, line) if state is MutantState.SURVIVED else None
        return Outcome(
            state, survivor, f"{run.duration:.2f}s" if survivor is None else f"{run.duration:.2f}s, {survivor}"
        )

    def evaluate(self, test: str, planned: PlannedMutant, timeout: float, label: str) -> Outcome:
        known = self.known_build_failure(planned)
        if known is not None:
            return known
        with self.mutated(planned):
            problem = self.build_mutant(planned, self.targets_for([test]), label)
            if problem is not None:
                return problem
            return self.run_mutated(test, timeout, planned.source.coverage, planned.mutant.line, label)

    def run_baseline(self, test: str, events: EventLog) -> float | None:
        """How long `test` takes without a mutant, or None when it is excluded."""
        self.coverage.reset()
        self.console.status(f"  running {test} without mutation…")
        baseline = ctest.run_test(self.build_dir, test, BASELINE_TIMEOUT_SECONDS)
        problem = baseline_problem(baseline)
        events.emit("baseline", test=test, duration=baseline.duration, problem=problem)
        if problem is None:
            timeout = mutant_timeout(baseline.duration)
            self.console.step(f"baseline passed in {baseline.duration:.2f}s; mutants time out after {timeout:.0f}s")
            return baseline.duration
        if self.options.strict:
            raise StrainError(f"test {test} {problem}; excluded tests are fatal with --strict")
        self.console.warn(f"test {test} {problem}; excluding it")
        return None

    def outcome_for(
        self, test: str, planned: PlannedMutant, timeout: float, events: EventLog, label: str
    ) -> Outcome | None:
        mutant = planned.mutant
        previous = self.previous_outcome(test, mutant, events)
        if previous is not None:
            return previous
        ignored = self.ignored_outcome(planned)
        if ignored is not None:
            return ignored
        if self.options.skip_killed and mutant.id in self.killed:
            return None
        return self.evaluate(test, planned, timeout, label)

    def previous_outcome(self, test: str, mutant: Mutant, events: EventLog) -> Outcome | None:
        previous = events.previous_results().get((test, mutant.id))
        return None if previous is None else Outcome(*previous, detail="from the interrupted run")

    def ignored_outcome(self, planned: PlannedMutant) -> Outcome | None:
        if planned.mutant.line in planned.suppressed:
            return Outcome(MutantState.IGNORED, detail="line suppressed by a mutate-off comment or arid pattern")
        if planned.mutant.id in self.config.ignored_mutants:
            return Outcome(MutantState.IGNORED, detail=f"ignored in {CONFIG_FILE}")
        return None

    def run_test(self, test: str, events: EventLog) -> list[MutantResult]:
        duration = self.run_baseline(test, events)
        if duration is None:
            return []
        timeout = mutant_timeout(duration)
        planned = self.plan_test(test, duration)
        width = len(str(len(planned)))
        results: list[MutantResult] = []
        for number, entry in enumerate(planned, start=1):
            progress = f"[{number:>{width}}/{len(planned)}]"
            label = f"  {progress} {describe_mutant(entry.mutant)}"
            self.start_mutant(number - 1, entry.source.text, entry.mutant)
            outcome = self.outcome_for(test, entry, timeout, events, label)
            self.finish_estimate(number - 1)
            if outcome is None:
                self.show_mutant(progress, entry.mutant, "skipped", "already killed by an earlier test")
                self.console.mutant_finished(number - 1, "skipped")
                continue
            function = entry.source.coverage.function_at(entry.mutant.line)
            results.append(MutantResult(entry.mutant, test, outcome.state, outcome.survivor, function))
            self.record(results[-1], events)
            self.console.mutant_evaluated(
                results[-1], entry.source.text, entry.source.coverage.lines.get(entry.mutant.line)
            )
            self.show_mutant(progress, entry.mutant, outcome.state, outcome.detail)
            self.console.mutant_finished(number - 1, outcome.state)
        self.rebuild_if_mutated()
        return results

    def plan_test(self, test: str, duration: float) -> list[PlannedMutant]:
        coverage = self.coverage.collect()
        self.verify_coverage_data(test, coverage)
        planned = order_planned(
            self.plan_mutants(self.source_files(coverage, self.test_code(test))), self.options.order, self.build_times
        )
        self.console.plan([entry.mutant for entry in planned])
        self.start_estimate([duration] * len(planned), [entry.mutant.file for entry in planned])
        return planned

    def verify_coverage_data(self, test: str, coverage: dict[Path, FileCoverage]) -> None:
        if self.coverage_verified:
            return
        if not any(self.is_project_source(path.resolve()) for path in coverage):
            raise StrainError(no_coverage_data(test))
        self.coverage_verified = True
        self.console.step("the tests write coverage data for project sources")

    def rebuild_if_mutated(self) -> None:
        if self.build_is_mutated:
            self.build_or_fail(
                "rebuilding the restored sources",
                "sources restored and rebuilt",
                f"`cmake --build {self.build_dir}` fails after restoring the sources",
            )
        self.build_is_mutated = False

    def record(self, result: MutantResult, events: EventLog) -> None:
        mutant = result.mutant
        self.results.append(result)
        if result.state in DETECTED_STATES:
            self.killed.add(mutant.id)
        events.emit(
            "mutant",
            test=result.test,
            id=mutant.id,
            file=mutant.file,
            line=mutant.line,
            column=mutant.column,
            operator=mutant.operator,
            original=mutant.original,
            replacement=mutant.replacement,
            state=result.state,
            survivor=result.survivor,
            eta=self.remaining_estimate(),
        )

    def show_mutant(self, progress: str, mutant: Mutant, state: str, detail: str) -> None:
        console = self.console
        eta = self.remaining_estimate()
        estimate = "" if eta is None else f"  ETA {format_eta(eta)}"
        console.line(
            f"  {console.paint(progress, DIM)} {console.state(state)} {describe_mutant(mutant)}"
            f"  {console.paint(f'{detail}  id {mutant.id}{estimate}', DIM)}"
        )

    def read_sources(self, combined: Sequence[CombinedResult]) -> dict[str, str]:
        return {
            entry.mutant.file: (self.source_dir / entry.mutant.file).read_bytes().decode(SOURCE_ENCODING, SOURCE_ERRORS)
            for entry in combined
        }

    def write_reports(self, combined: Sequence[CombinedResult], sources: Mapping[str, str]) -> None:
        report_dir = self.options.report_dir
        (report_dir / "report.json").write_text(
            json.dumps(mutation_testing_elements(combined, sources), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        no_mutants = self.why_no_mutants()
        (report_dir / "index.html").write_text(html_report(combined, sources, no_mutants), encoding="utf-8")
        (report_dir / "summary.md").write_text(
            markdown_summary(combined, self.options.threshold, no_mutants, self.compile_time_notes()), encoding="utf-8"
        )

    def compile_time_notes(self) -> list[str]:
        lines = self.not_mutated_at_compile_time()
        if not lines:
            return []
        return [f"{plural(len(lines), 'line')} {NOT_COMPILE_TIME}; mutate them with --compile-time"]

    def show_not_mutated(self) -> None:
        lines = self.not_mutated_at_compile_time()
        if lines:
            self.console.heading(f"Not mutated ({len(lines)})")
            for file, line in lines:
                self.console.line(f"  {file}:{line}: {self.console.paint(NOT_COMPILE_TIME, YELLOW)}")

    def why_no_mutants(self) -> str:
        """Why a run may have no mutants; with --since, an empty run passes, so it must say why."""
        if self.since is None:
            return NO_MUTANTS
        changed = sum(len(lines) for lines in self.since.values())
        if changed == 0:
            return f"No source line changed since {self.options.since}, so there was nothing to mutate."
        return (
            f"No mutant was generated on the {plural(changed, 'line')} changed since {self.options.since}: "
            "the selected tests don't execute them, or they hold nothing to mutate."
        )

    def show_survivors(self, combined: Sequence[CombinedResult], sources: Mapping[str, str]) -> None:
        survivors = [entry for entry in combined if entry.state is MutantState.SURVIVED]
        if survivors:
            self.console.heading(f"Survivors ({len(survivors)})")
            for entry in survivors:
                self.console.line(self.console.paint(compiler_warning(entry, self.source_dir), BOLD))
                for line in source_excerpt(entry.mutant, sources[entry.mutant.file]):
                    self.console.line(line)

    def show_files(self, combined: Sequence[CombinedResult]) -> None:
        rows = file_scores(combined)
        if not rows:
            return
        console = self.console
        console.heading("Files")
        width = max(len(row.file) for row in rows)
        for row in rows:
            console.line(self.file_line(row, width))

    def file_line(self, row: FileScore, width: int) -> str:
        console = self.console
        percent = None if row.score is None else row.score * 100
        survived_style = STATE_STYLES[MutantState.SURVIVED] if row.survived else DIM
        return (
            f"  {row.file:<{width}}  {console.paint(f'{format_score(row.score):>6}', score_style(percent))}"
            f"   {plural(row.mutants, 'mutant')}  {row.detected} detected"
            f"  {console.paint(f'{row.survived} survived', survived_style)}"
        )

    def show_patterns(self, combined: Sequence[CombinedResult]) -> None:
        """The groups of survivors that one kind of test would kill, ahead of the survivors one by one."""
        lines = pattern_lines(combined)
        if lines:
            self.console.heading("Patterns")
            for line in lines:
                self.console.line(f"  · {line}")

    def show_action_items(self, combined: Sequence[CombinedResult]) -> None:
        items = action_items(combined, limit=None if self.options.show_all else DEFAULT_ACTION_ITEMS)
        if items:
            self.console.heading("Action items")
            for number, item in enumerate(items, start=1):
                self.console.line(f"  {number}. {item}")

    def show_summary(self, combined: Sequence[CombinedResult], score: float | None) -> None:
        console = self.console
        console.heading("Summary")
        states = [entry.state for entry in combined]
        counts = [
            console.paint(f"{state} {states.count(state)}", STATE_STYLES[state])
            for state in MutantState
            if state in states
        ]
        console.field("mutants", f"{len(combined)}   {'  '.join(counts)}" if counts else "none", 8)
        if not combined:
            console.line(f"  {console.paint(self.why_no_mutants(), BOLD + YELLOW)}")
        for note in self.compile_time_notes():
            console.line(f"  {console.paint(note, YELLOW)}")
        percent = None if score is None else score * 100
        console.line(
            f"  {console.paint('mutation score', BOLD)} {console.score(percent)}{self.threshold_note(percent)}"
        )
        report_dir = self.options.report_dir
        console.field("html", str(report_dir / "index.html"), 8)
        console.field("markdown", str(report_dir / "summary.md"), 8)
        console.field("report", str(report_dir / "report.json"), 8)
        console.field("events", str(report_dir / "events.jsonl"), 8)

    def threshold_note(self, percent: float | None) -> str:
        threshold = self.options.threshold
        if threshold is None or percent is None:
            return ""
        if percent < threshold:
            return "   " + self.console.paint(f"✗ below --threshold {threshold:g}%", BOLD + RED)
        return "   " + self.console.paint(f"✓ meets --threshold {threshold:g}%", DIM)

    def run_tests(self, tests: Sequence[str], events: EventLog) -> None:
        width = max(len(test) for test in tests)
        for number, test in enumerate(tests, start=1):
            self.console.heading(f"Test {number}/{len(tests)}: {test}")
            self.console.test_started(test, number, len(tests))
            test_results = self.run_test(test, events)
            self.console.line(f"  {self.console.paint(summary_line(test, test_results, width), BOLD)}")

    def baseline_tests(self, tests: Sequence[str], events: EventLog) -> list[BaselinedTest]:
        baselined: list[BaselinedTest] = []
        for number, test in enumerate(tests, start=1):
            self.console.heading(f"Baseline {number}/{len(tests)}: {test}")
            self.console.test_started(test, number, len(tests))
            duration = self.run_baseline(test, events)
            if duration is None:
                continue
            coverage = self.coverage.collect()
            self.verify_coverage_data(test, coverage)
            baselined.append(BaselinedTest(test, duration, self.source_files(coverage, self.test_code(test))))
        return baselined

    def cover_mutants(self, tests: Sequence[BaselinedTest]) -> list[CoveredMutant]:
        """Every mutant on a line some test executes, with the tests that execute it, fastest first.

        Running stops at the first test that detects the mutant, so a fast test that kills it spares the slow ones.
        """
        covered: dict[str, CoveredMutant] = {}
        for test in sorted(tests, key=lambda test: (test.duration, test.name)):
            for source in test.sources:
                suppressed = frozenset(suppressed_lines(source.text, self.config.arid))
                for mutant in self.mutants_for(source):
                    entry = covered.setdefault(mutant.id, CoveredMutant(PlannedMutant(source, suppressed, mutant)))
                    entry.tests.append(CoveringTest(test.name, test.duration, source))
        return order_mutants(list(covered.values()), self.options.order, self.build_times)

    def plan_covered_mutants(self, covered: Sequence[CoveredMutant]) -> None:
        self.console.heading("Mutants")
        by_file: dict[str, list[Mutant]] = {}
        for entry in covered:
            by_file.setdefault(entry.planned.mutant.file, []).append(entry.planned.mutant)
        for file, mutants in by_file.items():
            lines = {mutant.line for mutant in mutants}
            self.console.note(f"{file}: {plural(len(mutants), 'mutant')} on {plural(len(lines), 'line')}")
        if not covered:
            self.console.note("no mutants on the lines the tests execute")
        self.console.plan([entry.planned.mutant for entry in covered])

    def previous_outcomes(self, entry: CoveredMutant, events: EventLog) -> list[tuple[CoveringTest, Outcome]]:
        """The outcomes an interrupted run recorded for `entry`.

        Every test is looked up: the order is by baseline duration, and close durations can swap between runs.
        """
        outcomes: list[tuple[CoveringTest, Outcome]] = []
        for test in entry.tests:
            previous = self.previous_outcome(test.name, entry.planned.mutant, events)
            if previous is not None:
                outcomes.append((test, previous))
        return outcomes

    def run_covering_tests(
        self, entry: CoveredMutant, tests: Sequence[CoveringTest], label: str
    ) -> list[tuple[CoveringTest, Outcome]]:
        """Builds the mutant once, then runs `tests` until one detects it."""
        planned = entry.planned
        known = self.known_build_failure(planned)
        if known is not None:
            return [(test, known) for test in tests]
        with self.mutated(planned):
            problem = self.build_mutant(planned, self.targets_for([test.name for test in tests]), label)
            if problem is not None:
                return [(test, problem) for test in tests]
            return self.run_until_detected(planned, tests, label)

    def run_until_detected(
        self, planned: PlannedMutant, tests: Sequence[CoveringTest], label: str
    ) -> list[tuple[CoveringTest, Outcome]]:
        """Runs `tests` on the built mutant, `--jobs` at a time, until a batch detects it."""
        line = planned.mutant.line
        outcomes: list[tuple[CoveringTest, Outcome]] = []
        for batch in batches(tests, self.options.jobs):
            self.console.status(f"{label}  running {', '.join(test.name for test in batch)}…")
            runs = self.run_batch(batch)
            outcomes += [(test, self.classify(run, test.source.coverage, line)) for test, run in zip(batch, runs)]
            if any(outcome.state in DETECTED_STATES for _, outcome in outcomes):
                break
        return outcomes

    def run_batch(self, batch: Sequence[CoveringTest]) -> list[ctest.RunResult]:
        """Runs a lone test on this thread, so Ctrl-C and SIGTERM stop its CTest at once, and a batch in parallel."""
        if len(batch) == 1:
            return [ctest.run_test(self.build_dir, batch[0].name, batch[0].timeout)]
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            return list(pool.map(lambda test: ctest.run_test(self.build_dir, test.name, test.timeout), batch))

    def covered_outcomes(
        self, entry: CoveredMutant, events: EventLog, label: str
    ) -> list[tuple[CoveringTest, Outcome]]:
        ignored = self.ignored_outcome(entry.planned)
        if ignored is not None:
            return [(test, ignored) for test in entry.tests]
        outcomes = self.previous_outcomes(entry, events)
        done = {test.name for test, _ in outcomes}
        remaining = [test for test in entry.tests if test.name not in done]
        if any(outcome.state in DETECTED_STATES for _, outcome in outcomes) or not remaining:
            return outcomes
        return outcomes + self.run_covering_tests(entry, remaining, label)

    def evaluate_covered(self, index: int, entry: CoveredMutant, events: EventLog, progress: str) -> list[MutantResult]:
        mutant = entry.planned.mutant
        label = f"  {progress} {describe_mutant(mutant)}"
        outcomes = self.covered_outcomes(entry, events, label)
        self.finish_estimate(index)
        results: list[MutantResult] = []
        for test, outcome in outcomes:
            function = test.source.coverage.function_at(mutant.line)
            results.append(MutantResult(mutant, test.name, outcome.state, outcome.survivor, function))
            self.record(results[-1], events)
            self.console.mutant_evaluated(results[-1], test.source.text, test.source.coverage.lines.get(mutant.line))
            self.show_mutant(progress, mutant, outcome.state, f"{test.name}: {outcome.detail}")
        return results

    def run_mutants(self, tests: Sequence[str], events: EventLog) -> None:
        """The per-mutant strategy: baseline every test, then build each mutant once for all the tests covering it."""
        baselined = self.baseline_tests(tests, events)
        covered = self.cover_mutants(baselined)
        self.plan_covered_mutants(covered)
        self.start_estimate(
            [sum(test.duration for test in entry.tests) for entry in covered],
            [entry.planned.mutant.file for entry in covered],
        )
        width = len(str(len(covered)))
        results: list[MutantResult] = []
        for number, entry in enumerate(covered, start=1):
            self.start_mutant(number - 1, entry.planned.source.text, entry.planned.mutant)
            mutant_results = self.evaluate_covered(number - 1, entry, events, f"[{number:>{width}}/{len(covered)}]")
            results += mutant_results
            self.console.mutant_finished(number - 1, combine(mutant_results)[0].state)
        self.rebuild_if_mutated()
        self.show_test_summaries([test.name for test in baselined], results)

    def show_test_summaries(self, tests: Sequence[str], results: Sequence[MutantResult]) -> None:
        if not tests:
            return
        width = max(len(test) for test in tests)
        for test in tests:
            test_results = [result for result in results if result.test == test]
            self.console.line(f"  {self.console.paint(summary_line(test, test_results, width), BOLD)}")

    def run_strategy(self, tests: Sequence[str], events: EventLog) -> None:
        if self.options.strategy is Strategy.PER_MUTANT:
            self.run_mutants(tests, events)
        else:
            self.run_tests(tests, events)

    def require_mutants(self, combined: Sequence[CombinedResult]) -> None:
        """Without --since, a run that mutated nothing tested nothing, so it must not pass."""
        if combined or self.options.since is not None:
            return
        raise StrainError(
            "no mutant was generated for any test, so nothing was mutation tested; check that the tests pass, that "
            f"the build writes coverage data and that {CONFIG_FILE} does not exclude every source"
        )

    def report(self, combined: Sequence[CombinedResult], events: EventLog, ending: str, start: float) -> float | None:
        """Writes the reports and shows the results so far, whether the run `ending` finished or was interrupted."""
        score = mutation_score(entry.state for entry in combined)
        sources = self.read_sources(combined)
        self.write_reports(combined, sources)
        events.emit(ending, score=score)
        self.show_survivors(combined, sources)
        self.show_patterns(combined)
        self.show_action_items(combined)
        self.show_files(combined)
        self.show_not_mutated()
        self.show_summary(combined, score)
        self.console.line(f"\n{self.console.paint(f'{ending} after {time.monotonic() - start:.1f}s', DIM)}")
        return score

    def run(self) -> int:
        start = time.monotonic()
        self.prepare()
        tests = ctest.list_tests(self.build_dir, self.options.include, self.options.exclude)
        if not tests:
            raise StrainError("no tests selected")
        self.console.step(f"selected {plural(len(tests), 'test')}: {', '.join(tests)}")
        with EventLog(
            self.options.report_dir / "events.jsonl",
            self.out if self.options.ui is UiMode.JSON else None,
            self.options.resume,
        ) as events:
            if self.options.resume:
                self.console.step(f"resuming: {plural(len(events.previous_results()), 'earlier result')} reused")
            try:
                self.run_strategy(tests, events)
            except KeyboardInterrupt:
                self.report(combine(self.results), events, "interrupted", start)
                raise
            combined = combine(self.results)
            self.require_mutants(combined)
            score = self.report(combined, events, "finished", start)
        self.console.run_finished()
        threshold = self.options.threshold
        return 1 if threshold is not None and score is not None and score * 100 < threshold else 0


def run(options: RunOptions, out: TextIO, err: TextIO) -> int:
    runner = Runner(options, out, err)
    try:
        runner.check()
        return runner.console.run(runner.run)
    finally:
        runner.console.finish()
