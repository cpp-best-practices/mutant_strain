import os
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from strain.estimate import BuildTimes, Order
from strain.mutators import Mutant
from strain.results import MutantState, SurvivorKind
from strain.ctest import RunResult
from strain.runner import (
    CoveredMutant,
    CoveringTest,
    Outcome,
    PlannedMutant,
    Runner,
    RunOptions,
    Strategy,
    StrainError,
    order_mutants,
    short_path,
)

MUTANT = Mutant("src/a.cpp", 3, 11, 40, "ror", "<", "<=")


class ShortPathTest(unittest.TestCase):
    def test_a_path_under_the_working_directory_is_shown_relative(self) -> None:
        self.assertEqual(short_path(Path.cwd() / "build" / "debug"), os.path.join("build", "debug"))

    def test_a_path_far_from_the_working_directory_stays_absolute(self) -> None:
        self.assertEqual(short_path(Path(Path.cwd().anchor)), str(Path(Path.cwd().anchor)))


class ResumeTest(unittest.TestCase):
    """Resuming a per-mutant run reuses what the interrupted run recorded and runs only the rest."""

    def setUp(self) -> None:
        self.recorded: dict[str, Outcome] = {}
        self.runner = object.__new__(Runner)
        self.runner.ignored_outcome = lambda planned: None  # type: ignore[method-assign]
        self.runner.previous_outcome = lambda test, mutant, events: self.recorded.get(test)  # type: ignore[method-assign]
        self.runner.run_covering_tests = mock.Mock(side_effect=self.run_tests)  # type: ignore[method-assign]
        source = mock.sentinel.source
        self.entry = CoveredMutant(
            PlannedMutant(source, frozenset(), MUTANT),  # type: ignore[arg-type]
            [CoveringTest(name, 0.1, source) for name in ("fast", "middle", "slow")],  # type: ignore[arg-type]
        )

    @staticmethod
    def run_tests(entry: CoveredMutant, tests: list[CoveringTest], label: str) -> list[tuple[CoveringTest, Outcome]]:
        return [(test, Outcome(MutantState.SURVIVED, SurvivorKind.NOT_DETECTED)) for test in tests]

    def outcomes(self) -> list[tuple[str, MutantState]]:
        return [(test.name, outcome.state) for test, outcome in self.runner.covered_outcomes(self.entry, None, "")]  # type: ignore[arg-type]

    def test_tests_recorded_out_of_order_are_not_run_again(self) -> None:
        # The baselines of the interrupted run ordered the tests differently.
        self.recorded["slow"] = Outcome(MutantState.SURVIVED, SurvivorKind.NOT_DETECTED)
        self.assertEqual(
            self.outcomes(),
            [("slow", MutantState.SURVIVED), ("fast", MutantState.SURVIVED), ("middle", MutantState.SURVIVED)],
        )
        [call] = self.runner.run_covering_tests.call_args_list  # type: ignore[attr-defined]
        self.assertEqual([test.name for test in call.args[1]], ["fast", "middle"])

    def test_a_recorded_kill_runs_nothing(self) -> None:
        self.recorded["middle"] = Outcome(MutantState.KILLED)
        self.assertEqual(self.outcomes(), [("middle", MutantState.KILLED)])
        self.runner.run_covering_tests.assert_not_called()  # type: ignore[attr-defined]


