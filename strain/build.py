"""The compilation database, `-fsyntax-only` pre-checks and incremental rebuilds."""

import json
import re
import shlex
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Flags that write files; each is followed by its argument unless it is attached (`-MFdeps.d`).
OUTPUT_FLAGS_WITH_ARGUMENT = frozenset({"-o", "-MF", "-MT", "-MQ"})
OUTPUT_FLAGS = frozenset({"-c", "-MD", "-MMD"})
SOURCE_BASED_COVERAGE_FLAGS = frozenset({"-fprofile-instr-generate", "-fcoverage-mapping"})
CMAKE_TRUE_VALUES = frozenset({"1", "ON", "YES", "TRUE", "Y"})
COMPILER_SETTING = re.compile(r'set\((CMAKE_CXX_COMPILER_(?:ID|VERSION)) "([^"]*)"\)')
FILE_API_CLIENT = "client-strain"
FILE_API_CODEMODEL = "codemodel-v2"


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

    def has_gcov_instrumentation(self) -> bool:
        arguments = self.arguments
        return "--coverage" in arguments or ("-fprofile-arcs" in arguments and "-ftest-coverage" in arguments)

    def has_source_based_coverage(self) -> bool:
        return any(argument in SOURCE_BASED_COVERAGE_FLAGS for argument in self.arguments)

    def passes_syntax_check(self) -> bool:
        completed = subprocess.run(self.syntax_only_arguments(), cwd=self.directory, capture_output=True)
        return completed.returncode == 0


