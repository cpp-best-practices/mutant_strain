"""Per-test line, branch and function coverage from gcov."""

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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


class GcovCoverage:
    def __init__(self, build_dir: Path) -> None:
        self.build_dir = build_dir

    def reset(self) -> None:
        for data_file in self.build_dir.rglob("*.gcda"):
            data_file.unlink()

    def collect(self) -> dict[Path, FileCoverage]:
        coverage: dict[Path, FileCoverage] = {}
        for data_file in sorted(self.build_dir.rglob("*.gcda")):
            completed = subprocess.run(
                ["gcov", "--json-format", "--stdout", "--branch-probabilities", str(data_file)],
                cwd=data_file.parent,
                check=True,
                capture_output=True,
                text=True,
            )
            for report in _json_documents(completed.stdout):
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
