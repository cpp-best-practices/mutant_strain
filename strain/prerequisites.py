"""What the build folder must provide before strain can mutation test at all; unlike safety nets, none can be ignored."""

import re
import subprocess
from collections.abc import Sequence

from .build import CompileCommand, CompilerIdentity

NO_COVERAGE = (
    "no project translation unit is built with coverage instrumentation; add --coverage to CMAKE_CXX_FLAGS and "
    "CMAKE_EXE_LINKER_FLAGS: strain only mutates the lines a test executes, and without coverage it sees none, so it "
    "would mutate nothing and report a pass"
)
SOURCE_BASED_COVERAGE = (
    "the build uses Clang source-based coverage (-fprofile-instr-generate/-fcoverage-mapping), which the gcov backend "
    "cannot read; build with --coverage instead so the tests write .gcda files"
)
GCOV_MISSING = "gcov was not found on PATH; strain reads the coverage of every test with gcov"
GCOV_VERSION = re.compile(r"LLVM version (\d[\w.]*)|(\d+\.\d+(?:\.\d+)?)\s*$")


def coverage_problem(commands: Sequence[CompileCommand]) -> str | None:
    if any(command.has_gcov_instrumentation() for command in commands):
        return None
    if any(command.has_source_based_coverage() for command in commands):
        return SOURCE_BASED_COVERAGE
    return NO_COVERAGE


def gcov_version_text() -> str | None:
    try:
        return subprocess.run(["gcov", "--version"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def gcov_identity(version_text: str) -> CompilerIdentity | None:
    for line in version_text.splitlines():
        match = GCOV_VERSION.search(line.strip())
        if match is not None:
            llvm_version, gnu_version = match.groups()
            return CompilerIdentity("Clang", llvm_version) if llvm_version else CompilerIdentity("GNU", gnu_version)
    return None


def _family(compiler_id: str) -> str:
    return "Clang" if compiler_id.endswith("Clang") else compiler_id


def _major(version: str) -> str:
    return version.partition(".")[0]


def gcov_problem(compiler: CompilerIdentity | None, version_text: str | None) -> str | None:
    if version_text is None:
        return GCOV_MISSING
    gcov = gcov_identity(version_text)
    if compiler is None or gcov is None:
        return None
    if _family(compiler.id) == gcov.id and _major(compiler.version) == _major(gcov.version):
        return None
    return (
        f"gcov is {gcov.id} {gcov.version} but the project is compiled by {compiler.id} {compiler.version}; gcov can "
        f"only read the .gcda files of its own compiler and version, so put {_matching_gcov(compiler)} first on PATH"
    )


def _matching_gcov(compiler: CompilerIdentity) -> str:
    major = _major(compiler.version)
    if _family(compiler.id) == "Clang":
        return f"a `gcov` that runs `llvm-cov-{major} gcov`"
    return f"gcov-{major}"


def no_coverage_data(test: str) -> str:
    return (
        f"test {test} passed but no coverage data was written for any project source; add --coverage to "
        "CMAKE_EXE_LINKER_FLAGS and CMAKE_SHARED_LINKER_FLAGS as well as CMAKE_CXX_FLAGS: without .gcda files strain "
        "sees no executed lines, so it would mutate nothing and report a pass"
    )


def prerequisite_problems(
    commands: Sequence[CompileCommand], compiler: CompilerIdentity | None, version_text: str | None
) -> list[str]:
    return [
        problem
        for problem in (coverage_problem(commands), gcov_problem(compiler, version_text))
        if problem is not None
    ]