def _command_from_entry(entry: Mapping[str, Any]) -> CompileCommand:
    directory = Path(entry["directory"])
    arguments = entry["arguments"] if "arguments" in entry else shlex.split(entry["command"])
    return CompileCommand(
        directory=directory,
        file=(directory / entry["file"]).resolve(),
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
        return self._by_file.get(file.resolve())


def _generator(cache: Mapping[str, str]) -> str:
    return cache.get("CMAKE_GENERATOR", "")


def generator_warning(cache: Mapping[str, str]) -> str | None:
    """Why the build folder's generator slows every mutant's rebuild, or None for Ninja, which rebuilds fastest."""
    generator = _generator(cache)
    if generator.startswith("Ninja"):
        return None
    return (
        f"the build folder uses the {generator or 'unknown'} generator; each mutant rebuilds faster with Ninja "
        "(reconfigure a fresh build folder with -G Ninja)"
    )


def _sets_make_jobs(makeflags: str) -> bool:
    return any(flag.startswith(("-j", "--jobs")) for flag in makeflags.split())


def parallel_jobs(cache: Mapping[str, str], environ: Mapping[str, str], cpu_count: int | None) -> int | None:
    """The `--parallel` level for a make build that would otherwise build serially, or None to leave it alone.

    Respects a level the user chose with `CMAKE_BUILD_PARALLEL_LEVEL` or `MAKEFLAGS`.
    """
    generator = _generator(cache)
    if not generator.endswith("Makefiles") or generator.startswith("NMake"):
        return None
    if "CMAKE_BUILD_PARALLEL_LEVEL" in environ or _sets_make_jobs(environ.get("MAKEFLAGS", "")):
        return None
    return cpu_count


def build_command(build_dir: Path, target: str | None = None, jobs: int | None = None) -> list[str]:
    command = ["cmake", "--build", str(build_dir)]
    if target is not None:
        command += ["--target", target]
    if jobs is not None:
        command += ["--parallel", str(jobs)]
    return command


def _discard(line: str) -> None:
    pass


def build(
    build_dir: Path,
    target: str | None = None,
    on_output: Callable[[str], None] = _discard,
    jobs: int | None = None,
) -> bool:
    """Builds `target` and what it depends on, or the whole build folder when there is no target.

    Passes each line the build prints to `on_output` as it arrives.
    """
    with subprocess.Popen(
        build_command(build_dir, target, jobs),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    ) as process:
        try:
            for line in process.stdout or ():
                on_output(line.rstrip("\n"))
        except BaseException:
            process.kill()
            raise
    return process.returncode == 0


# Make decides what to rebuild by comparing modification times, and the make macOS ships (GNU make 3.81) and some
# file systems keep only whole seconds of them. The margin covers file systems that stamp with a clock a tick behind.
TIMESTAMP_RESOLUTION_SECONDS = 1.0
TIMESTAMP_MARGIN_SECONDS = 0.05


class BuildClock:
    """Orders source writes after the last build, so a build never mistakes a changed source for an up-to-date one.

    A source written in the same second its object was built looks no newer than the object to a make that compares
    whole seconds, which then skips the rebuild and runs the previous mutant's binary.
    """

    def __init__(self) -> None:
        self.last_build_finished = 0.0

    def build_finished(self) -> None:
        self.last_build_finished = time.time()

    def wait_until_writes_outdate_the_build(self) -> None:
        next_second = (self.last_build_finished // TIMESTAMP_RESOLUTION_SECONDS + 1) * TIMESTAMP_RESOLUTION_SECONDS
        time.sleep(max(0.0, next_second + TIMESTAMP_MARGIN_SECONDS - time.time()))


def _file_api_directory(build_dir: Path) -> Path:
    return build_dir / ".cmake" / "api" / "v1"


@dataclass(frozen=True, slots=True)
class Target:
    name: str
    sources: frozenset[Path]


def _target_from_details(details: Mapping[str, Any], source_dir: Path) -> Target:
    sources = frozenset((source_dir / source["path"]).resolve() for source in details.get("sources", []))
    return Target(details["name"], sources)


def _read_codemodel_artifacts(reply_dir: Path) -> dict[Path, Target]:
    indexes = sorted(reply_dir.glob("index-*.json"))
    if not indexes:
        return {}
    index = json.loads(indexes[-1].read_text())
    codemodel = index.get("reply", {}).get(FILE_API_CLIENT, {}).get(FILE_API_CODEMODEL, {})
    if "jsonFile" not in codemodel:
        return {}
    model = json.loads((reply_dir / codemodel["jsonFile"]).read_text())
    build_dir = Path(model["paths"]["build"])
    source_dir = Path(model["paths"]["source"])
    artifacts: dict[Path, Target] = {}
    for configuration in model["configurations"]:
        for target in configuration["targets"]:
            details = json.loads((reply_dir / target["jsonFile"]).read_text())
            for artifact in details.get("artifacts", []):
                artifacts[(build_dir / artifact["path"]).resolve()] = _target_from_details(details, source_dir)
    return artifacts


def target_artifacts(build_dir: Path) -> dict[Path, Target]:
    """The target that produces each of the build's files, keyed by absolute path, from the CMake file API.

    Writes a codemodel query and reconfigures the build folder so CMake answers it; empty if that fails.
    """
    query = _file_api_directory(build_dir) / "query" / FILE_API_CLIENT / FILE_API_CODEMODEL
    query.parent.mkdir(parents=True, exist_ok=True)
    query.touch()
    if subprocess.run(["cmake", str(build_dir)], capture_output=True).returncode != 0:
        return {}
    return _read_codemodel_artifacts(_file_api_directory(build_dir) / "reply")


def read_cmake_cache(build_dir: Path) -> dict[str, str]:
    """The `NAME:TYPE=value` entries of `CMakeCache.txt`, keyed by name; empty when there is no cache."""
    cache = build_dir / "CMakeCache.txt"
    if not cache.is_file():
        return {}
    entries: dict[str, str] = {}
    for line in cache.read_text(errors="replace").splitlines():
        if not line or line.startswith(("//", "#")):
            continue
        key, _, value = line.partition("=")
        entries[key.partition(":")[0]] = value
    return entries


def cmake_is_true(value: str) -> bool:
    return value.upper() in CMAKE_TRUE_VALUES or (value.isdigit() and int(value) != 0)


@dataclass(frozen=True, slots=True)
class CompilerIdentity:
    id: str
    version: str


def read_cxx_compiler(build_dir: Path) -> CompilerIdentity | None:
    """The C++ compiler CMake detected, from `CMakeFiles/<cmake version>/CMakeCXXCompiler.cmake`."""
    for path in sorted(build_dir.glob("CMakeFiles/*/CMakeCXXCompiler.cmake"), reverse=True):
        settings = dict(COMPILER_SETTING.findall(path.read_text(errors="replace")))
        if settings.get("CMAKE_CXX_COMPILER_ID"):
            return CompilerIdentity(settings["CMAKE_CXX_COMPILER_ID"], settings.get("CMAKE_CXX_COMPILER_VERSION", ""))
    return None
