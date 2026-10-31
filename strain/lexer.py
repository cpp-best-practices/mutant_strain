"""A small C++ tokenizer: just enough to find operators outside comments, literals and directives."""

import enum
import re
from dataclasses import dataclass


class TokenKind(enum.Enum):
    IDENTIFIER = enum.auto()
    NUMBER = enum.auto()
    STRING = enum.auto()
    CHAR = enum.auto()
    OPERATOR = enum.auto()
    COMMENT = enum.auto()


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    text: str
    line: int
    column: int
    offset: int


OPERATORS = sorted(
    (
        "<=>", "<<=", ">>=", "->*", "...",
        "::", "->", "++", "--", "<<", ">>", "<=", ">=", "==", "!=", "&&", "||",
        "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", ".*", "##",
        "{", "}", "[", "]", "(", ")", ";", ":", "?", ".", ",", "#",
        "+", "-", "*", "/", "%", "^", "&", "|", "~", "!", "=", "<", ">",
    ),
    key=len,
    reverse=True,
)

LITERAL_PREFIXES = frozenset({"u8", "u", "U", "L"})
RAW_STRING_PREFIXES = frozenset({"R", "u8R", "uR", "UR", "LR"})

_IDENTIFIER = re.compile(r"[A-Za-z_\$][A-Za-z0-9_\$]*")
_NUMBER = re.compile(r"\.?[0-9](?:[eEpP][+-]|'[0-9A-Za-z_]|[0-9A-Za-z_.])*")
_WHITESPACE = re.compile(r"[ \t\r\f\v]+")
_RAW_STRING_DELIMITER = re.compile(r'"([^()\\\s]{0,16})\(')


class _Scanner:
    def __init__(self, source: str) -> None:
        self.source = source
        self.offset = 0
        self.line = 1
        self.line_start = 0
        self.tokens: list[Token] = []

    def at_line_start(self) -> bool:
        return self.source[self.line_start : self.offset].strip() == ""

    def emit(self, kind: TokenKind, end: int) -> None:
        text = self.source[self.offset : end]
        self.tokens.append(Token(kind, text, self.line, self.offset - self.line_start + 1, self.offset))
        self.advance_to(end)

    def advance_to(self, end: int) -> None:
        newlines = self.source.count("\n", self.offset, end)
        if newlines:
            self.line += newlines
            self.line_start = self.source.rindex("\n", self.offset, end) + 1
        self.offset = end

    def end_of_quoted(self, start: int, quote: str) -> int:
        position = start + 1
        while position < len(self.source) and self.source[position] not in (quote, "\n"):
            position += 2 if self.source[position] == "\\" else 1
        return min(position + 1, len(self.source))

    def end_of_raw_string(self, quote_start: int) -> int:
        match = _RAW_STRING_DELIMITER.match(self.source, quote_start)
        if match is None:
            return self.end_of_quoted(quote_start, '"')
        terminator = f"){match.group(1)}\""
        end = self.source.find(terminator, match.end())
        return len(self.source) if end == -1 else end + len(terminator)

    def end_of_directive(self) -> int:
        position = self.offset
        while True:
            newline = self.source.find("\n", position)
            if newline == -1:
                return len(self.source)
            if self.source[position:newline].rstrip("\r").endswith("\\"):
                position = newline + 1
                continue
            return newline

    def end_of_block_comment(self) -> int:
        end = self.source.find("*/", self.offset + 2)
        return len(self.source) if end == -1 else end + 2

    def end_of_line_comment(self) -> int:
        end = self.source.find("\n", self.offset)
        return len(self.source) if end == -1 else end

    def scan_identifier_or_prefixed_literal(self, match: re.Match[str]) -> None:
        word = match.group()
        following = self.source[match.end() : match.end() + 1]
        if following == '"' and word in RAW_STRING_PREFIXES:
            self.emit(TokenKind.STRING, self.end_of_raw_string(match.end()))
        elif following in ('"', "'") and word in LITERAL_PREFIXES:
            kind = TokenKind.STRING if following == '"' else TokenKind.CHAR
            self.emit(kind, self.end_of_quoted(match.end(), following))
        else:
            self.emit(TokenKind.IDENTIFIER, match.end())

    def scan_operator(self) -> None:
        operator = next((op for op in OPERATORS if self.source.startswith(op, self.offset)), None)
        self.emit(TokenKind.OPERATOR, self.offset + (len(operator) if operator else 1))

    def scan_one(self) -> None:
        char = self.source[self.offset]
        if char == "\n":
            self.advance_to(self.offset + 1)
        elif whitespace := _WHITESPACE.match(self.source, self.offset):
            self.advance_to(whitespace.end())
        elif self.source.startswith("//", self.offset):
            self.emit(TokenKind.COMMENT, self.end_of_line_comment())
        elif self.source.startswith("/*", self.offset):
            self.emit(TokenKind.COMMENT, self.end_of_block_comment())
        elif char == "#" and self.at_line_start():
            self.advance_to(self.end_of_directive())
        elif char == '"':
            self.emit(TokenKind.STRING, self.end_of_quoted(self.offset, '"'))
        elif char == "'":
            self.emit(TokenKind.CHAR, self.end_of_quoted(self.offset, "'"))
        elif number := _NUMBER.match(self.source, self.offset):
            self.emit(TokenKind.NUMBER, number.end())
        elif identifier := _IDENTIFIER.match(self.source, self.offset):
            self.scan_identifier_or_prefixed_literal(identifier)
        else:
            self.scan_operator()


def tokenize(source: str) -> list[Token]:
    scanner = _Scanner(source)
    while scanner.offset < len(source):
        scanner.scan_one()
    return scanner.tokens
