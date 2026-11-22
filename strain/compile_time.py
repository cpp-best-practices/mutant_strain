"""Code that only the compiler runs: constexpr and consteval functions evaluated in constant expressions.

gcov counts what runs, so a constexpr function checked only by `static_assert` has no executed lines and is never
mutated, and a header used only that way doesn't appear in the coverage data at all. strain finds these functions in
the sources instead, says which of their lines no test runs, and with --compile-time mutates them: a mutant that
makes a `static_assert` or a constant evaluation fail is killed at compile time.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .lexer import Token, TokenKind, tokenize

CONSTANT_KEYWORDS = frozenset({"constexpr", "consteval"})
# Diagnostics of a compile-time check that failed, from GCC and Clang. Anything else that stops the build means the
# mutant is simply invalid code: a compile error.
COMPILE_TIME_CHECK_FAILED = re.compile(
    r"static assertion failed|static_assert failed|non-constant condition for static assertion"
    r"|is not a constant expression|not an integral constant expression|must be initialized by a constant expression"
)
KILLED_AT_COMPILE_TIME = "killed at compile time"
# The name results get for the mutants --compile-time builds: the compile-time checks are what tests them.
COMPILE_TIME_TEST = "compile-time checks"
NOT_MUTATED_HEADING = "Not mutated: evaluated at compile time only"
NOT_MUTATED_HINT = (
    "no test runs these constexpr lines; --compile-time mutates them, and a failed static_assert kills them"
)


@dataclass(frozen=True, slots=True)
class ConstantFunction:
    name: str
    keyword: str
    # The lines with code between the function's braces.
    body_lines: frozenset[int]


def compile_time_check_failure(diagnostics: str) -> str | None:
    """The diagnostic line of the compile-time check that failed, or None when the build failed for another reason."""
    for line in diagnostics.splitlines():
        if COMPILE_TIME_CHECK_FAILED.search(line):
            return line.strip()
    return None


def _matching(tokens: list[Token], start: int, opening: str, closing: str) -> int | None:
    depth = 0
    for index in range(start, len(tokens)):
        if tokens[index].text == opening:
            depth += 1
        elif tokens[index].text == closing:
            depth -= 1
            if depth == 0:
                return index
    return None


def _body_start(tokens: list[Token], start: int) -> tuple[str, int] | None:
    """After a constexpr or consteval keyword: the function's name and opening brace, or None for anything else.

    A variable reaches `=` or `;` first, or opens a brace before any parameter list; `if constexpr` is not a
    declaration at all.
    """
    name: str | None = None
    index = start
    while index < len(tokens):
        token = tokens[index]
        if token.text == ";" or (token.text == "=" and tokens[index - 1].text != "operator"):
            return None
        if token.text == "(":
            if name is None:
                previous = tokens[index - 1]
                name = previous.text if previous.kind is TokenKind.IDENTIFIER else "operator"
            closing = _matching(tokens, index, "(", ")")
            if closing is None:
                return None
            index = closing
        elif token.text == "{":
            return None if name is None else (name, index)
        index += 1
    return None


def constant_functions(source: str) -> list[ConstantFunction]:
    tokens = [token for token in tokenize(source) if token.kind is not TokenKind.COMMENT]
    functions: list[ConstantFunction] = []
    for index, token in enumerate(tokens):
        if token.text not in CONSTANT_KEYWORDS or (index > 0 and tokens[index - 1].text == "if"):
            continue
        found = _body_start(tokens, index + 1)
        if found is None:
            continue
        name, opening = found
        closing = _matching(tokens, opening, "{", "}")
        if closing is None:
            continue
        lines = frozenset(inner.line for inner in tokens[opening + 1 : closing])
        functions.append(ConstantFunction(name, token.text, lines))
    return functions


def constant_lines(source: str) -> dict[int, str]:
    """Each line inside a constexpr or consteval function, with the innermost function's name."""
    lines: dict[int, str] = {}
    # Outer functions first, so a lambda or local function inside one names its own lines.
    for function in sorted(constant_functions(source), key=lambda function: -len(function.body_lines)):
        lines.update(dict.fromkeys(function.body_lines, function.name))
    return lines


@dataclass(frozen=True, slots=True)
class NotMutated:
    """Lines of a constexpr or consteval function that hold mutants but that no test runs, so none was tested."""

    file: str
    function: str
    lines: tuple[int, ...]
    mutants: int

    @property
    def location(self) -> str:
        first, last = self.lines[0], self.lines[-1]
        return f"{self.file}:{first}" if first == last else f"{self.file}:{first}-{last}"

    def summary(self) -> str:
        """`src/limits.hpp:6-7 clamp_index(): 3 mutants on 2 lines`"""
        mutants = f"{self.mutants} mutant{'' if self.mutants == 1 else 's'}"
        lines = f"{len(self.lines)} line{'' if len(self.lines) == 1 else 's'}"
        return f"{self.location} {self.function}(): {mutants} on {lines}"


def not_mutated(found: "Sequence[tuple[Any, str]]") -> list[NotMutated]:
    """Groups (planned mutant, function) pairs by file and function."""
    groups: dict[tuple[str, str], list[int]] = {}
    for planned, function in found:
        groups.setdefault((planned.mutant.file, function), []).append(planned.mutant.line)
    return [
        NotMutated(file, function, tuple(sorted(set(lines))), len(lines))
        for (file, function), lines in sorted(groups.items(), key=lambda item: (item[0][0], min(item[1])))
    ]
