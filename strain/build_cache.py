"""Mutants known to break the build, remembered across runs of the same commit and build configuration.

Whether a mutant compiles depends only on the sources, which a clean working tree pins to the commit, and on how
they are compiled, which the compilation database records. A cache file is named after both, so a new commit or a
reconfigured build starts an empty cache instead of trusting results that may no longer hold.
"""

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path

from .mutators import Mutant

CACHE_DIRECTORY = "strain-cache"
FORMAT_VERSION = 1
FINGERPRINT_LENGTH = 16


def cache_path(build_dir: Path, commit: str, compile_database: bytes) -> Path:
    fingerprint = hashlib.sha256(compile_database).hexdigest()[:FINGERPRINT_LENGTH]
    return build_dir / CACHE_DIRECTORY / f"{commit}-{fingerprint}.json"


def _read_failures(path: Path) -> dict[str, str]:
    """The failures recorded in `path`; a missing, unreadable or foreign file is an empty cache, never an error."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(document, dict) or document.get("version") != FORMAT_VERSION:
        return {}
    failures = document.get("build_failures")
    if not isinstance(failures, dict):
        return {}
    return {key: detail for key, detail in failures.items() if isinstance(key, str) and isinstance(detail, str)}


class BuildFailureCache:
    """Maps each mutant known not to compile to why; `path` None keeps it in memory for this run only."""

    def __init__(self, path: Path | None = None, failures: Mapping[str, str] | None = None) -> None:
        self.path = path
        self._failures = dict(failures or {})

    @classmethod
    def load(cls, path: Path) -> "BuildFailureCache":
        return cls(path, _read_failures(path))

    def __len__(self) -> int:
        return len(self._failures)

    def failure(self, mutant: Mutant) -> str | None:
        return self._failures.get(mutant.key)

    def add(self, mutant: Mutant, detail: str) -> None:
        """Records the failure and saves at once, so an interrupted run keeps what it learned."""
        self._failures[mutant.key] = detail
        self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {"version": FORMAT_VERSION, "build_failures": dict(sorted(self._failures.items()))}
        partial = self.path.with_suffix(".partial")
        partial.write_text(json.dumps(document, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(partial, self.path)
