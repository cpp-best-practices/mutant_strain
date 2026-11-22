import os
import unittest
from pathlib import Path
from unittest import mock

from strain.mutators import Mutant
from strain.results import MutantState, SurvivorKind
from strain.runner import CoveredMutant, CoveringTest, Outcome, PlannedMutant, Runner, short_path

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


if __name__ == "__main__":
    unittest.main()
