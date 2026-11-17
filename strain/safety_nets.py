"""Checks that the build folder is set up so that mutants which break the code get caught, and caught quickly."""

import enum
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .build import CompileCommand, cmake_is_true


class SafetyNet(enum.StrEnum):
    ASAN = "asan"
    UBSAN = "ubsan"
    STDLIB_ASSERTIONS = "stdlib_assertions"
    AUTO_VAR_INIT = "auto_var_init"
    NDEBUG = "ndebug"
    OPTIMIZATION = "optimization"
    PARTIAL_COVERAGE = "partial_coverage"
    UNITY_BUILD = "unity_build"
    LTO = "lto"


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
    SafetyNet.NDEBUG: (
        "remove -DNDEBUG (configure with -DCMAKE_BUILD_TYPE=Debug or drop it from CMAKE_CXX_FLAGS_<CONFIG>): NDEBUG "
        "compiles out every assert(), and assertions are among the strongest mutant killers, so a mutant that breaks "
        "an asserted invariant survives"
    ),
    SafetyNet.OPTIMIZATION: (
        "build with -O0 or -Og (configure with -DCMAKE_BUILD_TYPE=Debug): at -O1 and above inlining and merged blocks "
        "make gcov attribute lines inaccurately, so strain skips lines that ran or mutates lines that did not, and "
        "every mutant pays for a slower optimized rebuild"
    ),
    SafetyNet.PARTIAL_COVERAGE: (
        "add --coverage to these translation units too: strain only mutates lines that gcov reports as executed, so "
        "code built without coverage never gets a mutant and its weak tests stay hidden"
    ),
    SafetyNet.UNITY_BUILD: (
        "configure with -DCMAKE_UNITY_BUILD=OFF: a unity build compiles merged unity_*.cxx files instead of your "
        "sources, so the -fsyntax-only pre-check cannot find the command for a mutated file and every mutant "
        "rebuilds a whole batch of files"
    ),
    SafetyNet.LTO: (
        "remove -flto and configure with -DCMAKE_INTERPROCEDURAL_OPTIMIZATION=OFF: link-time optimization reruns the "
        "optimizer over the whole program at every link, so each mutant's rebuild gets much slower without making "
        "any mutant easier to kill"
    ),
}


def fix(net: SafetyNet) -> str:
    """How to put `net` back, the part of its explanation before the colon."""
    return EXPLANATIONS[net].partition(": ")[0]


def why(net: SafetyNet) -> str:
    """What goes wrong without `net`, the part of its explanation after the colon."""
    return EXPLANATIONS[net].partition(": ")[2]


def where_missing(files: Sequence[Path]) -> str:
    return f"fails in {len(files)} translation unit(s), e.g. {files[0]}"


def where_build_wide(evidence: str) -> str:
    return f"fails for the whole build ({evidence})"


def describe(net: SafetyNet, where: str) -> str:
    return f"safety net {net} {where}; {EXPLANATIONS[net]}"


def describe_missing(net: SafetyNet, files: Sequence[Path]) -> str:
    return describe(net, where_missing(files))


def describe_build_wide(net: SafetyNet, evidence: str) -> str:
    return describe(net, where_build_wide(evidence))


class Level(enum.StrEnum):
    ERROR = "error"
    WARN = "warn"
    IGNORE = "ignore"


# Nets that let a broken mutant pass are errors; nets that only cost precision or speed are warnings.
DEFAULT_LEVELS: dict[SafetyNet, Level] = {
    SafetyNet.ASAN: Level.ERROR,
    SafetyNet.UBSAN: Level.ERROR,
    SafetyNet.STDLIB_ASSERTIONS: Level.ERROR,
    SafetyNet.AUTO_VAR_INIT: Level.ERROR,
    SafetyNet.NDEBUG: Level.ERROR,
    SafetyNet.OPTIMIZATION: Level.WARN,
    SafetyNet.PARTIAL_COVERAGE: Level.WARN,
    SafetyNet.UNITY_BUILD: Level.WARN,
    SafetyNet.LTO: Level.WARN,
}


@dataclass(frozen=True, slots=True)
class RelaxedSafetyNet:
    """A safety net that fails, but whose configured level let the run go on: what that costs and how to fix it."""

    net: SafetyNet
    level: Level
    where: str

    @property
    def default(self) -> Level:
        return DEFAULT_LEVELS[self.net]

    @property
    def hides_bugs(self) -> bool:
        """Whether a mutant that breaks the code can survive without this net, which is why it errors by default."""
        return self.default is Level.ERROR

    @property
    def fix(self) -> str:
        return fix(self.net)

    @property
    def why(self) -> str:
        return why(self.net)

    @property
    def message(self) -> str:
        return describe(self.net, self.where)


