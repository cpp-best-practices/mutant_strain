"""Listing and running tests through CTest."""

import json
import math
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# Extra time CTest gets to tear a timed-out test down before we kill CTest itself.
KILL_GRACE_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class RunResult:
    returncode: int | None
    output: str
    duration: float
    timed_out: bool


def list_tests(build_dir: Path, include: str | None, exclude: str | None) -> list[str]:
    command = ["ctest", "--show-only=json-v1"]
    if include is not None:
        command += ["-R", include]
    if exclude is not None:
        command += ["-E", exclude]
    listing = subprocess.run(command, cwd=build_dir, check=True, capture_output=True, text=True)
    return [test["name"] for test in json.loads(listing.stdout)["tests"]]


def run_test(build_dir: Path, name: str, timeout: float) -> RunResult:
    command = [
        "ctest", "--verbose", "--timeout", str(math.ceil(timeout)), "-R", f"^{re.escape(name)}$",
    ]
    start = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=build_dir,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout + KILL_GRACE_SECONDS,
        )
    except subprocess.TimeoutExpired as expired:
        output = expired.output if isinstance(expired.output, str) else ""
        return RunResult(returncode=None, output=output, duration=time.monotonic() - start, timed_out=True)
    output = completed.stdout + completed.stderr
    return RunResult(
        returncode=completed.returncode,
        output=output,
        duration=time.monotonic() - start,
        timed_out="***Timeout" in output,
    )
