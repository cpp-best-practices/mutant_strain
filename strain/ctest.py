"""Listing and running tests through CTest."""

import json
import math
import mmap
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# Extra time CTest gets to tear a timed-out test down before we kill CTest itself.
KILL_GRACE_SECONDS = 10.0
MEGABYTE = 1024 * 1024
# getrusage reports the peak resident set size in kilobytes on Linux, and in bytes on macOS.
MAXRSS_UNIT = 1 if sys.platform == "darwin" else 1024
# Every program built with AddressSanitizer calls it at startup, whether the runtime is linked statically or not.
ASAN_MARKER = b"__asan_init"
# Sets the data segment limit, which the test inherits through CTest; `$0` is the limit in kilobytes.
LIMIT_DATA = 'ulimit -d "$0" 2>/dev/null; exec "$@"'


@dataclass(frozen=True, slots=True)
class RunResult:
    returncode: int | None
    output: str
    duration: float
    timed_out: bool
    # The largest resident set size of CTest or the test, in bytes; None when the run was killed.
    peak_memory: int | None = None


@dataclass(frozen=True, slots=True)
class MemoryLimit:
    """How much memory a test may use with a mutant in place.

    Under AddressSanitizer the limit goes through ASAN_OPTIONS, because ASan reserves terabytes of address space and
    fails to start under a data segment limit.
    """

    megabytes: int
    asan: bool

    def wrap(self, command: list[str]) -> list[str]:
        return command if self.asan else ["/bin/sh", "-c", LIMIT_DATA, str(self.megabytes * 1024), *command]

    def environment(self) -> dict[str, str] | None:
        if not self.asan:
            return None
        # The user's own options come last, so they win.
        options = [f"hard_rss_limit_mb={self.megabytes}", "allocator_may_return_null=0", os.environ.get("ASAN_OPTIONS")]
        return {**os.environ, "ASAN_OPTIONS": ":".join(option for option in options if option)}


def uses_asan(executable: Path) -> bool:
    try:
        with executable.open("rb") as file, mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as data:
            return data.find(ASAN_MARKER) != -1
    except (OSError, ValueError):
        return False


def list_tests(build_dir: Path, include: str | None, exclude: str | None) -> list[str]:
    command = ["ctest", "--show-only=json-v1"]
    if include is not None:
        command += ["-R", include]
    if exclude is not None:
        command += ["-E", exclude]
    listing = subprocess.run(command, cwd=build_dir, check=True, capture_output=True, text=True)
    return [test["name"] for test in json.loads(listing.stdout)["tests"]]


def executables_by_test(build_dir: Path) -> dict[str, Path]:
    """The program each test runs, for the tests CTest can resolve a command for."""
    listing = subprocess.run(
        ["ctest", "--show-only=json-v1"], cwd=build_dir, check=True, capture_output=True, text=True
    )
    executables: dict[str, Path] = {}
    for test in json.loads(listing.stdout)["tests"]:
        command = test.get("command")
        if command:
            executables[test["name"]] = (build_dir / command[0]).resolve()
    return executables


def wait_with_usage(process: subprocess.Popen[bytes], timeout: float) -> tuple[int, int] | None:
    """Waits for `process` to exit, then returns its exit status and the peak memory of it and its children.

    os.wait4 reports the peak of the process tree, which subprocess cannot. Waiting on a thread keeps the timeout.
    """
    waited: list[tuple[int, int, os.struct_rusage]] = []
    thread = threading.Thread(target=lambda: waited.append(os.wait4(process.pid, 0)), daemon=True)
    thread.start()
    thread.join(timeout)
    if not waited:
        process.kill()
        thread.join()
        process.returncode = -9
        return None
    _, status, usage = waited[0]
    process.returncode = os.waitstatus_to_exitcode(status)
    return process.returncode, usage.ru_maxrss * MAXRSS_UNIT


def run_test(build_dir: Path, name: str, timeout: float, memory: MemoryLimit | None = None) -> RunResult:
    command = [
        "ctest",
        "--verbose",
        "--timeout",
        str(math.ceil(timeout)),
        "-R",
        f"^{re.escape(name)}$",
    ]
    start = time.monotonic()
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(
            command if memory is None else memory.wrap(command),
            cwd=build_dir,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=None if memory is None else memory.environment(),
        )
        with process:
            exited = wait_with_usage(process, timeout + KILL_GRACE_SECONDS)
        duration = time.monotonic() - start
        log.seek(0)
        output = log.read().decode(errors="replace")
    if exited is None:
        return RunResult(returncode=None, output=output, duration=duration, timed_out=True)
    returncode, peak_memory = exited
    return RunResult(returncode, output, duration, timed_out="***Timeout" in output, peak_memory=peak_memory)
