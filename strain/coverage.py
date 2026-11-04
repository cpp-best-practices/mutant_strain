"""Per-test line, branch and function coverage from gcov."""

import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

from .prerequisites import gcov_identity, gcov_version_text


@dataclass(frozen=True, slots=True)
class LineCoverage:
    count: int
    branches: int
    branches_taken: int

    def merged(self, other: "LineCoverage") -> "LineCoverage":
        return LineCoverage(
            count=self.count + other.count,
            branches=max(self.branches, other.branches),
            branches_taken=max(self.branches_taken, other.branches_taken),
        )


@dataclass(frozen=True, slots=True)
class FunctionRange:
    name: str
    start_line: int
    end_line: int


@dataclass(slots=True)
class FileCoverage:
    lines: dict[int, LineCoverage] = field(default_factory=dict)
    functions: list[FunctionRange] = field(default_factory=list)

    def executed_lines(self) -> set[int]:
        return {number for number, line in self.lines.items() if line.count > 0}

    def function_at(self, line: int) -> str | None:
        enclosing = [function for function in self.functions if function.start_line <= line <= function.end_line]
        if not enclosing:
            return None
        return min(enclosing, key=lambda function: function.end_line - function.start_line).name


def _line_coverage(line: Mapping[str, Any]) -> LineCoverage:
    branches = [branch for branch in line.get("branches", []) if not branch.get("throw", False)]
    return LineCoverage(
        count=int(line["count"]),
        branches=len(branches),
        branches_taken=sum(1 for branch in branches if branch["count"] > 0),
    )


def _merge_file(coverage: FileCoverage, data: Mapping[str, Any]) -> None:
    for line in data.get("lines", []):
        number = int(line["line_number"])
        new = _line_coverage(line)
        coverage.lines[number] = coverage.lines[number].merged(new) if number in coverage.lines else new
    for function in data.get("functions", []):
        entry = FunctionRange(
            name=function.get("demangled_name", function["name"]),
            start_line=int(function["start_line"]),
            end_line=int(function["end_line"]),
        )
        if entry not in coverage.functions:
            coverage.functions.append(entry)


def merge_gcov_json(coverage: dict[Path, FileCoverage], report: Mapping[str, Any]) -> None:
    working_directory = report.get("current_working_directory", "")
    for data in report.get("files", []):
        path = Path(os.path.normpath(os.path.join(working_directory, data["file"])))
        _merge_file(coverage.setdefault(path, FileCoverage()), data)


def _add_llvm_record(files: list[dict[str, Any]], tag: str, value: str) -> None:
    if tag == "file":
        files.append({"file": value, "functions": [], "lines": []})
    elif tag == "function":
        start, _count, name = value.split(",", 2)
        files[-1]["functions"].append({"name": name, "start_line": int(start)})
    elif tag == "lcount":
        number, count = value.split(",")[:2]
        files[-1]["lines"].append({"line_number": int(number), "count": int(count), "branches": []})
    elif tag == "branch":
        state = value.partition(",")[2]
        files[-1]["lines"][-1]["branches"].append({"count": int(state == "taken")})


def _add_llvm_end_lines(data: dict[str, Any]) -> None:
    """llvm-cov gcov reports where a function starts but not where it ends, so it ends at its last line before the
    next function starts."""
    starts = [function["start_line"] for function in data["functions"]]
    line_numbers = [line["line_number"] for line in data["lines"]]
    for function in data["functions"]:
        start = function["start_line"]
        limit = min((other for other in starts if other > start), default=None)
        body = [number for number in line_numbers if number >= start and (limit is None or number < limit)]
        function["end_line"] = max(body, default=start)


def llvm_gcov_report(text: str, working_directory: Path) -> dict[str, Any]:
    """Translates the intermediate text format of llvm-cov gcov, which has no JSON format, into a gcov JSON report."""
    files: list[dict[str, Any]] = []
    for line in text.splitlines():
        tag, _, value = line.partition(":")
        _add_llvm_record(files, tag, value)
    for data in files:
        _add_llvm_end_lines(data)
    return {"current_working_directory": str(working_directory), "files": files}


def _run_gcov(arguments: list[str], working_directory: Path) -> str:
    return subprocess.run(
        ["gcov", *arguments], cwd=working_directory, check=True, capture_output=True, text=True
    ).stdout


def _llvm_gcov_text(data_file: Path) -> str:
    """llvm-cov gcov writes the intermediate format to a file even when asked for stdout, so it runs in a scratch
    directory to keep the build folder clean."""
    with tempfile.TemporaryDirectory() as output_dir:
        output = Path(output_dir)
        _run_gcov(["--intermediate-format", "--branch-probabilities", "--demangled-names", str(data_file)], output)
        return "".join(report.read_text() for report in sorted(output.glob("*.gcov")))


class GcovCoverage:
    def __init__(self, build_dir: Path) -> None:
        self.build_dir = build_dir

    @cached_property
    def gcov_is_llvm(self) -> bool:
        version_text = gcov_version_text()
        identity = None if version_text is None else gcov_identity(version_text)
        return identity is not None and identity.id == "Clang"

    def reset(self) -> None:
        for data_file in self.build_dir.rglob("*.gcda"):
            data_file.unlink()

    def reports(self, data_file: Path) -> list[Any]:
        if self.gcov_is_llvm:
            return [llvm_gcov_report(_llvm_gcov_text(data_file), self.build_dir)]
        return _json_documents(
            _run_gcov(["--json-format", "--stdout", "--branch-probabilities", str(data_file)], data_file.parent)
        )

    def collect(self) -> dict[Path, FileCoverage]:
        coverage: dict[Path, FileCoverage] = {}
        for data_file in sorted(self.build_dir.rglob("*.gcda")):
            for report in self.reports(data_file):
                merge_gcov_json(coverage, report)
        return coverage


def _json_documents(text: str) -> list[Any]:
    decoder = json.JSONDecoder()
    documents: list[Any] = []
    remaining = text.strip()
    while remaining:
        document, end = decoder.raw_decode(remaining)
        documents.append(document)
        remaining = remaining[end:].lstrip()
    return documents
