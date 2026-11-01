"""Mutant generation: an engine finds the operators, the replacement table turns each one into mutants."""

import enum
import fnmatch
import hashlib
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass

from . import cppcheck_engine, tree_sitter_engine
from .lexer import Token, TokenKind, tokenize
from .operator_sites import EngineError, OperatorSite

REPLACEMENTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "<": ("ror", ("<=", ">=")),
    "<=": ("ror", ("<", ">")),
    ">": ("ror", (">=", "<=")),
    ">=": ("ror", (">", "<")),
    "==": ("ror", ("!=",)),
    "!=": ("ror", ("==",)),
    "&&": ("lcr", ("||",)),
    "||": ("lcr", ("&&",)),
    "+": ("aor", ("-",)),
    "-": ("aor", ("+",)),
    "*": ("aor", ("/",)),
    "/": ("aor", ("*",)),
    "%": ("aor", ("*",)),
    "+=": ("asr", ("-=",)),
    "-=": ("asr", ("+=",)),
    "*=": ("asr", ("/=",)),
    "/=": ("asr", ("*=",)),
}

BINARY_ONLY = frozenset({"+", "-", "*", "/", "%"})
TEMPLATE_BRACKETS = frozenset({"<", ">", ">>"})

# Keywords after which an operator is unary or a declarator, never a binary operator.
NON_OPERAND_KEYWORDS = frozenset(
    {
        "auto", "bool", "case", "char", "char8_t", "char16_t", "char32_t", "co_return", "co_yield", "const",
        "constexpr", "default", "delete", "do", "double", "else", "float", "int", "long", "new", "noexcept",
        "operator", "return", "short", "signed", "sizeof", "throw", "unsigned", "void", "volatile", "wchar_t",
    }
)
TEMPLATE_ARGUMENT_TOKENS = frozenset({"::", ",", "*", "&", "<", ">", ">>", "...", "(", ")"})

MUTATE_OFF = "mutate-off"


class Engine(enum.StrEnum):
    AUTO = "auto"
    CPPCHECK = "cppcheck"
    TREE_SITTER = "tree-sitter"
    LEXER = "lexer"


# What `auto` picks: the first engine that is installed, most precise first.
ENGINE_PREFERENCE = (Engine.CPPCHECK, Engine.TREE_SITTER, Engine.LEXER)


@dataclass(frozen=True, slots=True)
class Mutant:
    file: str
    line: int
    column: int
    offset: int
    operator: str
    original: str
    replacement: str

    @property
    def end_column(self) -> int:
        return self.column + len(self.original)

    @property
    def id(self) -> str:
        key = f"{self.file}:{self.line}:{self.column}:{self.original}:{self.replacement}"
        return hashlib.sha1(key.encode()).hexdigest()[:8]


def _is_operand_end(token: Token) -> bool:
    if token.kind is TokenKind.IDENTIFIER:
        return token.text not in NON_OPERAND_KEYWORDS
    if token.kind is TokenKind.OPERATOR:
        return token.text in (")", "]")
    return token.kind is not TokenKind.COMMENT


def _closing_bracket(tokens: Sequence[Token], opening: int) -> int | None:
    depth = 0
    for index in range(opening, len(tokens)):
        token = tokens[index]
        if token.kind is TokenKind.OPERATOR and token.text not in TEMPLATE_ARGUMENT_TOKENS:
            return None
        if token.kind in (TokenKind.STRING, TokenKind.CHAR) or token.text == "&&":
            return None
        depth += {"<": 1, ">": -1, ">>": -2}.get(token.text, 0)
        if depth <= 0:
            return index
    return None


def _template_bracket_indices(tokens: Sequence[Token]) -> set[int]:
    brackets: set[int] = set()
    for index, token in enumerate(tokens):
        if token.text != "<" or index == 0 or index in brackets:
            continue
        previous = tokens[index - 1]
        if previous.kind is not TokenKind.IDENTIFIER or previous.text == "operator":
            continue
        closing = _closing_bracket(tokens, index)
        if closing is not None:
            brackets.update(
                position for position in range(index, closing + 1) if tokens[position].text in TEMPLATE_BRACKETS
            )
    return brackets


