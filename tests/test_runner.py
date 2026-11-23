import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from strain.estimate import BuildTimes, Order
from strain.mutators import Mutant
from strain.results import MutantState, SurvivorKind
from strain.runner import CoveredMutant, CoveringTest, Outcome, PlannedMutant, Runner, order_mutants, short_path

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


@unittest.skipUnless(shutil.which("g++"), "needs g++")
class DiscardedCoverageTest(unittest.TestCase):
    """Mutant runs write their coverage data to a scratch folder that is deleted, not into the build folder."""

    def test_a_mutant_run_leaves_no_coverage_data_behind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            build = Path(tmp) / "build"
            build.mkdir()
            (build / "a.cpp").write_text("int main() { return 0; }\n")
            subprocess.run(["g++", "--coverage", "a.cpp", "-o", "a"], cwd=build, check=True, capture_output=True)
            runner = object.__new__(Runner)
            runner.coverage_sink = Path(tmp)

            with runner.discarded_coverage() as environment:
                subprocess.run([str(build / "a")], env={**os.environ, **environment}, check=True)
                sink = Path(environment["GCOV_PREFIX"])
                self.assertTrue(list(sink.rglob("*.gcda")))

            self.assertEqual(list(build.rglob("*.gcda")), [])
            self.assertFalse(sink.exists())


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

    def test_cost_order_runs_the_cheapest_file_first_and_its_cheapest_mutants_first(self) -> None:
        cheap = covered(Mutant("src/a.cpp", 5, 5, 50, "aor", "-", "+"), 0.1)
        ordered = order_mutants([*self.planned, cheap], Order.COST, BuildTimes())
        # a.cpp averages 4.0s a mutant and b.cpp 1.0s, so b.cpp goes first; within a.cpp the cheapest go first.
        self.assertEqual(ordered, [self.other, cheap, self.leaf, self.central])

    def test_cost_order_keeps_the_mutants_of_a_file_together(self) -> None:
        ordered = order_mutants(self.planned, Order.COST, BuildTimes())
        # leaf alone is cheaper than other, but going to b.cpp and back would compile a.cpp once more.
        self.assertEqual(ordered, [self.other, self.leaf, self.central])

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
