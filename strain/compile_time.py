"""Code evaluated only at compile time: the `constexpr` and `consteval` functions coverage cannot see.

A function evaluated in a constant expression (a `static_assert`, a template argument, a `constexpr` initialiser)
leaves no runtime count, so the runner, which mutates executed lines, would never mutate it. These lines are reported
instead, or mutated with `--compile-time`; a mutant that makes a constant evaluation fail is a detection.
"""

import re
from collections.abc import Collection, Sequence

from .coverage import FileCoverage
from .lexer import Token, TokenKind, tokenize

COMPILE_TIME_SPECIFIERS = frozenset({"constexpr", "consteval"})
NOT_COMPILE_TIME = "not mutated: evaluated at compile time only"
OPENING_GROUPS = frozenset({"(", "["})
CLOSING_GROUPS = frozenset({")", "]"})

ERROR_MESSAGE = re.compile(r"\berror: (.*)")
COLOR_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# What GCC and Clang say when a static_assert or a constant evaluation fails, rather than when code is invalid.
CONSTANT_EVALUATION_FAILURE = re.compile(
    r"static assertion failed|static_assert failed|non-constant condition for static assertion"
    r"|is not a constant expression|must be initialized by a constant expression"
)


def _closing_brace(tokens: Sequence[Token], opening: int) -> int | None:
    depth = 0
    for position in range(opening, len(tokens)):
        depth += {"{": 1, "}": -1}.get(tokens[position].text, 0)
        if depth == 0:
            return position
    return None


def _is_initializer_brace(tokens: Sequence[Token], position: int, in_initializers: bool) -> bool:
    """A `{` that initialises a member in a constructor's initializer list, `m_x{x}`, rather than opening the body."""
    return in_initializers and tokens[position - 1].kind is TokenKind.IDENTIFIER


def _body_start(tokens: Sequence[Token], specifier: int) -> int | None:
    """The `{` opening the body of the function `tokens[specifier]` declares; None for a variable or a declaration."""
    depth = 0
    seen_parameters = False
    in_initializers = False
    position = specifier + 1
    while position < len(tokens):
        text = tokens[position].text
        if text in OPENING_GROUPS:
            depth += 1
            seen_parameters = seen_parameters or text == "("
        elif text in CLOSING_GROUPS:
            depth -= 1
        elif depth > 0:
            pass
        elif text == ";" or (text == "=" and tokens[position - 1].text != "operator"):
            return None
        elif text == ":" and seen_parameters:
            in_initializers = True
        elif text == "{" and _is_initializer_brace(tokens, position, in_initializers):
            closing = _closing_brace(tokens, position)
            if closing is None:
                return None
            position = closing
        elif text == "{":
            return position if seen_parameters else None
        position += 1
    return None


def _function_lines(tokens: Sequence[Token], specifier: int) -> range:
    opening = _body_start(tokens, specifier)
    closing = None if opening is None else _closing_brace(tokens, opening)
    if closing is None:
        return range(0)
    return range(tokens[specifier].line, tokens[closing].line + 1)


def _is_function_specifier(tokens: Sequence[Token], index: int) -> bool:
    """`constexpr` or `consteval` declaring something, not the `constexpr` of `if constexpr`."""
    return tokens[index].text in COMPILE_TIME_SPECIFIERS and (index == 0 or tokens[index - 1].text != "if")


def constexpr_function_lines(source: str) -> set[int]:
    """The lines of every `constexpr` or `consteval` function in `source`, from its specifier to its closing brace."""
    code = [token for token in tokenize(source) if token.kind is not TokenKind.COMMENT]
    return {
        line
        for index in range(len(code))
        if code[index].kind is TokenKind.IDENTIFIER and _is_function_specifier(code, index)
        for line in _function_lines(code, index)
    }


def compile_time_only_lines(source: str, coverage: FileCoverage, site_lines: Collection[int]) -> set[int]:
    """The lines with something to mutate in `constexpr` or `consteval` functions that never ran at runtime."""
    return (constexpr_function_lines(source) & set(site_lines)) - coverage.executed_lines()


def error_messages(diagnostics: str) -> list[str]:
    return [match.group(1).strip() for match in ERROR_MESSAGE.finditer(COLOR_ESCAPE.sub("", diagnostics))]


def is_constant_evaluation_failure(diagnostics: str) -> bool:
    """Whether every error the compiler reports is a failed `static_assert` or constant evaluation.

    An invalid mutant can make a `static_assert` fail too, so one other error makes it a compile error.
    """
    errors = error_messages(diagnostics)
    return bool(errors) and all(CONSTANT_EVALUATION_FAILURE.search(error) for error in errors)
