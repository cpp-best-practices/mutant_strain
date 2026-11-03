"""Console summaries, action items, `report.json` and `events.jsonl`."""

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self, TextIO

from .mutators import Mutant
from .results import (
    DETECTED_STATES,
    CombinedResult,
    MutantResult,
    MutantState,
    SurvivorKind,
    SCORED_STATES,
    mutation_score,
)

MUTATION_TESTING_ELEMENTS_STATUS = {
    MutantState.KILLED: "Killed",
    MutantState.KILLED_BY_SANITIZER: "Killed",
    MutantState.TIMEOUT: "Timeout",
    MutantState.SURVIVED: "Survived",
    MutantState.SKIPPED_UNEVALUATED: "Ignored",
    MutantState.COMPILE_ERROR: "CompileError",
    MutantState.IGNORED: "Ignored",
}
NOT_REACHED_HINT = "add a test input that takes the other branch"
TAB_WIDTH = 4
EXCERPT_GUTTER = 5


def format_score(score: float | None) -> str:
    return "n/a" if score is None else f"{score * 100:.1f}%"


def summary_line(test: str, results: Sequence[MutantResult], width: int) -> str:
    states = [result.state for result in results]
    return (
        f"{test:<{width}}   score {format_score(mutation_score(states))}"
        f"   killed {states.count(MutantState.KILLED)}"
        f"  sanitizer {states.count(MutantState.KILLED_BY_SANITIZER)}"
        f"  timeout {states.count(MutantState.TIMEOUT)}"
        f"  survived {states.count(MutantState.SURVIVED)}"
    )


def _change(entry: CombinedResult) -> str:
    return f"`{entry.mutant.original}` → `{entry.mutant.replacement}`"


def _survivor_label(entry: CombinedResult) -> str:
    return "" if entry.survivor is None else f" ({entry.survivor})"


def compiler_warning(entry: CombinedResult, source_root: Path) -> str:
    mutant = entry.mutant
    return (
        f"{source_root / mutant.file}:{mutant.line}:{mutant.column}: warning: "
        f"{entry.state} [{mutant.operator}] {_change(entry)}{_survivor_label(entry)}"
    )


def source_excerpt(mutant: Mutant, source: str) -> list[str]:
    """The mutated line with a caret under the operator and the replacement below it, as GCC shows a fix-it."""
    lines = source.splitlines()
    if not 1 <= mutant.line <= len(lines):
        return []
    text = lines[mutant.line - 1]
    indent = " " * len(text[: mutant.column - 1].expandtabs(TAB_WIDTH))
    gutter = " " * EXCERPT_GUTTER
    return [
        f"{mutant.line:>{EXCERPT_GUTTER}} | {text.expandtabs(TAB_WIDTH)}",
        f"{gutter} | {indent}^{'~' * (len(mutant.original) - 1)}",
        f"{gutter} | {indent}{mutant.replacement}",
    ]


@dataclass(frozen=True, slots=True)
class FileScore:
    file: str
    mutants: int
    detected: int
    survived: int

    @property
    def score(self) -> float | None:
        scored = self.detected + self.survived
        return None if scored == 0 else self.detected / scored


def file_scores(combined: Iterable[CombinedResult]) -> list[FileScore]:
    by_file: dict[str, list[MutantState]] = {}
    for entry in combined:
        by_file.setdefault(entry.mutant.file, []).append(entry.state)
    return [
        FileScore(
            file=file,
            mutants=len(states),
            detected=sum(state in DETECTED_STATES for state in states),
            survived=states.count(MutantState.SURVIVED),
        )
        for file, states in sorted(by_file.items())
    ]


def hint(entry: CombinedResult) -> str:
    if entry.survivor is SurvivorKind.NOT_REACHED:
        return NOT_REACHED_HINT
    if entry.mutant.operator == "aor":
        return "assert the computed value"
    if entry.mutant.operator == "asr":
        return "assert the updated value"
    return f"add a test where `{entry.mutant.original}` and `{entry.mutant.replacement}` give different results"


def _short_function_name(function: str) -> str:
    return f"{function.partition('(')[0]}()"


def _pseudo_tested_functions(scored: Sequence[CombinedResult]) -> list[str]:
    detected: dict[str, bool] = {}
    for entry in scored:
        if entry.function is not None:
            detected[entry.function] = detected.get(entry.function, False) or entry.state is not MutantState.SURVIVED
    return [function for function, killed in detected.items() if not killed]


def _tests_that_kill_nothing(scored: Sequence[CombinedResult]) -> list[str]:
    covering = dict.fromkeys(test for entry in scored for test in entry.covered_by)
    killing = {test for entry in scored for test in entry.killed_by}
    return [test for test in covering if test not in killing]


