"""How long the mutants left will take, from what each one costs and how the ones so far compared with their cost.

A mutant costs a build of its file plus a run of its covering tests. The tests' baseline durations give an upper bound
on the second part, since running stops at the first test that detects the mutant. The estimate learns two things as
mutants finish: how long each file takes to rebuild, and what fraction of that upper bound the tests really take.
"Build" here is everything but the tests, such as mutating, journaling and restoring the source, which add up.
"""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MutantCost:
    file: str
    # The baseline durations of the tests that cover the mutant, added up.
    test_seconds: float


class Estimate:
    def __init__(self, costs: Sequence[MutantCost]) -> None:
        self.costs = list(costs)
        self.pending = set(range(len(self.costs)))
        self.builds: dict[str, list[float]] = {}
        self.spent_testing = 0.0
        self.test_bound = 0.0

    def record(self, index: int, build_seconds: float, test_seconds: float | None) -> None:
        """Learns from a mutant that was built: `build_seconds` is all its time but the tests, and `test_seconds` is
        None when it did not build, so no test ran."""
        self.pending.discard(index)
        cost = self.costs[index]
        self.builds.setdefault(cost.file, []).append(build_seconds)
        if test_seconds is not None:
            self.spent_testing += test_seconds
            self.test_bound += cost.test_seconds

    def skip(self, index: int) -> None:
        """Drops a mutant that finished without a build, such as an ignored one, without learning from it."""
        self.pending.discard(index)

    def build_seconds(self, file: str) -> float:
        builds = self.builds.get(file) or [seconds for times in self.builds.values() for seconds in times]
        return sum(builds) / len(builds)

    def remaining_seconds(self) -> float | None:
        """None until a mutant has been built and tested, since until then there is nothing to scale the costs by."""
        if not self.builds:
            return None
        if not self.pending:
            return 0.0
        # How much of the baseline durations the tests really took: below 1 when early kills skip tests.
        test_fraction = self.spent_testing / self.test_bound if self.test_bound else 1.0
        return sum(
            self.build_seconds(self.costs[index].file) + test_fraction * self.costs[index].test_seconds
            for index in self.pending
        )


def format_eta(seconds: float) -> str:
    """`ETA 45s`, `ETA 12m30s` or `ETA 2h10m`: precise enough for a guess, short enough for every progress line."""
    whole = round(seconds)
    if whole < 60:
        return f"ETA {whole}s"
    minutes, seconds_left = divmod(whole, 60)
    if minutes < 60:
        return f"ETA {minutes}m{seconds_left:02}s"
    hours, minutes_left = divmod(minutes, 60)
    return f"ETA {hours}h{minutes_left:02}m"
