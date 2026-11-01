"""The per-test strategy: baseline, generate mutants on covered lines, mutate, rebuild, run, restore."""

import json
import shlex
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from . import ctest, git
from .build import (
    CompilationDatabase,
    CompileCommand,
    build,
    build_command,
    read_cmake_cache,
    read_cxx_compiler,
    target_artifacts,
)
from .config import CONFIG_FILE, Config, load_config
from .console import BOLD, DIM, RED, STATE_STYLES, UiMode, plural
from .coverage import FileCoverage, GcovCoverage
from .dashboard import create_console
from .journal import Journal
from .mutators import (
    Engine,
    Mutant,
    apply_mutant,
    describe_mutant,
    mutants_at,
    operator_sites,
    resolve_engine,
    suppressed_lines,
)
from .operator_sites import EngineError, OperatorSite
from .report import EventLog, action_items, compiler_warning, mutation_testing_elements, summary_line
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
    describe_build_wide,
    describe_missing,
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


@dataclass(frozen=True, slots=True)
class RunOptions:
    build_dir: Path = Path(".")
    include: str | None = None
    exclude: str | None = None
    since: str | None = None
    skip_killed: bool = False
    engine: Engine = Engine.AUTO
    safety_nets: Level | None = None
    threshold: float | None = None
    strict: bool = False
    max_per_line: int | None = None
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


@dataclass(frozen=True, slots=True)
class PlannedMutant:
    source: SourceFile
    suppressed: frozenset[int]
    mutant: Mutant


