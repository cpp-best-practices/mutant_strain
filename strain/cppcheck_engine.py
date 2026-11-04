"""The `cppcheck` engine: binary operators from the abstract syntax tree in cppcheck's `--dump` output."""

import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterator
from pathlib import Path

from .operator_sites import SOURCE_ENCODING, SOURCE_ERRORS, EngineError, LineIndex, OperatorSite

CPPCHECK = "cppcheck"
MISSING = "the cppcheck engine needs cppcheck on PATH"
SOURCE_NAME = "source.cpp"
DUMP_SUFFIX = ".dump"
MAIN_FILE_INDEX = "0"
# One line per message on stderr: cppcheck still exits 0 and writes a dump when it cannot parse a configuration,
# but leaves that configuration out of the dump, so only these messages tell a failed parse from a file without operators.
FIELD_SEPARATOR = "\t"
MESSAGE_TEMPLATE = FIELD_SEPARATOR.join(("{line}", "{id}", "{message}"))
# The ids of cppcheck's InternalError types: each one means cppcheck gave up on a configuration.
PARSE_FAILURE_IDS = frozenset(
    {"syntaxError", "unknownMacro", "internalAstError", "internalError", "cppcheckLimit", "instantiationError"}
)


def is_available() -> bool:
    return shutil.which(CPPCHECK) is not None


def cppcheck_command(path: Path) -> list[str]:
    return [CPPCHECK, "--dump", "--quiet", "--language=c++", f"--template={MESSAGE_TEMPLATE}", str(path)]


def parse_failures(stderr: str) -> list[str]:
    """cppcheck's reasons for giving up on a configuration, among the messages in `stderr`."""
    failures: list[str] = []
    for message in stderr.splitlines():
        fields = message.split(FIELD_SEPARATOR, 2)
        if len(fields) == 3 and fields[1] in PARSE_FAILURE_IDS:
            line, failure_id, text = fields
            failures.append(f"line {line}: {text} [{failure_id}]")
    return failures


def run_dump(source: str) -> str:
    """cppcheck's dump of `source`, analyzed alone in a scratch folder so the project is never touched."""
    with tempfile.TemporaryDirectory(prefix="strain-cppcheck-") as directory:
        path = Path(directory) / SOURCE_NAME
        path.write_bytes(source.encode(SOURCE_ENCODING, SOURCE_ERRORS))
        try:
            result = subprocess.run(cppcheck_command(path), capture_output=True, text=True, check=False)
        except OSError as error:
            raise EngineError(f"{MISSING}: {error}") from error
        dump = path.with_name(path.name + DUMP_SUFFIX)
        if not dump.is_file():
            raise EngineError(f"cppcheck wrote no dump (exit code {result.returncode}): {result.stderr.strip()}")
        failures = parse_failures(result.stderr)
        if failures:
            raise EngineError(f"cppcheck cannot parse it, {failures[0]}")
        return dump.read_bytes().decode(SOURCE_ENCODING, SOURCE_ERRORS)


def _main_file_name(root: ElementTree.Element) -> str | None:
    """The analyzed file; cppcheck lists it first among the raw tokens, inside or beside the configurations."""
    return next((file.get("name") for file in root.iter("file") if file.get("index") == MAIN_FILE_INDEX), None)


def _is_in_main_file(token: ElementTree.Element, main_file: str | None) -> bool:
    file_index = token.get("fileIndex")
    if file_index is not None:
        return file_index == MAIN_FILE_INDEX
    return token.get("file") == main_file


def _is_binary_operator(token: ElementTree.Element, declared_names: frozenset[str]) -> bool:
    """Written by the user (not by a macro) with two operands, none of them a name being declared like `p` in `T *p`."""
    operands = (token.get("astOperand1"), token.get("astOperand2"))
    return (
        token.get("macroName") is None
        and all(operand is not None for operand in operands)
        and not any(operand in declared_names for operand in operands)
    )


def _configuration_sites(
    configuration: ElementTree.Element, main_file: str | None, index: LineIndex
) -> Iterator[OperatorSite]:
    declared_names = frozenset(
        name for variable in configuration.iter("var") if (name := variable.get("nameToken")) is not None
    )
    for token in configuration.iter("token"):
        if not _is_in_main_file(token, main_file) or not _is_binary_operator(token, declared_names):
            continue
        line, column = int(token.get("linenr", "0")), int(token.get("column", "0"))
        site = index.site_at_byte_column(token.get("str", ""), line, column)
        if site is not None:
            yield site


def sites_from_dump(dump: str, source: str) -> list[OperatorSite]:
    """The operator sites of every preprocessor configuration cppcheck analyzed, each site once."""
    index = LineIndex(source)
    try:
        root = ElementTree.fromstring(dump)  # noqa: S314 - cppcheck wrote the dump; expat ignores external entities
    except ElementTree.ParseError as error:
        raise EngineError(f"cannot read the cppcheck dump: {error}") from error
    main_file = _main_file_name(root)
    sites = {
        site.offset: site
        for configuration in root.iter("dump")
        for site in _configuration_sites(configuration, main_file, index)
    }
    return [sites[offset] for offset in sorted(sites)]


def operator_sites(source: str) -> list[OperatorSite]:
    return sites_from_dump(run_dump(source), source)