def action_items(combined: Sequence[CombinedResult], limit: int | None) -> list[str]:
    scored = [entry for entry in combined if entry.state in SCORED_STATES]
    items = [
        *(f"{_short_function_name(function)} is pseudo-tested: nothing checks its result"
          for function in _pseudo_tested_functions(scored)),
        *(f"test {test} kills nothing: its assertions never fail on a mutant" for test in _tests_that_kill_nothing(scored)),
        *(f"{entry.mutant.file}:{entry.mutant.line} {_change(entry)} survived{_survivor_label(entry)}: {hint(entry)}"
          for entry in scored if entry.state is MutantState.SURVIVED),
    ]
    return items if limit is None else items[:limit]


def _markdown_cell(text: str) -> str:
    return text.replace("|", "\\|")


def _markdown_threshold(score: float | None, threshold: float | None) -> str:
    if threshold is None or score is None:
        return ""
    if score * 100 < threshold:
        return f" ❌ below the {threshold:g}% threshold"
    return f" ✅ meets the {threshold:g}% threshold"


def _markdown_counts(combined: Sequence[CombinedResult]) -> str:
    states = [entry.state for entry in combined]
    counts = ", ".join(f"{state} {states.count(state)}" for state in MutantState if state in states)
    return f"{len(combined)} mutant{'' if len(combined) == 1 else 's'}: {counts}"


def _markdown_file_table(combined: Sequence[CombinedResult]) -> list[str]:
    return [
        "| File | Mutants | Detected | Survived | Score |",
        "| --- | ---: | ---: | ---: | ---: |",
        *(
            f"| `{_markdown_cell(row.file)}` | {row.mutants} | {row.detected} | {row.survived} | {format_score(row.score)} |"
            for row in file_scores(combined)
        ),
    ]


def _markdown_survivors(combined: Sequence[CombinedResult]) -> list[str]:
    survivors = [entry for entry in combined if entry.state is MutantState.SURVIVED]
    if not survivors:
        return []
    return [
        "",
        f"### Survivors ({len(survivors)})",
        "",
        *(
            f"- `{entry.mutant.file}:{entry.mutant.line}:{entry.mutant.column}` "
            f"{_change(entry)}{_survivor_label(entry)}: {hint(entry)}"
            for entry in survivors
        ),
    ]


def markdown_summary(combined: Sequence[CombinedResult], threshold: float | None) -> str:
    """A summary for CI pages such as `$GITHUB_STEP_SUMMARY` or a pull request comment."""
    heading = ["## strain mutation testing", ""]
    if not combined:
        return "\n".join([*heading, "No mutants were generated."]) + "\n"
    score = mutation_score(entry.state for entry in combined)
    lines = [
        *heading,
        f"**Mutation score: {format_score(score)}**{_markdown_threshold(score, threshold)}",
        "",
        _markdown_counts(combined),
        "",
        *_markdown_file_table(combined),
        *_markdown_survivors(combined),
    ]
    return "\n".join(lines) + "\n"


def _mutant_element(entry: CombinedResult) -> dict[str, Any]:
    mutant = entry.mutant
    return {
        "id": mutant.id,
        "mutatorName": mutant.operator,
        "replacement": mutant.replacement,
        "location": {
            "start": {"line": mutant.line, "column": mutant.column},
            "end": {"line": mutant.line, "column": mutant.end_column},
        },
        "status": MUTATION_TESTING_ELEMENTS_STATUS[entry.state],
        "statusReason": str(entry.state),
        "killedBy": list(entry.killed_by),
        "coveredBy": list(entry.covered_by),
    }


def mutation_testing_elements(combined: Iterable[CombinedResult], sources: Mapping[str, str]) -> dict[str, Any]:
    files: dict[str, Any] = {}
    for entry in combined:
        file_report = files.setdefault(
            entry.mutant.file, {"language": "cpp", "source": sources[entry.mutant.file], "mutants": []}
        )
        file_report["mutants"].append(_mutant_element(entry))
    return {"schemaVersion": "1", "thresholds": {"high": 80, "low": 60}, "files": files}


PreviousResults = dict[tuple[str, str], tuple[MutantState, SurvivorKind | None]]


class EventLog:
    def __init__(self, path: Path, echo: TextIO | None = None, resume: bool = False) -> None:
        self.path = path
        self.echo = echo
        self.resume = resume
        self._previous: PreviousResults = {}
        self._file: TextIO | None = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.resume and self.path.is_file():
            self._previous = self._read_results()
        self._file = self.path.open("a" if self.resume else "w", encoding="utf-8")
        return self

    def __exit__(
        self, kind: type[BaseException] | None, value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def _read_results(self) -> PreviousResults:
        results: PreviousResults = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event["event"] == "mutant":
                survivor = event.get("survivor")
                results[(event["test"], event["id"])] = (
                    MutantState(event["state"]),
                    None if survivor is None else SurvivorKind(survivor),
                )
        return results

    def previous_results(self) -> PreviousResults:
        return self._previous

    def emit(self, event: str, **fields: Any) -> None:
        if self._file is None:
            raise RuntimeError("EventLog used outside its `with` block")
        line = json.dumps({"event": event, **fields}, ensure_ascii=False)
        self._file.write(line + "\n")
        self._file.flush()
        if self.echo is not None:
            print(line, file=self.echo, flush=True)
