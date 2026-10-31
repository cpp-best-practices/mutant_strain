"""The compilation database, `-fsyntax-only` pre-checks and incremental rebuilds."""

import json
import os
import shlex
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Flags that write files; each is followed by its argument unless it is attached (`-MFdeps.d`).
OUTPUT_FLAGS_WITH_ARGUMENT = frozenset({"-o", "-MF", "-MT", "-MQ"})
OUTPUT_FLAGS = frozenset({"-c", "-MD", "-MMD"})


@dataclass(frozen=True, slots=True)
class CompileCommand:
    directory: Path
    file: Path
    arguments: tuple[str, ...]

    def syntax_only_arguments(self) -> list[str]:
        kept: list[str] = []
        skip_next = False
        for argument in self.arguments:
            if skip_next:
                skip_next = False
            elif argument in OUTPUT_FLAGS_WITH_ARGUMENT:
                skip_next = True
            elif argument not in OUTPUT_FLAGS and not argument.startswith(("-MF", "-MT", "-MQ")):
                kept.append(argument)
        return [*kept, "-fsyntax-only"]

    def passes_syntax_check(self) -> bool:
        completed = subprocess.run(self.syntax_only_arguments(), cwd=self.directory, capture_output=True)
        return completed.returncode == 0


def _command_from_entry(entry: Mapping[str, Any]) -> CompileCommand:
    directory = Path(entry["directory"])
    arguments = entry["arguments"] if "arguments" in entry else shlex.split(entry["command"])
    return CompileCommand(
        directory=directory,
        file=Path(os.path.normpath(directory / entry["file"])),
        arguments=tuple(arguments),
    )


class CompilationDatabase:
    def __init__(self, commands: Iterable[CompileCommand]) -> None:
        self.commands = list(commands)
        self._by_file = {command.file: command for command in self.commands}

    @classmethod
    def from_entries(cls, entries: Iterable[Mapping[str, Any]]) -> "CompilationDatabase":
        return cls(_command_from_entry(entry) for entry in entries)

    @classmethod
    def load(cls, build_dir: Path) -> "CompilationDatabase":
        path = build_dir / "compile_commands.json"
        if not path.is_file():
            raise FileNotFoundError(f"{path} not found; configure with -DCMAKE_EXPORT_COMPILE_COMMANDS=ON")
        return cls.from_entries(json.loads(path.read_text()))

    def command_for(self, file: Path) -> CompileCommand | None:
        return self._by_file.get(Path(os.path.normpath(file)))


def build(build_dir: Path) -> bool:
    return subprocess.run(["cmake", "--build", str(build_dir)], capture_output=True).returncode == 0