def _mutable_tokens(tokens: Sequence[Token]) -> Iterator[Token]:
    template_brackets = _template_bracket_indices(tokens)
    for index, token in enumerate(tokens):
        if token.kind is not TokenKind.OPERATOR or token.text not in REPLACEMENTS or index in template_brackets:
            continue
        previous = tokens[index - 1] if index > 0 else None
        if previous is not None and previous.text == "operator":
            continue
        if token.text in BINARY_ONLY and (previous is None or not _is_operand_end(previous)):
            continue
        yield token


def lexer_sites(source: str) -> list[OperatorSite]:
    code = [token for token in tokenize(source) if token.kind is not TokenKind.COMMENT]
    return [OperatorSite(token.text, token.line, token.column, token.offset) for token in _mutable_tokens(code)]


PARSING_ENGINES = {Engine.CPPCHECK: cppcheck_engine, Engine.TREE_SITTER: tree_sitter_engine}


def engine_is_available(engine: Engine) -> bool:
    return engine not in PARSING_ENGINES or PARSING_ENGINES[engine].is_available()


def resolve_engine(requested: Engine) -> Engine:
    """The engine `requested` names, or the best installed one for `auto`; raises EngineError if it is missing."""
    if requested is Engine.AUTO:
        return next(engine for engine in ENGINE_PREFERENCE if engine_is_available(engine))
    if not engine_is_available(requested):
        raise EngineError(PARSING_ENGINES[requested].MISSING)
    return requested


def operator_sites(engine: Engine, source: str) -> list[OperatorSite]:
    resolved = resolve_engine(engine)
    if resolved is Engine.LEXER:
        return lexer_sites(source)
    return PARSING_ENGINES[resolved].operator_sites(source)


def mutants_at(
    file: str,
    sites: Sequence[OperatorSite],
    lines: Collection[int] | None = None,
    max_per_line: int | None = None,
) -> list[Mutant]:
    mutants: list[Mutant] = []
    per_line: dict[int, int] = {}
    for site in sites:
        if site.text not in REPLACEMENTS or (lines is not None and site.line not in lines):
            continue
        operator, replacements = REPLACEMENTS[site.text]
        for replacement in replacements:
            if max_per_line is not None and per_line.get(site.line, 0) >= max_per_line:
                break
            per_line[site.line] = per_line.get(site.line, 0) + 1
            mutants.append(Mutant(file, site.line, site.column, site.offset, operator, site.text, replacement))
    return mutants


def generate_mutants(
    file: str,
    source: str,
    lines: Collection[int] | None = None,
    max_per_line: int | None = None,
    engine: Engine = Engine.LEXER,
) -> list[Mutant]:
    return mutants_at(file, operator_sites(engine, source), lines, max_per_line)


def describe_mutant(mutant: Mutant) -> str:
    return (
        f"{mutant.file}:{mutant.line}:{mutant.column} [{mutant.operator}] "
        f"`{mutant.original}` → `{mutant.replacement}`"
    )


def apply_mutant(source: str, mutant: Mutant) -> str:
    end = mutant.offset + len(mutant.original)
    if source[mutant.offset : end] != mutant.original:
        raise ValueError(f"{mutant.file}:{mutant.line}:{mutant.column}: source does not contain `{mutant.original}`")
    return source[: mutant.offset] + mutant.replacement + source[end:]


def _qualified_names(tokens: Sequence[Token]) -> Iterator[tuple[int, str]]:
    index = 0
    while index < len(tokens):
        if tokens[index].kind is not TokenKind.IDENTIFIER:
            index += 1
            continue
        start = index
        while index + 2 < len(tokens) and tokens[index + 1].text == "::" and tokens[index + 2].kind is TokenKind.IDENTIFIER:
            index += 2
        yield tokens[start].line, "::".join(tokens[position].text for position in range(start, index + 1, 2))
        index += 1


def suppressed_lines(source: str, arid: Sequence[str]) -> set[int]:
    tokens = tokenize(source)
    lines = {token.line for token in tokens if token.kind is TokenKind.COMMENT and MUTATE_OFF in token.text}
    code = [token for token in tokens if token.kind is not TokenKind.COMMENT]
    lines.update(
        line for line, name in _qualified_names(code) if any(fnmatch.fnmatchcase(name, pattern) for pattern in arid)
    )
    return lines
