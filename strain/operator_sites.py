"""What every mutation engine reports: where the source spells a binary or compound assignment operator."""

import re
from dataclasses import dataclass

SOURCE_ENCODING = "utf-8"
SOURCE_ERRORS = "surrogateescape"

_NEWLINE = re.compile("\n")


class EngineError(Exception):
    """A mutation engine is not installed or could not analyze a source."""


@dataclass(frozen=True, slots=True)
class OperatorSite:
    text: str
    line: int
    column: int
    offset: int


class LineIndex:
    """Locates operators in a source by one-based line and column, as parsers report them."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.starts = [0, *(newline.end() for newline in _NEWLINE.finditer(source))]
        self.encoded_lines = source.encode(SOURCE_ENCODING, SOURCE_ERRORS).split(b"\n")

    def site(self, text: str, line: int, column: int) -> OperatorSite | None:
        """The site of `text` at `line`:`column`, or None when the source does not spell `text` there."""
        if not 1 <= line <= len(self.starts) or column < 1:
            return None
        offset = self.starts[line - 1] + column - 1
        if self.source[offset : offset + len(text)] != text:
            return None
        return OperatorSite(text, line, column, offset)


    def site_at_byte_column(self, text: str, line: int, byte_column: int) -> OperatorSite | None:
        """Like `site`, for parsers that count the column in UTF-8 bytes rather than characters."""
        if not 1 <= line <= len(self.encoded_lines):
            return None
        prefix = self.encoded_lines[line - 1][: byte_column - 1]
        return self.site(text, line, len(prefix.decode(SOURCE_ENCODING, SOURCE_ERRORS)) + 1)
