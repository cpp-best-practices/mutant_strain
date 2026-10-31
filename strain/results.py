"""Result states, classification of test runs and the mutation score."""

import enum
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .ctest import RunResult
from .mutators import Mutant

SANITIZER_REPORT = re.compile(
    r"ERROR: (?:Address|Leak|Memory|Thread)Sanitizer|runtime error: |SUMMARY: UndefinedBehaviorSanitizer"
)


class MutantState(enum.StrEnum):
    KILLED = "killed"
    KILLED_BY_SANITIZER = "killed_by_sanitizer"
    TIMEOUT = "timeout"
    SURVIVED = "survived"
    SKIPPED_UNEVALUATED = "skipped_unevaluated"
    COMPILE_ERROR = "compile_error"
    IGNORED = "ignored"


class SurvivorKind(enum.StrEnum):
    NOT_REACHED = "not_reached"
    NOT_DETECTED = "not_detected"


DETECTED_STATES = frozenset({MutantState.KILLED, MutantState.KILLED_BY_SANITIZER, MutantState.TIMEOUT})
SCORED_STATES = DETECTED_STATES | {MutantState.SURVIVED}

# Lower index wins when one mutant has different results under different tests.
STATE_STRENGTH = tuple(MutantState)


def has_sanitizer_report(output: str) -> bool:
    return SANITIZER_REPORT.search(output) is not None


def classify_run(run: RunResult) -> MutantState:
    if run.timed_out:
        return MutantState.TIMEOUT
    if has_sanitizer_report(run.output):
        return MutantState.KILLED_BY_SANITIZER
    if run.returncode != 0:
        return MutantState.KILLED
    return MutantState.SURVIVED


def baseline_problem(run: RunResult) -> str | None:
    if run.timed_out:
        return "times out without mutation"
    if has_sanitizer_report(run.output):
        return "reports a sanitizer error without mutation"
    if run.returncode != 0:
        return "fails without mutation"
    return None


def mutation_score(states: Iterable[MutantState]) -> float | None:
    scored = [state for state in states if state in SCORED_STATES]
    if not scored:
        return None
    return sum(state in DETECTED_STATES for state in scored) / len(scored)


@dataclass(frozen=True, slots=True)
class MutantResult:
    mutant: Mutant
    test: str
    state: MutantState
    survivor: SurvivorKind | None = None
    function: str | None = None


@dataclass(frozen=True, slots=True)
class CombinedResult:
    mutant: Mutant
    state: MutantState
    survivor: SurvivorKind | None
    function: str | None
    killed_by: tuple[str, ...]
    covered_by: tuple[str, ...]


def _combine_one(results: Sequence[MutantResult]) -> CombinedResult:
    state = min((result.state for result in results), key=STATE_STRENGTH.index)
    survivor_kinds = {result.survivor for result in results if result.state is MutantState.SURVIVED}
    survivor = None
    if state is MutantState.SURVIVED and survivor_kinds - {None}:
        survivor = SurvivorKind.NOT_DETECTED if SurvivorKind.NOT_DETECTED in survivor_kinds else SurvivorKind.NOT_REACHED
    return CombinedResult(
        mutant=results[0].mutant,
        state=state,
        survivor=survivor,
        function=next((result.function for result in results if result.function is not None), None),
        killed_by=tuple(result.test for result in results if result.state in DETECTED_STATES),
        covered_by=tuple(dict.fromkeys(result.test for result in results)),
    )


def combine(results: Iterable[MutantResult]) -> list[CombinedResult]:
    by_mutant: dict[str, list[MutantResult]] = {}
    for result in results:
        by_mutant.setdefault(result.mutant.id, []).append(result)
    combined = [_combine_one(group) for group in by_mutant.values()]
    return sorted(combined, key=lambda entry: (entry.mutant.file, entry.mutant.offset))
