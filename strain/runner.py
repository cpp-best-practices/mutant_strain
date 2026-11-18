"""The strategies: baseline, generate mutants on covered lines, mutate, rebuild, run, restore."""

import enum
import json
import math
import os
import re
import shlex
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from . import ctest, git
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
from .config import CONFIG_FILE, Config, load_config
from .console import BOLD, DIM, RED, STATE_STYLES, UiMode, create_console, plural, score_style
from .compile_time import (
    COMPILE_TIME_TEST,
    KILLED_AT_COMPILE_TIME,
    NOT_MUTATED_HEADING,
    NOT_MUTATED_HINT,
    NotMutated,
    compile_time_check_failure,
    constant_lines,
    not_mutated,
)
from .coverage import FileCoverage, GcovCoverage
from .estimate import Estimate, MutantCost, format_eta
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
from .report import (
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
# A mutant may use this many times the memory its test uses without one, but always at least the minimum.
MEMORY_FACTOR = 4
MINIMUM_MEMORY_MEGABYTES = 512
DEFAULT_ACTION_ITEMS = 10
CXX_SUFFIXES = frozenset(
    {".h", ".hh", ".hpp", ".hxx", ".h++", ".ipp", ".inl", ".tpp", ".c", ".cc", ".cpp", ".cxx", ".c++"}
)
SOURCE_ENCODING = "utf-8"
SOURCE_ERRORS = "surrogateescape"


class StrainError(Exception):
    """A problem that stops the run before or between tests."""


class Order(enum.StrEnum):
    """cost runs the mutants with the cheapest covering tests first, so most results arrive early; source keeps the
    order of the files and offsets, which is reproducible."""

    COST = "cost"
    SOURCE = "source"


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
    engine: Engine = Engine.AUTO
    safety_nets: Level | None = None
    threshold: float | None = None
    strict: bool = False
    max_per_line: int | None = None
    # Megabytes per test run with a mutant; None derives it from the baseline, and 0 turns the limit off.
    memory_limit: int | None = None
    compile_time: bool = False
    resume: bool = False
    show_all: bool = False
    ui: UiMode = UiMode.AUTO
    report_dir: Path = Path("strain-report")


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


@dataclass(slots=True)
class MutantTimes:
    """Whether the mutant in progress was built, and how long its tests took, for the estimate of the time left."""

    built: bool = False
    tests: float | None = None


@dataclass(frozen=True, slots=True)
class Outcome:
    state: MutantState
    survivor: SurvivorKind | None = None
    detail: str = ""


# An absolute path in a compiler diagnostic: it ends at whitespace, a colon, a quote or a bracket.
ABSOLUTE_PATH = re.compile(r"(?<![\w.])/[^\s:'\"()\[\]]+")


def relative_paths(diagnostics: str, source_dir: Path) -> str:
    """`diagnostics` with paths in the project relative to it, so details and the build cache stay short.

    A path counts if it resolves into the project, so a symlinked spelling such as macOS's /var for /private/var
    is shortened too.
    """

    def shorten(match: re.Match[str]) -> str:
        try:
            return Path(match.group(0)).resolve().relative_to(source_dir).as_posix()
        except (OSError, ValueError):
            return match.group(0)

    return ABSOLUTE_PATH.sub(shorten, diagnostics)


def failure_description(diagnostics: str, otherwise: str) -> str:
    """Why a mutant did not build: the compile-time check it failed, which kills it, or else `otherwise`."""
    check = compile_time_check_failure(diagnostics)
    return otherwise if check is None else f"{KILLED_AT_COMPILE_TIME}: {check}"


def build_failure_state(failure: str) -> MutantState:
    """A failed compile-time check detects the mutant; any other build failure only means it is invalid code."""
    return (
        MutantState.KILLED_AT_COMPILE_TIME if failure.startswith(KILLED_AT_COMPILE_TIME) else MutantState.COMPILE_ERROR
    )


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
        self.sites: dict[str, list[OperatorSite]] = {}
        self.test_targets: dict[str, Target] = {}
        self.test_executables: dict[str, Path] = {}
        self.memory_limits: dict[str, ctest.MemoryLimit] = {}
        self.build_log: list[str] = []
        # Every line some test ran without a mutant, by file, to find the constexpr code that only the compiler runs.
        self.runtime_lines: dict[Path, set[int]] = {}
        self.compile_time_only: list[NotMutated] = []
        self.times = MutantTimes()
        self.eta = ""
        self.build_is_mutated = False
        self.coverage_verified = False
        self.build_jobs: int | None = None

    def show_banner(self) -> None:
        console = self.console
        console.line(f"{console.paint('strain', BOLD)} {console.paint('mutation testing for CMake/CTest', DIM)}")
        console.field("source", str(self.source_dir), 7)
        console.field("build", str(self.build_dir), 7)
        console.field("reports", str(self.options.report_dir), 7)

    def build(self, target: str | None = None) -> bool:
        """Builds `target`, keeping what it printed in `build_log` to tell a failed compile-time check apart."""
        self.console.build_started(shlex.join(build_command(Path(short_path(self.build_dir)), target, self.build_jobs)))
        self.build_log = []

        def show(line: str) -> None:
            self.build_log.append(line)
            self.console.build_output(line)

        try:
            return build(self.build_dir, target, show, self.build_jobs)
        finally:
            self.build_clock.build_finished()

    def build_or_fail(self, activity: str, success: str, failure: str) -> None:
        self.console.status(f"  {activity}…")
        start = time.monotonic()
        if not self.build():
            raise StrainError(failure)
        self.console.step(f"{success} ({time.monotonic() - start:.1f}s)")

    def prepare(self) -> None:
        self.console.heading("Preparing")
        for path in self.journal.recover():
            self.console.warn(f"restored {path} from an interrupted run")
        if not git.is_clean(self.source_dir):
            raise StrainError(f"strain needs a clean working tree: commit or stash the changes in {self.source_dir}")
        self.console.step("working tree is clean")
        self.config = load_config(self.source_dir)
        self.console.step(self.describe_config())
        self.database = CompilationDatabase.load(self.build_dir)
        commands = self.project_commands()
        self.console.step(f"compilation database: {plural(len(commands), 'project translation unit')}")
        self.check_prerequisites(commands)
        self.check_safety_nets(commands)
        self.check_generator()
        self.engine = self.resolve_engine()
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
        self.test_executables = ctest.executables_by_test(self.build_dir)
        return {
            test: artifacts[executable] for test, executable in self.test_executables.items() if executable in artifacts
        }

    def memory_limit_for(self, test: str, peak_memory: int | None) -> ctest.MemoryLimit | None:
        """How much memory `test` may use with a mutant in place, so a mutant that allocates forever is stopped.

        When the program is unknown, the limit goes through ASAN_OPTIONS alone: harmless without ASan, while a data
        segment limit would stop an ASan program from starting.
        """
        megabytes = self.options.memory_limit
        if megabytes == 0:
            return None
        if megabytes is None:
            measured = math.ceil(MEMORY_FACTOR * (peak_memory or 0) / ctest.MEGABYTE)
            megabytes = max(MINIMUM_MEMORY_MEGABYTES, measured)
        executable = self.test_executables.get(test)
        return ctest.MemoryLimit(megabytes, asan=executable is None or ctest.uses_asan(executable))

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

    def mutants_for(self, source: SourceFile) -> list[Mutant]:
        lines = source.coverage.executed_lines()
        if self.since is not None:
            lines &= self.since.get(source.relative, set())
        return mutants_at(source.relative, self.sites_for(source), lines, self.options.max_per_line)

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

    def build_failure(self, planned: PlannedMutant, targets: Sequence[str | None], label: str) -> str | None:
        """Compiles the mutated source; returns why it does not build, or None when it does."""
        command = self.database.command_for(planned.source.path)
        self.console.status(f"{label}  checking syntax…")
        diagnostics = None if command is None else command.syntax_check()
        if diagnostics is not None:
            return failure_description(relative_paths(diagnostics, self.source_dir), "rejected by -fsyntax-only")
        self.console.status(f"{label}  building…")
        for target in targets:
            if not self.build(target):
                return failure_description(relative_paths("\n".join(self.build_log), self.source_dir), "build failed")
        return None

    def build_mutant(self, planned: PlannedMutant, targets: Sequence[str | None], label: str) -> Outcome | None:
        """Compiles the mutated source; returns the compile error outcome, and remembers it, when it does not build."""
        self.times.built = True
        failure = self.build_failure(planned, targets, label)
        if failure is None:
            return None
        self.build_failures.add(planned.mutant, failure)
        return Outcome(build_failure_state(failure), detail=failure)

    def known_build_failure(self, planned: PlannedMutant) -> Outcome | None:
        """The outcome a mutant known not to build gets without mutating or building anything."""
        failure = self.build_failures.failure(planned.mutant)
        if failure is None:
            return None
        return Outcome(build_failure_state(failure), detail=f"{failure}, known from the cache")

    def run_mutated(self, test: str, timeout: float, coverage: FileCoverage, line: int, label: str) -> Outcome:
        self.console.status(f"{label}  running {test}…")
        memory = self.memory_limits.get(test)
        run = ctest.run_test(self.build_dir, test, timeout, memory)
        self.times.tests = (self.times.tests or 0.0) + run.duration
        state = classify_run(run, None if memory is None else memory.megabytes * ctest.MEGABYTE)
        survivor = survivor_kind(coverage, line) if state is MutantState.SURVIVED else None
        detail = f"{run.duration:.2f}s"
        if survivor is not None:
            detail += f", {survivor}"
        if state is MutantState.KILLED_BY_RESOURCE_LIMIT and memory is not None:
            detail += f", reached the {memory.megabytes} MB memory limit"
        return Outcome(state, survivor, detail)

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
        events.emit(
            "baseline", test=test, duration=baseline.duration, peak_memory=baseline.peak_memory, problem=problem
        )
        if problem is None:
            self.show_baseline(test, baseline)
            return baseline.duration
        if self.options.strict:
            raise StrainError(f"test {test} {problem}; excluded tests are fatal with --strict")
        self.console.warn(f"test {test} {problem}; excluding it")
        return None

    def show_baseline(self, test: str, baseline: ctest.RunResult) -> None:
        """Shows how the test did without a mutant, and the limits that derives for its mutants."""
        memory = self.memory_limit_for(test, baseline.peak_memory)
        if memory is not None:
            self.memory_limits[test] = memory
        used = "" if baseline.peak_memory is None else f" using {math.ceil(baseline.peak_memory / ctest.MEGABYTE)} MB"
        limits = f"time out after {mutant_timeout(baseline.duration):.0f}s"
        if memory is not None:
            limits += f" or stop at {memory.megabytes} MB"
        self.console.step(f"baseline passed in {baseline.duration:.2f}s{used}; mutants {limits}")

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
        coverage = self.coverage.collect()
        self.verify_coverage_data(test, coverage)
        self.note_runtime_lines(coverage)
        planned = self.plan_mutants(self.source_files(coverage, self.test_code(test)))
        self.console.plan([entry.mutant for entry in planned])
        estimate = Estimate([MutantCost(entry.mutant.file, duration) for entry in planned])
        self.eta = ""
        width = len(str(len(planned)))
        results: list[MutantResult] = []
        for number, entry in enumerate(planned, start=1):
            progress = f"[{number:>{width}}/{len(planned)}]"
            self.console.mutant_started(number - 1, entry.source.text, entry.mutant)
            with self.estimating(estimate, number - 1, events):
                state = self.test_planned(test, entry, timeout, events, progress, results)
            self.console.mutant_finished(number - 1, state)
        self.rebuild_if_mutated()
        return results

    def test_planned(
        self,
        test: str,
        entry: PlannedMutant,
        timeout: float,
        events: EventLog,
        progress: str,
        results: list[MutantResult],
    ) -> str:
        """Runs `test` on one mutant of the per-test strategy, adds the result to `results` and returns its state."""
        outcome = self.outcome_for(test, entry, timeout, events, f"  {progress} {describe_mutant(entry.mutant)}")
        if outcome is None:
            self.show_mutant(progress, entry.mutant, "skipped", "already killed by an earlier test")
            return "skipped"
        function = entry.source.coverage.function_at(entry.mutant.line)
        results.append(MutantResult(entry.mutant, test, outcome.state, outcome.survivor, function))
        self.record(results[-1], events)
        self.console.mutant_evaluated(
            results[-1], entry.source.text, entry.source.coverage.lines.get(entry.mutant.line)
        )
        self.show_mutant(progress, entry.mutant, outcome.state, outcome.detail)
        return outcome.state

    def note_runtime_lines(self, coverage: dict[Path, FileCoverage]) -> None:
        for path, file_coverage in coverage.items():
            self.runtime_lines.setdefault(path.resolve(), set()).update(file_coverage.executed_lines())

    def project_files(self, tests: Sequence[str]) -> Iterator[SourceFile]:
        """The project's C++ sources and headers that might hold constexpr code: not test code, not excluded."""
        test_code = frozenset().union(*(self.test_code(test) for test in tests))
        for directory, subdirectories, files in os.walk(self.source_dir):
            folder = Path(directory)
            subdirectories[:] = sorted(
                name
                for name in subdirectories
                if not name.startswith(".") and not (folder / name).resolve().is_relative_to(self.build_dir)
            )
            for name in sorted(files):
                path = (folder / name).resolve()
                if path.suffix not in CXX_SUFFIXES or path in test_code:
                    continue
                relative = path.relative_to(self.source_dir).as_posix()
                data = path.read_bytes()
                if self.config.is_excluded(relative) or (b"constexpr" not in data and b"consteval" not in data):
                    continue
                yield SourceFile(path, relative, data.decode(SOURCE_ENCODING, SOURCE_ERRORS), FileCoverage())

    def compile_time_mutants(self, tests: Sequence[str]) -> list[tuple[PlannedMutant, str]]:
        """The mutants on lines of constexpr and consteval functions that no test ran, with each function's name."""
        found: list[tuple[PlannedMutant, str]] = []
        for source in self.project_files(tests):
            executed = self.runtime_lines.get(source.path, set())
            functions = {line: name for line, name in constant_lines(source.text).items() if line not in executed}
            lines = set(functions)
            if self.since is not None:
                lines &= self.since.get(source.relative, set())
            suppressed = frozenset(suppressed_lines(source.text, self.config.arid))
            for mutant in mutants_at(source.relative, self.sites_for(source), lines, self.options.max_per_line):
                found.append((PlannedMutant(source, suppressed, mutant), functions[mutant.line]))
        return found

    def check_compile_time(self, tests: Sequence[str], events: EventLog) -> None:
        """Reports the constexpr code no test runs, or with --compile-time mutates it and builds each mutant."""
        found = self.compile_time_mutants(tests)
        if not self.options.compile_time:
            self.compile_time_only = not_mutated(found)
            return
        if found:
            self.run_compile_time(
                [planned for planned, _ in found], {planned.mutant.id: name for planned, name in found}, events
            )

    def run_compile_time(
        self, planned: Sequence[PlannedMutant], functions: Mapping[str, str], events: EventLog
    ) -> None:
        self.console.heading("Compile time")
        self.console.test_started(COMPILE_TIME_TEST, 1, 1)
        self.console.note(f"{plural(len(planned), 'mutant')} on constexpr lines no test runs")
        self.console.plan([entry.mutant for entry in planned])
        estimate = Estimate([MutantCost(entry.mutant.file, 0.0) for entry in planned])
        self.eta = ""
        width = len(str(len(planned)))
        for number, entry in enumerate(planned, start=1):
            progress = f"[{number:>{width}}/{len(planned)}]"
            self.console.mutant_started(number - 1, entry.source.text, entry.mutant)
            with self.estimating(estimate, number - 1, events):
                outcome = self.evaluate_at_compile_time(entry, f"  {progress} {describe_mutant(entry.mutant)}")
            result = MutantResult(entry.mutant, COMPILE_TIME_TEST, outcome.state, None, functions[entry.mutant.id])
            self.record(result, events)
            self.console.mutant_evaluated(result, entry.source.text, None)
            self.show_mutant(progress, entry.mutant, outcome.state, outcome.detail)
            self.console.mutant_finished(number - 1, outcome.state)
        self.rebuild_if_mutated()

    def evaluate_at_compile_time(self, planned: PlannedMutant, label: str) -> Outcome:
        """Builds everything with the mutant in place: only a failed compile-time check can detect it."""
        ignored = self.ignored_outcome(planned)
        if ignored is not None:
            return ignored
        known = self.known_build_failure(planned)
        if known is not None:
            return known
        with self.mutated(planned):
            problem = self.build_mutant(planned, [None], label)
        return problem or Outcome(MutantState.SURVIVED, detail="builds, and no test runs this line")

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
        )

    def show_mutant(self, progress: str, mutant: Mutant, state: str, detail: str) -> None:
        console = self.console
        eta = f"  · {self.eta}" if self.eta else ""
        console.line(
            f"  {console.paint(progress, DIM)} {console.state(state)} {describe_mutant(mutant)}"
            f"  {console.paint(f'{detail}  id {mutant.id}{eta}', DIM)}"
        )

    @contextmanager
    def estimating(self, estimate: Estimate, index: int, events: EventLog) -> Iterator[None]:
        """Times the mutant at `index` for `estimate`, then reports the time left."""
        self.times = MutantTimes()
        start = time.monotonic()
        yield
        if not self.times.built:
            estimate.skip(index)
        else:
            # Everything but the tests counts as the build, which includes mutating and restoring the source.
            estimate.record(index, time.monotonic() - start - (self.times.tests or 0.0), self.times.tests)
        remaining = estimate.remaining_seconds()
        self.eta = "" if remaining is None else format_eta(remaining)
        done = len(estimate.costs) - len(estimate.pending)
        events.emit("progress", done=done, total=len(estimate.costs), remaining_seconds=remaining)
        self.console.progress_estimated(remaining)

    def read_sources(self, combined: Sequence[CombinedResult]) -> dict[str, str]:
        return {
            entry.mutant.file: (self.source_dir / entry.mutant.file).read_bytes().decode(SOURCE_ENCODING, SOURCE_ERRORS)
            for entry in combined
        }

    def write_reports(self, combined: Sequence[CombinedResult], sources: Mapping[str, str]) -> None:
        report_dir = self.options.report_dir
        not_mutated = self.compile_time_only
        report = mutation_testing_elements(combined, sources, not_mutated)
        (report_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        (report_dir / "index.html").write_text(html_report(combined, sources, not_mutated), encoding="utf-8")
        summary = markdown_summary(combined, self.options.threshold, not_mutated)
        (report_dir / "summary.md").write_text(summary, encoding="utf-8")

    def show_not_mutated(self) -> None:
        if not self.compile_time_only:
            return
        self.console.heading(NOT_MUTATED_HEADING)
        for entry in self.compile_time_only:
            self.console.line(f"  · {entry.summary()}")
        self.console.line(f"  {self.console.paint(NOT_MUTATED_HINT, DIM)}")

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
            self.note_runtime_lines(coverage)
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
        return sorted(covered.values(), key=self.mutant_order)

    def mutant_order(self, entry: CoveredMutant) -> tuple[float, str, int]:
        """With --order cost, the mutants whose covering tests take least time come first, so most results arrive
        early and an interrupted run has covered the most ground."""
        cost = sum(test.duration for test in entry.tests) if self.options.order is Order.COST else 0.0
        return (cost, entry.planned.mutant.file, entry.planned.mutant.offset)

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
            outcomes: list[tuple[CoveringTest, Outcome]] = []
            for test in tests:
                outcome = self.run_mutated(test.name, test.timeout, test.source.coverage, planned.mutant.line, label)
                outcomes.append((test, outcome))
                if outcome.state in DETECTED_STATES:
                    break
            return outcomes

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

    def evaluate_covered(self, entry: CoveredMutant, events: EventLog, progress: str) -> list[MutantResult]:
        mutant = entry.planned.mutant
        label = f"  {progress} {describe_mutant(mutant)}"
        results: list[MutantResult] = []
        for test, outcome in self.covered_outcomes(entry, events, label):
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
        estimate = Estimate(
            [MutantCost(entry.planned.mutant.file, sum(test.duration for test in entry.tests)) for entry in covered]
        )
        self.eta = ""
        width = len(str(len(covered)))
        results: list[MutantResult] = []
        for number, entry in enumerate(covered, start=1):
            self.console.mutant_started(number - 1, entry.planned.source.text, entry.planned.mutant)
            with self.estimating(estimate, number - 1, events):
                mutant_results = self.evaluate_covered(entry, events, f"[{number:>{width}}/{len(covered)}]")
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
        compile_time = sum(entry.mutants for entry in self.compile_time_only)
        hint = f"; {plural(compile_time, 'mutant')} wait on constexpr lines no test runs: try --compile-time"
        raise StrainError(
            "no mutant was generated for any test, so nothing was mutation tested; check that the tests pass, that "
            f"the build writes coverage data and that {CONFIG_FILE} does not exclude every source"
            f"{hint if compile_time else ''}"
        )

    def report(self, combined: Sequence[CombinedResult], events: EventLog, ending: str, start: float) -> float | None:
        """Writes the reports and shows the results so far, whether the run `ending` finished or was interrupted."""
        score = mutation_score(entry.state for entry in combined)
        sources = self.read_sources(combined)
        self.write_reports(combined, sources)
        events.emit(ending, score=score)
        self.show_survivors(combined, sources)
        self.show_action_items(combined)
        self.show_files(combined)
        self.show_not_mutated()
        self.show_summary(combined, score)
        self.console.line(f"\n{self.console.paint(f'{ending} after {time.monotonic() - start:.1f}s', DIM)}")
        return score

    def run(self) -> int:
        start = time.monotonic()
        self.show_banner()
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
                self.check_compile_time(tests, events)
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
        return runner.console.run(runner.run)
    finally:
        runner.console.finish()
