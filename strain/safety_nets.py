"""Checks that every translation unit is built with the flags that turn silent bugs into failures."""

import enum
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path

from .build import CompileCommand


class SafetyNet(enum.StrEnum):
    ASAN = "asan"
    UBSAN = "ubsan"
    STDLIB_ASSERTIONS = "stdlib_assertions"
    AUTO_VAR_INIT = "auto_var_init"


class Level(enum.StrEnum):
    ERROR = "error"
    WARN = "warn"
    IGNORE = "ignore"


def _sanitizers(arguments: Sequence[str]) -> set[str]:
    return {
        sanitizer
        for argument in arguments
        if argument.startswith("-fsanitize=")
        for sanitizer in argument.removeprefix("-fsanitize=").split(",")
    }


def _has_stdlib_assertions(arguments: Sequence[str]) -> bool:
    return any(
        argument in ("-D_GLIBCXX_ASSERTIONS", "-D_GLIBCXX_DEBUG") or argument.startswith("-D_LIBCPP_HARDENING_MODE=")
        for argument in arguments
    )


CHECKS: dict[SafetyNet, Callable[[Sequence[str]], bool]] = {
    SafetyNet.ASAN: lambda arguments: "address" in _sanitizers(arguments),
    SafetyNet.UBSAN: lambda arguments: "undefined" in _sanitizers(arguments),
    SafetyNet.STDLIB_ASSERTIONS: _has_stdlib_assertions,
    SafetyNet.AUTO_VAR_INIT: lambda arguments: "-ftrivial-auto-var-init=pattern" in arguments,
}


def missing_safety_nets(commands: Iterable[CompileCommand]) -> dict[SafetyNet, list[Path]]:
    missing: dict[SafetyNet, list[Path]] = {}
    for command in commands:
        for net, check in CHECKS.items():
            if not check(command.arguments):
                missing.setdefault(net, []).append(command.file)
    return missing


def resolve_levels(config: Mapping[str, str], cli: Level | None) -> dict[SafetyNet, Level]:
    unknown = set(config) - {net.value for net in SafetyNet}
    if unknown:
        raise ValueError(f"unknown safety net(s) in config: {', '.join(sorted(unknown))}")
    if cli is not None:
        return dict.fromkeys(SafetyNet, cli)
    return {net: Level(config.get(net.value, Level.ERROR)) for net in SafetyNet}
