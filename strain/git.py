"""The git queries strain needs: a clean working tree and the lines changed since a ref."""

import re
import subprocess
from pathlib import Path

HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True, text=True).stdout


def is_clean(root: Path) -> bool:
    return _git(root, "status", "--porcelain", "--untracked-files=no") == ""


def parse_changed_lines(diff: str) -> dict[str, set[int]]:
    changed: dict[str, set[int]] = {}
    current: set[int] | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line.removeprefix("+++ ")
            current = None if target == "/dev/null" else changed.setdefault(target.removeprefix("b/"), set())
        elif current is not None and (hunk := HUNK_HEADER.match(line)):
            start = int(hunk.group(1))
            length = 1 if hunk.group(2) is None else int(hunk.group(2))
            current.update(range(start, start + length))
    return {file: lines for file, lines in changed.items() if lines}


def changed_lines(root: Path, ref: str) -> dict[str, set[int]]:
    return parse_changed_lines(_git(root, "diff", "--relative", "--unified=0", "--no-color", "--no-ext-diff", ref, "--"))