class ParallelTestsTest(unittest.TestCase):
    """With --jobs N, the covering tests of a mutant run N at a time, and the run stops after the batch that kills it."""

    def setUp(self) -> None:
        self.runner = object.__new__(Runner)
        self.runner.options = RunOptions(strategy=Strategy.PER_MUTANT, jobs=2)
        self.runner.build_dir = Path("build")
        self.runner.console = mock.Mock()
        self.runner.known_build_failure = lambda planned: None  # type: ignore[method-assign]
        self.runner.mutated = lambda planned: nullcontext()  # type: ignore[method-assign]
        self.runner.build_mutant = lambda planned, targets, label: None  # type: ignore[method-assign]
        self.runner.targets_for = lambda tests: [None]  # type: ignore[method-assign]
        source = mock.Mock()
        source.coverage.lines = {}
        self.entry = covered(MUTANT, 0.1, 0.2, 0.3, 0.4)
        self.entry.tests = [CoveringTest(test.name, test.duration, source) for test in self.entry.tests]  # type: ignore[arg-type]
        self.both_running = threading.Barrier(2, timeout=5)
        self.ran: list[str] = []

    def run_test(self, build_dir: Path, name: str, timeout: float) -> RunResult:
        self.ran.append(name)
        if name in ("test0", "test1"):
            # Fails with BrokenBarrierError unless the other test of the batch runs at the same time.
            self.both_running.wait()
        returncode = 1 if name == "test3" else 0
        return RunResult(returncode=returncode, output="", duration=0.1, timed_out=False)

    def outcomes(self) -> list[tuple[str, MutantState]]:
        with mock.patch("strain.ctest.run_test", side_effect=self.run_test):
            outcomes = self.runner.run_covering_tests(self.entry, self.entry.tests, "")
        return [(test.name, outcome.state) for test, outcome in outcomes]

    def test_the_tests_of_a_batch_run_at_the_same_time_and_report_in_order(self) -> None:
        self.assertEqual(
            self.outcomes(),
            [
                ("test0", MutantState.SURVIVED),
                ("test1", MutantState.SURVIVED),
                ("test2", MutantState.SURVIVED),
                ("test3", MutantState.KILLED),
            ],
        )

    def test_no_batch_starts_after_the_one_that_kills_the_mutant(self) -> None:
        self.entry.tests = [self.entry.tests[2], self.entry.tests[3], *self.entry.tests[:2]]
        self.assertEqual(self.outcomes(), [("test2", MutantState.SURVIVED), ("test3", MutantState.KILLED)])
        self.assertEqual(sorted(self.ran), ["test2", "test3"])

    def test_jobs_needs_the_per_mutant_strategy(self) -> None:
        self.runner.options = RunOptions(strategy=Strategy.PER_TEST, jobs=2)
        with self.assertRaisesRegex(StrainError, "--strategy per-mutant"):
            self.runner.check_jobs()


def covered(mutant: Mutant, *durations: float) -> CoveredMutant:
    source = mock.sentinel.source
    return CoveredMutant(
        PlannedMutant(source, frozenset(), mutant),  # type: ignore[arg-type]
        [CoveringTest(f"test{number}", duration, source) for number, duration in enumerate(durations)],  # type: ignore[arg-type]
    )


class OrderTest(unittest.TestCase):
    """The cheapest mutants run first, so most results arrive early; --order source keeps file and offset order."""

    def setUp(self) -> None:
        self.central = covered(Mutant("src/a.cpp", 1, 5, 4, "aor", "+", "-"), 0.5, 2.0, 9.0)
        self.leaf = covered(Mutant("src/a.cpp", 9, 5, 90, "ror", "<", "<="), 0.5)
        self.other = covered(Mutant("src/b.cpp", 2, 5, 10, "lcr", "&&", "||"), 1.0)
        self.planned = [self.central, self.leaf, self.other]

    def test_cost_order_runs_the_mutants_with_the_cheapest_covering_tests_first(self) -> None:
        ordered = order_mutants(self.planned, Order.COST, BuildTimes())
        self.assertEqual(ordered, [self.leaf, self.other, self.central])

    def test_cost_order_counts_what_building_each_file_took(self) -> None:
        builds = BuildTimes()
        builds.observe("src/a.cpp", 5.0)
        builds.observe("src/b.cpp", 1.0)
        ordered = order_mutants(self.planned, Order.COST, builds)
        self.assertEqual(ordered, [self.other, self.leaf, self.central])

    def test_source_order_keeps_file_and_offset_order(self) -> None:
        ordered = order_mutants([self.other, self.leaf, self.central], Order.SOURCE, BuildTimes())
        self.assertEqual(ordered, [self.central, self.leaf, self.other])


if __name__ == "__main__":
    unittest.main()
