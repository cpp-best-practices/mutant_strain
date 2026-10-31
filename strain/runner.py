"""The per-test strategy: baseline, generate mutants on covered lines, mutate, rebuild, run, restore."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from . import ctest, git
from .build import CompilationDatabase, build
from .config import Config, load_config
from .coverage import FileCoverage, GcovCoverage
from .journal import Journal
from .mutators import Mutant, apply_mutant, generate_mutants, suppressed_lines
from .report import EventLog, action_items, compiler_warning, mutation_testing_elements, summary_line
from .results import (
    DETECTED_STATES,
    MutantResult,
    MutantState,
    SurvivorKind,
    baseline_problem,
    classify_run,
    combine,
    mutation_score,
)
from .safety_nets import Level, describe_missing, missing_safety_nets, resolve_levels

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
    safety_nets: Level | None = None
    threshold: float | None = None
    strict: bool = False
    max_per_line: int | None = None
    resume: bool = False
    show_all: bool = False
    json_ui: bool = False
    report_dir: Path = Path("strain-report")


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: Path
    relative: str
    text: str
    coverage: FileCoverage


def cmake_source_dir(build_dir: Path) -> Path:
    cache = build_dir / "CMakeCache.txt"
    if not cache.is_file():
        raise StrainError(f"{build_dir} is not a CMake build directory (no CMakeCache.txt)")
    for line in cache.read_text(errors="replace").splitlines():
        if line.startswith("CMAKE_HOME_DIRECTORY:"):
            return Path(line.partition("=")[2]).resolve()
    raise StrainError(f"{cache} does not name a source directory")


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
        self.err = err
        self.build_dir = options.build_dir.resolve()
        self.source_dir = cmake_source_dir(self.build_dir)
        self.journal = Journal(self.build_dir / "strain-journal")
        self.coverage = GcovCoverage(self.build_dir)
        self.config = Config()
        self.database = CompilationDatabase([])
        self.since: dict[str, set[int]] | None = None
        self.killed: set[str] = set()
        self.build_is_mutated = False

    def log(self, message: str) -> None:
        if not self.options.json_ui:
            print(message, file=self.out, flush=True)

    def warn(self, message: str) -> None:
        print(f"strain: warning: {message}", file=self.err, flush=True)

    def prepare(self) -> None:
        for path in self.journal.recover():
            self.warn(f"restored {path} from an interrupted run")
        if not git.is_clean(self.source_dir):
            raise StrainError(f"strain needs a clean working tree: commit or stash the changes in {self.source_dir}")
        self.config = load_config(self.source_dir)
        self.database = CompilationDatabase.load(self.build_dir)
        self.check_safety_nets()
        if not build(self.build_dir):
            raise StrainError(f"`cmake --build {self.build_dir}` fails before any mutation")
        if self.options.since is not None:
            self.since = git.changed_lines(self.source_dir, self.options.since)

    def is_project_source(self, path: Path) -> bool:
        return path.is_relative_to(self.source_dir) and not path.is_relative_to(self.build_dir)

    def check_safety_nets(self) -> None:
        levels = resolve_levels(self.config.safety_nets, self.options.safety_nets)
        commands = [command for command in self.database.commands if self.is_project_source(command.file)]
        errors: list[str] = []
        for net, files in missing_safety_nets(commands).items():
            message = describe_missing(net, files)
            if levels[net] is Level.ERROR:
                errors.append(message)
            elif levels[net] is Level.WARN:
                self.warn(message)
        if errors:
            raise StrainError("\n".join([*errors, "rebuild with the flags from the README or pass --safety-nets=warn"]))

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

    def mutants_for(self, source: SourceFile) -> list[Mutant]:
        lines = source.coverage.executed_lines()
        if self.since is not None:
            lines &= self.since.get(source.relative, set())
        return generate_mutants(source.relative, source.text, lines, self.options.max_per_line)

    def evaluate(self, test: str, source: SourceFile, mutant: Mutant, timeout: float) -> MutantState:
        mutated = apply_mutant(source.text, mutant).encode(SOURCE_ENCODING, SOURCE_ERRORS)
        self.build_is_mutated = True
        with self.journal.mutated(source.path, mutated):
            command = self.database.command_for(source.path)
            if command is not None and not command.passes_syntax_check():
                return MutantState.COMPILE_ERROR
            if not build(self.build_dir):
                return MutantState.COMPILE_ERROR
            return classify_run(ctest.run_test(self.build_dir, test, timeout))

    def run_baseline(self, test: str, events: EventLog) -> float | None:
        self.coverage.reset()
        baseline = ctest.run_test(self.build_dir, test, BASELINE_TIMEOUT_SECONDS)
        problem = baseline_problem(baseline)
        events.emit("baseline", test=test, duration=baseline.duration, problem=problem)
        if problem is None:
            return max(MINIMUM_TIMEOUT_SECONDS, baseline.duration * TIMEOUT_FACTOR)
        if self.options.strict:
            raise StrainError(f"test {test} {problem}; excluded tests are fatal with --strict")
        self.warn(f"test {test} {problem}; excluding it")
        return None

    def mutant_state(
        self, test: str, source: SourceFile, mutant: Mutant, suppressed: set[int], timeout: float, events: EventLog
    ) -> tuple[MutantState, SurvivorKind | None] | None:
        previous = events.previous_results().get((test, mutant.id))
        if previous is not None:
            return previous
        if mutant.line in suppressed or mutant.id in self.config.ignored_mutants:
            return MutantState.IGNORED, None
        if self.options.skip_killed and mutant.id in self.killed:
            return None
        state = self.evaluate(test, source, mutant, timeout)
        return state, survivor_kind(source.coverage, mutant.line) if state is MutantState.SURVIVED else None

    def run_test(self, test: str, events: EventLog) -> list[MutantResult]:
        timeout = self.run_baseline(test, events)
        if timeout is None:
            return []
        results: list[MutantResult] = []
        for source in self.source_files(self.coverage.collect()):
            suppressed = suppressed_lines(source.text, self.config.arid)
            for mutant in self.mutants_for(source):
                outcome = self.mutant_state(test, source, mutant, suppressed, timeout, events)
                if outcome is None:
                    continue
                state, survivor = outcome
                results.append(MutantResult(mutant, test, state, survivor, source.coverage.function_at(mutant.line)))
                self.record(results[-1], events)
        self.rebuild_if_mutated()
        return results

    def rebuild_if_mutated(self) -> None:
        if self.build_is_mutated and not build(self.build_dir):
            raise StrainError(f"`cmake --build {self.build_dir}` fails after restoring the sources")
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
        self.log(
            f"  {mutant.id} {mutant.file}:{mutant.line}:{mutant.column} [{mutant.operator}] "
            f"`{mutant.original}` → `{mutant.replacement}`: {result.state}"
        )

    def write_report(self, results: Sequence[MutantResult]) -> float | None:
        combined = combine(results)
        sources = {
            entry.mutant.file: (self.source_dir / entry.mutant.file).read_bytes().decode(SOURCE_ENCODING, SOURCE_ERRORS)
            for entry in combined
        }
        report_path = self.options.report_dir / "report.json"
        report_path.write_text(json.dumps(mutation_testing_elements(combined, sources), indent=2, ensure_ascii=False))
        for entry in combined:
            if entry.state is MutantState.SURVIVED:
                self.log(compiler_warning(entry, self.source_dir))
        items = action_items(combined, limit=None if self.options.show_all else DEFAULT_ACTION_ITEMS)
        if items:
            self.log("\nAction items")
            for number, item in enumerate(items, start=1):
                self.log(f"  {number}. {item}")
        return mutation_score(entry.state for entry in combined)

    def run(self) -> int:
        self.prepare()
        tests = ctest.list_tests(self.build_dir, self.options.include, self.options.exclude)
        if not tests:
            raise StrainError("no tests selected")
        width = max(len(test) for test in tests)
        results: list[MutantResult] = []
        with EventLog(self.options.report_dir / "events.jsonl", self.out if self.options.json_ui else None,
                      self.options.resume) as events:
            for test in tests:
                self.log(f"{test}: running")
                test_results = self.run_test(test, events)
                results += test_results
                self.log(summary_line(test, test_results, width))
            score = self.write_report(results)
            events.emit("finished", score=score)
        self.log(f"\nmutation score {'n/a' if score is None else f'{score * 100:.1f}%'}")
        threshold = self.options.threshold
        return 1 if threshold is not None and score is not None and score * 100 < threshold else 0


def run(options: RunOptions, out: TextIO, err: TextIO) -> int:
    return Runner(options, out, err).run()
