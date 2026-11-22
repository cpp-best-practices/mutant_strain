"""Survivors grouped into the few test-writing actions that would kill most of them.

One survivor at a time says what to test; a group says what kind of test is missing. Twelve `&&` → `||` survivors in
one file mean the tests check only what is accepted, never the lookalikes that must be rejected.

Each survivor belongs to one primary pattern at most: the kind of change it makes, in its file. Hotspots, the
functions with the most survivors, cut across those, so they are listed apart and are not primary.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .results import CombinedResult, MutantState, SurvivorKind

# A pattern needs at least this many survivors; a single one is just a survivor.
MINIMUM_SURVIVORS = 2
HOTSPOTS = 5


@dataclass(frozen=True, slots=True)
class Family:
    """A kind of change, with what the tests are missing when mutants of that kind survive."""

    kind: str
    change: str
    advice: str


BOUNDARY = Family("boundary", "move a comparison's limit by one", "add a test exactly at the limit")
NEGATION = Family("negated-comparison", "invert a comparison", "add a test on each side of the comparison")
EQUALITY = Family("equality", "swap `==` and `!=`", "add a test where the values are equal and one where they differ")
AND_TO_OR = Family(
    "and-to-or",
    "turn `&&` into `||`",
    "add inputs that must be rejected, since the tests check only what is accepted",
)
OR_TO_AND = Family("or-to-and", "turn `||` into `&&`", "add an input that satisfies only one of the alternatives")
ARITHMETIC = Family(
    "arithmetic", "change an arithmetic operator", "assert the computed values, not just that there are results"
)
FAMILIES = (BOUNDARY, NEGATION, EQUALITY, AND_TO_OR, OR_TO_AND, ARITHMETIC)

BOUNDARY_CHANGES = frozenset({("<", "<="), ("<=", "<"), (">", ">="), (">=", ">")})
NEGATION_CHANGES = frozenset({("<", ">="), (">=", "<"), (">", "<="), ("<=", ">")})


def family_of(entry: CombinedResult) -> Family | None:
    change = (entry.mutant.original, entry.mutant.replacement)
    if change in BOUNDARY_CHANGES:
        return BOUNDARY
    if change in NEGATION_CHANGES:
        return NEGATION
    if change in {("==", "!="), ("!=", "==")}:
        return EQUALITY
    if change == ("&&", "||"):
        return AND_TO_OR
    if change == ("||", "&&"):
        return OR_TO_AND
    if entry.mutant.operator in ("aor", "asr"):
        return ARITHMETIC
    return None


def file_name(file: str) -> str:
    return PurePosixPath(file).name


@dataclass(frozen=True, slots=True)
class Pattern:
    family: Family
    file: str
    mutant_ids: tuple[str, ...]

    def summary(self) -> str:
        """`12 survivors turn `&&` into `||` in parser.cpp: add inputs that must be rejected…`"""
        return f"{len(self.mutant_ids)} survivors {self.family.change} in {file_name(self.file)}: {self.family.advice}"


@dataclass(frozen=True, slots=True)
class Hotspot:
    function: str
    file: str
    mutant_ids: tuple[str, ...]
    not_reached: int
    not_detected: int

    def summary(self) -> str:
        """`clamp() in math.cpp has 3 survivors: 2 not_reached, 1 not_detected`"""
        split = [
            f"{count} {kind}"
            for kind, count in (("not_reached", self.not_reached), ("not_detected", self.not_detected))
            if count
        ]
        name = f"{self.function.partition('(')[0]}()"
        detail = f": {', '.join(split)}" if split else ""
        return f"{name} in {file_name(self.file)} has {len(self.mutant_ids)} survivors{detail}"


def survivors(combined: Sequence[CombinedResult]) -> list[CombinedResult]:
    return [entry for entry in combined if entry.state is MutantState.SURVIVED]


def find_patterns(combined: Sequence[CombinedResult]) -> list[Pattern]:
    """The primary patterns, largest first: survivors that make the same kind of change in the same file."""
    groups: dict[tuple[Family, str], list[str]] = {}
    for entry in survivors(combined):
        family = family_of(entry)
        if family is not None:
            groups.setdefault((family, entry.mutant.file), []).append(entry.mutant.id)
    patterns = [
        Pattern(family, file, tuple(ids)) for (family, file), ids in groups.items() if len(ids) >= MINIMUM_SURVIVORS
    ]
    return sorted(
        patterns, key=lambda pattern: (-len(pattern.mutant_ids), pattern.file, FAMILIES.index(pattern.family))
    )


def find_hotspots(combined: Sequence[CombinedResult], limit: int = HOTSPOTS) -> list[Hotspot]:
    """The functions with the most survivors, and how many need a new input or a stronger assertion."""
    groups: dict[tuple[str, str], list[CombinedResult]] = {}
    for entry in survivors(combined):
        if entry.function is not None:
            groups.setdefault((entry.function, entry.mutant.file), []).append(entry)
    hotspots = [
        Hotspot(
            function,
            file,
            tuple(entry.mutant.id for entry in entries),
            sum(entry.survivor is SurvivorKind.NOT_REACHED for entry in entries),
            sum(entry.survivor is SurvivorKind.NOT_DETECTED for entry in entries),
        )
        for (function, file), entries in groups.items()
        if len(entries) >= MINIMUM_SURVIVORS
    ]
    return sorted(hotspots, key=lambda hotspot: (-len(hotspot.mutant_ids), hotspot.file, hotspot.function))[:limit]


def patterns_json(combined: Sequence[CombinedResult]) -> list[dict[str, Any]]:
    """The patterns for report.json; `primary` tells the patterns that share no survivor from the hotspots."""
    return [
        *(
            {
                "kind": pattern.family.kind,
                "primary": True,
                "file": pattern.file,
                "survivors": len(pattern.mutant_ids),
                "summary": pattern.summary(),
                "advice": pattern.family.advice,
                "mutants": list(pattern.mutant_ids),
            }
            for pattern in find_patterns(combined)
        ),
        *(
            {
                "kind": "hotspot",
                "primary": False,
                "file": hotspot.file,
                "function": hotspot.function,
                "survivors": len(hotspot.mutant_ids),
                "notReached": hotspot.not_reached,
                "notDetected": hotspot.not_detected,
                "summary": hotspot.summary(),
                "mutants": list(hotspot.mutant_ids),
            }
            for hotspot in find_hotspots(combined)
        ),
    ]


def pattern_lines(combined: Sequence[CombinedResult]) -> list[str]:
    """One line per pattern, then per hotspot, for the console, summary.md and the HTML report."""
    return [
        *(pattern.summary() for pattern in find_patterns(combined)),
        *(f"hotspot: {hotspot.summary()}" for hotspot in find_hotspots(combined)),
    ]
