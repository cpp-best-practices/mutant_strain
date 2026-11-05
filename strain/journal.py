"""Crash-safe source mutation: the original is journaled before a file is touched."""

import base64
import hashlib
import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path


def _nothing() -> None:
    pass


class Journal:
    """Calls `before_write` before every write to a source, so the caller can order the write after a build."""

    def __init__(self, directory: Path, before_write: Callable[[], None] = _nothing) -> None:
        self.directory = directory
        self.before_write = before_write

    def recover(self) -> list[Path]:
        if not self.directory.is_dir():
            return []
        restored: list[Path] = []
        for entry in sorted(self.directory.glob("*.json")):
            record = json.loads(entry.read_text())
            path = Path(record["path"])
            path.write_bytes(base64.b64decode(record["original"]))
            entry.unlink()
            restored.append(path)
        return restored

    @contextmanager
    def mutated(self, path: Path, content: bytes) -> Iterator[None]:
        original = path.read_bytes()
        entry = self._record(path, original)
        try:
            self._write(path, content)
            yield
        finally:
            self._write(path, original)
            entry.unlink(missing_ok=True)

    def _write(self, path: Path, content: bytes) -> None:
        self.before_write()
        path.write_bytes(content)

    def _record(self, path: Path, original: bytes) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        resolved = str(path.resolve())
        entry = self.directory / f"{hashlib.sha1(resolved.encode(), usedforsecurity=False).hexdigest()}.json"
        temporary = entry.with_suffix(".tmp")
        temporary.write_text(json.dumps({"path": resolved, "original": base64.b64encode(original).decode()}))
        os.replace(temporary, entry)
        return entry