def classify_failures(
    failures: Mapping[SafetyNet, str], levels: Mapping[SafetyNet, Level]
) -> tuple[list[str], list[RelaxedSafetyNet]]:
    """The failing nets (net to where it fails) that stop the run, as messages, and those the levels let pass.

    Nets come in declaration order, so the ones that hide bugs come before the ones that cost precision or speed.
    """
    errors: list[str] = []
    relaxed: list[RelaxedSafetyNet] = []
    for net in SafetyNet:
        if net not in failures:
            continue
        if levels[net] is Level.ERROR:
            errors.append(describe(net, failures[net]))
        else:
            relaxed.append(RelaxedSafetyNet(net, levels[net], failures[net]))
    return errors, relaxed


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


def _macro_operations(arguments: Sequence[str]) -> Iterator[tuple[str, str]]:
    """Each `-D`/`-U` in order as (flag, macro name), whether the name is attached (`-DX=1`) or separate (`-D X`)."""
    for previous, argument in zip(("", *arguments), arguments):
        if previous in ("-D", "-U"):
            yield previous, argument.partition("=")[0]
        elif argument.startswith(("-D", "-U")) and len(argument) > 2:
            yield argument[:2], argument[2:].partition("=")[0]


def _defines_ndebug(arguments: Sequence[str]) -> bool:
    defined = False
    for flag, name in _macro_operations(arguments):
        if name == "NDEBUG":
            defined = flag == "-D"
    return defined


def _optimization_level(arguments: Sequence[str]) -> str:
    """The level of the last `-O` flag, which is the one the compiler uses; `-O` alone means `-O1`."""
    levels = [argument.removeprefix("-O") or "1" for argument in arguments if argument.startswith("-O")]
    return levels[-1] if levels else "0"


def _uses_lto(arguments: Sequence[str]) -> bool:
    enabled = False
    for argument in arguments:
        if argument == "-flto" or argument.startswith("-flto="):
            enabled = True
        elif argument == "-fno-lto":
            enabled = False
    return enabled


CHECKS: dict[SafetyNet, Callable[[CompileCommand], bool]] = {
    SafetyNet.ASAN: lambda command: "address" in _sanitizers(command.arguments),
    SafetyNet.UBSAN: lambda command: "undefined" in _sanitizers(command.arguments),
    SafetyNet.STDLIB_ASSERTIONS: lambda command: _has_stdlib_assertions(command.arguments),
    SafetyNet.AUTO_VAR_INIT: lambda command: "-ftrivial-auto-var-init=pattern" in command.arguments,
    SafetyNet.NDEBUG: lambda command: not _defines_ndebug(command.arguments),
    SafetyNet.OPTIMIZATION: lambda command: _optimization_level(command.arguments) in ("0", "g"),
    SafetyNet.PARTIAL_COVERAGE: CompileCommand.has_gcov_instrumentation,
    SafetyNet.LTO: lambda command: not _uses_lto(command.arguments),
}


def missing_safety_nets(commands: Iterable[CompileCommand]) -> dict[SafetyNet, list[Path]]:
    missing: dict[SafetyNet, list[Path]] = {}
    for command in commands:
        for net, check in CHECKS.items():
            if not check(command):
                missing.setdefault(net, []).append(command.file)
    return missing


def _is_unity_source(path: Path) -> bool:
    return path.parent.name == "Unity" and path.name.startswith("unity_")


def build_wide_problems(cache: Mapping[str, str], commands: Sequence[CompileCommand]) -> dict[SafetyNet, str]:
    """Problems set for the whole build folder rather than per translation unit, with the evidence for each."""
    problems: dict[SafetyNet, str] = {}
    unity_sources = [command.file for command in commands if _is_unity_source(command.file)]
    if cmake_is_true(cache.get("CMAKE_UNITY_BUILD", "")):
        problems[SafetyNet.UNITY_BUILD] = f"CMAKE_UNITY_BUILD={cache['CMAKE_UNITY_BUILD']}"
    elif unity_sources:
        problems[SafetyNet.UNITY_BUILD] = f"{len(unity_sources)} unity source(s), e.g. {unity_sources[0]}"
    if cmake_is_true(cache.get("CMAKE_INTERPROCEDURAL_OPTIMIZATION", "")):
        problems[SafetyNet.LTO] = f"CMAKE_INTERPROCEDURAL_OPTIMIZATION={cache['CMAKE_INTERPROCEDURAL_OPTIMIZATION']}"
    return problems


def resolve_levels(config: Mapping[str, str], cli: Level | None) -> dict[SafetyNet, Level]:
    unknown = set(config) - {net.value for net in SafetyNet}
    if unknown:
        raise ValueError(f"unknown safety net(s) in config: {', '.join(sorted(unknown))}")
    if cli is not None:
        return dict.fromkeys(SafetyNet, cli)
    return {net: Level(config.get(net.value, DEFAULT_LEVELS[net])) for net in SafetyNet}
