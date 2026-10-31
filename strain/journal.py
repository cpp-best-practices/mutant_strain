"""Crash-safe source mutation: the original is journaled before a file is touched."""

import base64
import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class Journal:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

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
            path.write_bytes(content)
            yield
        finally:
            path.write_bytes(original)
            entry.unlink(missing_ok=True)

    def _record(self, path: Path, original: bytes) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        resolved = str(path.resolve())
        entry = self.directory / f"{hashlib.sha1(resolved.encode()).hexdigest()}.json"
        temporary = entry.with_suffix(".tmp")
        temporary.write_text(json.dumps({"path": resolved, "original": base64.b64encode(original).decode()}))
        os.replace(temporary, entry)
        return entry
