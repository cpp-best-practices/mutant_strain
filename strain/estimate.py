"""What each mutant is expected to cost, and how long the mutants not yet evaluated will take.

A mutant costs the baseline durations of the tests that cover it, an upper bound since detection stops early, plus
the incremental build of its file, learned from the builds of that file so far.
"""

import enum
from collections import Counter
from collections.abc import Sequence


class Order(enum.StrEnum):
    """cost runs the cheapest mutants first; source keeps the order of files and offsets, for reproducibility."""

    COST = "cost"
    SOURCE = "source"


class BuildTimes:
    """How long a mutant of each file takes to build, from the builds of mutants of that file so far.

    A file with no build yet is expected to take as long as the average file, or nothing before any build.
    """

    def __init__(self) -> None:
        self._seconds: dict[str, list[float]] = {}

    def observe(self, file: str, seconds: float) -> None:
        self._seconds.setdefault(file, []).append(seconds)

    def estimate(self, file: str) -> float:
        seconds = self._seconds.get(file)
        if seconds:
            return sum(seconds) / len(seconds)
        means = [sum(observed) / len(observed) for observed in self._seconds.values()]
        return sum(means) / len(means) if means else 0.0


class Eta:
    """The time the mutants not yet evaluated will take.

    Their builds take what builds of their files took so far. Their tests take their baseline durations at the pace
    the tests of the evaluated mutants ran: detection stops at the first test that kills a mutant, so the pace is
    usually faster than the baselines, and it is measured, so the estimate improves as results arrive.
    Sums are kept per file, so each update is cheap even with many thousands of mutants.
    """

    def __init__(self, test_seconds: Sequence[float], files: Sequence[str], build_times: BuildTimes) -> None:
        self.test_seconds = list(test_seconds)
        self.files = list(files)
        self.build_times = build_times
        self.finished: set[int] = set()
        self.spent_testing = 0.0
        self.done_test_seconds = 0.0
        self.remaining_test_seconds = sum(self.test_seconds)
        self.remaining_files: Counter[str] = Counter(self.files)

    def finish(self, index: int, seconds: float, build_seconds: float = 0.0) -> None:
        """Records that mutant `index` took `seconds`, `build_seconds` of them building; it counts once."""
        if index in self.finished:
            return
        self.finished.add(index)
        self.spent_testing += max(0.0, seconds - build_seconds)
        self.done_test_seconds += self.test_seconds[index]
        self.remaining_test_seconds -= self.test_seconds[index]
        self.remaining_files[self.files[index]] -= 1

    def pace(self) -> float:
        """Seconds of testing per second of baseline so far; 1 until a test with a baseline has run."""
        return self.spent_testing / self.done_test_seconds if self.done_test_seconds > 0.0 else 1.0

    def remaining(self) -> float | None:
        """Seconds until every mutant is evaluated, or None before the first one is."""
        if not self.finished:
            return None
        builds = sum(count * self.build_times.estimate(file) for file, count in self.remaining_files.items())
        return builds + max(0.0, self.remaining_test_seconds) * self.pace()


def format_eta(seconds: float) -> str:
    """`2h10m`, `4m05s` or `12s`: short enough for the end of a log line."""
    whole = round(seconds)
    if whole >= 3600:
        hours, minutes = divmod(round(seconds / 60), 60)
        return f"{hours}h{minutes:02}m"
    minutes, rest = divmod(whole, 60)
    return f"{minutes}m{rest:02}s" if minutes else f"{rest}s"
