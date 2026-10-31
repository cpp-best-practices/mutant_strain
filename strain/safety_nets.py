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


EXPLANATIONS: dict[SafetyNet, str] = {
    SafetyNet.ASAN: (
        "add -fsanitize=address: without AddressSanitizer an out-of-bounds or use-after-free access introduced by a "
        "mutant can silently read garbage and pass the tests, so the mutant survives even though the code is broken"
    ),
    SafetyNet.UBSAN: (
        "add -fsanitize=undefined: without UBSan a mutant that causes signed overflow, an invalid shift or a null "
        "dereference is undefined behavior that may happen to produce the expected result, hiding a real bug"
    ),
    SafetyNet.STDLIB_ASSERTIONS: (
        "add -D_GLIBCXX_ASSERTIONS (libstdc++) or -D_LIBCPP_HARDENING_MODE=... (libc++): without standard library "
        "assertions a mutant that indexes past the end of a container or breaks a precondition goes unnoticed"
    ),
    SafetyNet.AUTO_VAR_INIT: (
        "add -ftrivial-auto-var-init=pattern: without it a mutant that leaves a variable uninitialized can read "
        "whatever was left on the stack, which often matches the old value and lets the mutant survive by luck"
    ),
}


def describe_missing(net: SafetyNet, files: Sequence[Path]) -> str:
    return f"safety net {net} is missing from {len(files)} translation unit(s), e.g. {files[0]}; {EXPLANATIONS[net]}"


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