@dataclass(frozen=True, slots=True)
class Outcome:
    state: MutantState
    survivor: SurvivorKind | None = None
    detail: str = ""


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
        self.journal = Journal(self.build_dir / "strain-journal")
        self.coverage = GcovCoverage(self.build_dir)
        self.config = Config()
        self.database = CompilationDatabase([])
        self.since: dict[str, set[int]] | None = None
        self.killed: set[str] = set()
        self.engine = Engine.LEXER
        self.sites: dict[str, list[OperatorSite]] = {}
        self.test_targets: dict[str, str] = {}
        self.build_is_mutated = False
        self.coverage_verified = False

    def show_banner(self) -> None:
        console = self.console
        console.line(f"{console.paint('strain', BOLD)} {console.paint('mutation testing for CMake/CTest', DIM)}")
        console.field("source", str(self.source_dir), 7)
        console.field("build", str(self.build_dir), 7)
        console.field("reports", str(self.options.report_dir), 7)

    def build(self, target: str | None = None) -> bool:
        self.console.build_started(shlex.join(build_command(self.build_dir, target)))
        return build(self.build_dir, target, self.console.build_output)

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
        self.engine = self.resolve_engine()
        self.build_or_fail(
            f"building {self.build_dir}",
            "unmutated build is up to date",
            f"`cmake --build {self.build_dir}` fails before any mutation",
        )
        self.test_targets = self.find_test_targets()
        if self.options.since is not None:
            self.since = git.changed_lines(self.source_dir, self.options.since)
            changed = sum(len(lines) for lines in self.since.values())
            self.console.step(
                f"--since {self.options.since}: {plural(changed, 'changed line')} in {plural(len(self.since), 'file')}"
            )

    def resolve_engine(self) -> Engine:
        try:
            engine = resolve_engine(self.options.engine)
        except EngineError as error:
            raise StrainError(f"{error}, or pass --engine lexer") from error
        self.console.step(f"mutation engine: {engine}")
        return engine

    def find_test_targets(self) -> dict[str, str]:
        artifacts = target_artifacts(self.build_dir)
        return {
            test: artifacts[executable]
            for test, executable in ctest.executables_by_test(self.build_dir).items()
            if executable in artifacts
        }

    def target_for(self, test: str) -> str | None:
        """The CMake target that builds the program `test` runs; None rebuilds everything."""
        return self.test_targets.get(test)

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
        build_wide = build_wide_problems(read_cmake_cache(self.build_dir), self.database.commands)
        failures = {net: describe_build_wide(net, evidence) for net, evidence in build_wide.items()}
        failures.update((net, describe_missing(net, files)) for net, files in missing_safety_nets(commands).items())
        return failures

    def check_safety_nets(self, commands: Sequence[CompileCommand]) -> None:
        levels = resolve_levels(self.config.safety_nets, self.options.safety_nets)
        missing = self.safety_net_failures(commands)
        errors: list[str] = []
        for net in SafetyNet:
            if net not in missing:
                continue
            message = missing[net]
            if levels[net] is Level.ERROR:
                errors.append(message)
            elif levels[net] is Level.WARN:
                self.console.warn(message)
            else:
                self.console.note(f"safety net {net} is missing but ignored by configuration")
        if errors:
            raise StrainError("\n".join([*errors, "rebuild with the flags from the README or pass --safety-nets=warn"]))
        present = [net for net in SafetyNet if net not in missing]
        if present:
            self.console.step(f"safety nets that hold for the whole build: {', '.join(present)}")

    def source_files(self, coverage: dict[Path, FileCoverage]) -> list[SourceFile]:
        files: list[SourceFile] = []
        for path, file_coverage in coverage.items():
            resolved = path.resolve()
            if not self.is_project_source(resolved) or not resolved.is_file():
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
            try:
                self.sites[source.relative] = operator_sites(self.engine, source.text)
            except EngineError as error:
                raise StrainError(f"{source.relative}: {error}") from error
        return self.sites[source.relative]

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

    def evaluate(self, test: str, planned: PlannedMutant, timeout: float, label: str) -> Outcome:
        source, mutant = planned.source, planned.mutant
        mutated = apply_mutant(source.text, mutant).encode(SOURCE_ENCODING, SOURCE_ERRORS)
        self.build_is_mutated = True
        with self.journal.mutated(source.path, mutated):
            command = self.database.command_for(source.path)
            self.console.status(f"{label}  checking syntax…")
            if command is not None and not command.passes_syntax_check():
                return Outcome(MutantState.COMPILE_ERROR, detail="rejected by -fsyntax-only")
            self.console.status(f"{label}  building…")
            if not self.build(self.target_for(test)):
                return Outcome(MutantState.COMPILE_ERROR, detail="build failed")
            self.console.status(f"{label}  running {test}…")
            run = ctest.run_test(self.build_dir, test, timeout)
        state = classify_run(run)
        survivor = survivor_kind(source.coverage, mutant.line) if state is MutantState.SURVIVED else None
        return Outcome(state, survivor, f"{run.duration:.2f}s" if survivor is None else f"{run.duration:.2f}s, {survivor}")

    def run_baseline(self, test: str, events: EventLog) -> float | None:
        self.coverage.reset()
        self.console.status(f"  running {test} without mutation…")
        baseline = ctest.run_test(self.build_dir, test, BASELINE_TIMEOUT_SECONDS)
        problem = baseline_problem(baseline)
        events.emit("baseline", test=test, duration=baseline.duration, problem=problem)
        if problem is None:
            timeout = max(MINIMUM_TIMEOUT_SECONDS, baseline.duration * TIMEOUT_FACTOR)
            self.console.step(f"baseline passed in {baseline.duration:.2f}s; mutants time out after {timeout:.0f}s")
            return timeout
        if self.options.strict:
            raise StrainError(f"test {test} {problem}; excluded tests are fatal with --strict")
        self.console.warn(f"test {test} {problem}; excluding it")
        return None

    def outcome_for(
        self, test: str, planned: PlannedMutant, timeout: float, events: EventLog, label: str
    ) -> Outcome | None:
        mutant = planned.mutant
        previous = events.previous_results().get((test, mutant.id))
        if previous is not None:
            return Outcome(*previous, detail="from the interrupted run")
        if mutant.line in planned.suppressed:
            return Outcome(MutantState.IGNORED, detail="line suppressed by a mutate-off comment or arid pattern")
        if mutant.id in self.config.ignored_mutants:
            return Outcome(MutantState.IGNORED, detail=f"ignored in {CONFIG_FILE}")
        if self.options.skip_killed and mutant.id in self.killed:
            return None
        return self.evaluate(test, planned, timeout, label)

    def run_test(self, test: str, events: EventLog) -> list[MutantResult]:
        timeout = self.run_baseline(test, events)
        if timeout is None:
            return []
        coverage = self.coverage.collect()
        self.verify_coverage_data(test, coverage)
        planned = self.plan_mutants(self.source_files(coverage))
        self.console.plan([entry.mutant for entry in planned])
        width = len(str(len(planned)))
        results: list[MutantResult] = []
        for number, entry in enumerate(planned, start=1):
            progress = f"[{number:>{width}}/{len(planned)}]"
            label = f"  {progress} {describe_mutant(entry.mutant)}"
            self.console.mutant_started(number - 1, entry.source.text, entry.mutant)
            outcome = self.outcome_for(test, entry, timeout, events, label)
            if outcome is None:
                self.show_mutant(progress, entry.mutant, "skipped", "already killed by an earlier test")
                self.console.mutant_finished(number - 1, "skipped")
                continue
            function = entry.source.coverage.function_at(entry.mutant.line)
            results.append(MutantResult(entry.mutant, test, outcome.state, outcome.survivor, function))
            self.record(results[-1], events)
            self.show_mutant(progress, entry.mutant, outcome.state, outcome.detail)
            self.console.mutant_finished(number - 1, outcome.state)
        self.rebuild_if_mutated()
        return results

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
        if result.state in DETECTED_STATES:
            self.killed.add(mutant.id)
        events.emit(
            "mutant", test=result.test, id=mutant.id, file=mutant.file, line=mutant.line, column=mutant.column,
            operator=mutant.operator, original=mutant.original, replacement=mutant.replacement,
            state=result.state, survivor=result.survivor,
        )

    def show_mutant(self, progress: str, mutant: Mutant, state: str, detail: str) -> None:
        console = self.console
        console.line(
            f"  {console.paint(progress, DIM)} {console.state(state)} {describe_mutant(mutant)}"
            f"  {console.paint(f'{detail}  id {mutant.id}', DIM)}"
        )

    def write_report(self, combined: Sequence[CombinedResult]) -> Path:
        sources = {
            entry.mutant.file: (self.source_dir / entry.mutant.file).read_bytes().decode(SOURCE_ENCODING, SOURCE_ERRORS)
            for entry in combined
        }
        report_path = self.options.report_dir / "report.json"
        report_path.write_text(json.dumps(mutation_testing_elements(combined, sources), indent=2, ensure_ascii=False))
        return report_path

    def show_survivors(self, combined: Sequence[CombinedResult]) -> None:
        survivors = [entry for entry in combined if entry.state is MutantState.SURVIVED]
        if survivors:
            self.console.heading(f"Survivors ({len(survivors)})")
            for entry in survivors:
                self.console.line(compiler_warning(entry, self.source_dir))

    def show_action_items(self, combined: Sequence[CombinedResult]) -> None:
        items = action_items(combined, limit=None if self.options.show_all else DEFAULT_ACTION_ITEMS)
        if items:
            self.console.heading("Action items")
            for number, item in enumerate(items, start=1):
                self.console.line(f"  {number}. {item}")

    def show_summary(self, combined: Sequence[CombinedResult], score: float | None, report_path: Path) -> None:
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
        console.line(f"  {console.paint('mutation score', BOLD)} {console.score(percent)}{self.threshold_note(percent)}")
        console.field("report", str(report_path), 8)
        console.field("events", str(self.options.report_dir / "events.jsonl"), 8)

    def threshold_note(self, percent: float | None) -> str:
        threshold = self.options.threshold
        if threshold is None or percent is None:
            return ""
        if percent < threshold:
            return "   " + self.console.paint(f"✗ below --threshold {threshold:g}%", BOLD + RED)
        return "   " + self.console.paint(f"✓ meets --threshold {threshold:g}%", DIM)

    def run_tests(self, tests: Sequence[str], events: EventLog) -> list[MutantResult]:
        width = max(len(test) for test in tests)
        results: list[MutantResult] = []
        for number, test in enumerate(tests, start=1):
            self.console.heading(f"Test {number}/{len(tests)}: {test}")
            self.console.test_started(test, number, len(tests))
            test_results = self.run_test(test, events)
            results += test_results
            self.console.line(f"  {self.console.paint(summary_line(test, test_results, width), BOLD)}")
        return results

    def require_mutants(self, combined: Sequence[CombinedResult]) -> None:
        """Without --since, a run that mutated nothing tested nothing, so it must not pass."""
        if combined or self.options.since is not None:
            return
        raise StrainError(
            "no mutant was generated for any test, so nothing was mutation tested; check that the tests pass, that "
            f"the build writes coverage data and that {CONFIG_FILE} does not exclude every source"
        )

    def run(self) -> int:
        start = time.monotonic()
        self.show_banner()
        self.prepare()
        tests = ctest.list_tests(self.build_dir, self.options.include, self.options.exclude)
        if not tests:
            raise StrainError("no tests selected")
        self.console.step(f"selected {plural(len(tests), 'test')}: {', '.join(tests)}")
        with EventLog(self.options.report_dir / "events.jsonl", self.out if self.options.ui is UiMode.JSON else None,
                      self.options.resume) as events:
            if self.options.resume:
                self.console.step(f"resuming: {plural(len(events.previous_results()), 'earlier result')} reused")
            combined = combine(self.run_tests(tests, events))
            self.require_mutants(combined)
            score = mutation_score(entry.state for entry in combined)
            report_path = self.write_report(combined)
            events.emit("finished", score=score)
        self.show_survivors(combined)
        self.show_action_items(combined)
        self.show_summary(combined, score, report_path)
        self.console.line(f"\n{self.console.paint(f'finished in {time.monotonic() - start:.1f}s', DIM)}")
        threshold = self.options.threshold
        return 1 if threshold is not None and score is not None and score * 100 < threshold else 0


def run(options: RunOptions, out: TextIO, err: TextIO) -> int:
    runner = Runner(options, out, err)
    try:
        return runner.run()
    finally:
        runner.console.finish()
