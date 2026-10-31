"""`strain.toml`, read from the root of the source tree."""

import fnmatch
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_FILE = "strain.toml"
KNOWN_KEYS = frozenset({"exclude", "arid", "safety_nets", "ignore"})


@dataclass(frozen=True, slots=True)
class Config:
    exclude: tuple[str, ...] = ()
    arid: tuple[str, ...] = ()
    safety_nets: Mapping[str, str] = field(default_factory=dict)
    ignored_mutants: frozenset[str] = frozenset()

    def is_excluded(self, relative_path: str) -> bool:
        return any(fnmatch.fnmatchcase(relative_path, pattern) for pattern in self.exclude)


def _config_from_table(table: Mapping[str, Any]) -> Config:
    unknown = set(table) - KNOWN_KEYS
    if unknown:
        raise ValueError(f"{CONFIG_FILE}: unknown key(s): {', '.join(sorted(unknown))}")
    return Config(
        exclude=tuple(table.get("exclude", ())),
        arid=tuple(table.get("arid", ())),
        safety_nets=dict(table.get("safety_nets", {})),
        ignored_mutants=frozenset(table.get("ignore", {}).get("mutants", ())),
    )


def load_config(source_root: Path) -> Config:
    path = source_root / CONFIG_FILE
    if not path.is_file():
        return Config()
    with path.open("rb") as file:
        return _config_from_table(tomllib.load(file))
