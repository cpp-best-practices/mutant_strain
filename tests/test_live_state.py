import unittest

from strain.coverage import LineCoverage
from strain.live_state import (
    PENDING,
    RUNNING,
    LiveState,
    clock_time,
    execution_summary,
    status_phase,
)
from strain.mutators import Mutant
from strain.results import MutantResult, MutantState, SurvivorKind
from strain.safety_nets import Level, RelaxedSafetyNet, SafetyNet

SOURCE = "int clamp(int a, int b)\n{\n    if (a < b)\n        return a;\n    return b;\n}\n"
MUTANT = Mutant("src/clamp.cpp", 3, 11, SOURCE.index("<"), "ror", "<", "<=")
SECOND_MUTANT = Mutant("src/clamp.cpp", 3, 11, SOURCE.index("<"), "ror", "<", ">=")
LINE_COVERAGE = LineCoverage(count=12, branches=2, branches_taken=2)


def survived(mutant: Mutant, test: str = "sample_tests") -> MutantResult:
    return MutantResult(mutant, test, MutantState.SURVIVED, SurvivorKind.NOT_DETECTED, "clamp(int, int)")


def killed(mutant: Mutant, test: str = "other_tests") -> MutantResult:
    return MutantResult(mutant, test, MutantState.KILLED, None, "clamp(int, int)")


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class HelpersTest(unittest.TestCase):
    def test_status_phase_is_the_activity_after_the_mutant(self) -> None:
        self.assertEqual(status_phase("[3/6] src/a.cpp:7:13 [ror] `<` → `>=`  building…"), "building")
        self.assertEqual(
            status_phase("[3/6] src/a.cpp:7:13 [ror] `<` → `>=`  running sample_tests…"), "running sample_tests"
        )
        self.assertEqual(
            status_phase("running sample_tests without mutation…"), "running sample_tests without mutation"
        )
        self.assertEqual(status_phase("✓ working tree is clean"), "")

    def test_clock_time(self) -> None:
        self.assertEqual(clock_time(5), "0:05")
        self.assertEqual(clock_time(125), "2:05")
        self.assertEqual(clock_time(3725), "1:02:05")

    def test_execution_counts_add_up_over_every_test_that_ran_the_line(self) -> None:
        executions = {"sample_tests": LINE_COVERAGE, "other_tests": LineCoverage(3, 2, 1)}
        self.assertEqual(
            execution_summary(3, executions),
            "line 3 executed 15 times: sample_tests 12, other_tests 3; 2/2 branches taken",
        )
        self.assertEqual(execution_summary(3, {}), "no coverage data for line 3")


class LiveStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.state = LiveState(self.clock)
        self.state.set_test("sample_tests", 1, 2)
        self.state.plan([MUTANT, SECOND_MUTANT])

    def test_queue_tracks_each_mutant_and_how_long_it_took(self) -> None:
        self.state.start_mutant(0, SOURCE, MUTANT)
        self.assertEqual([entry.state for entry in self.state.queue], [RUNNING, PENDING])
        self.clock.now += 2.5
        self.state.finish_mutant(0, MutantState.KILLED)
        self.assertEqual(self.state.queue[0].duration, 2.5)
        self.assertEqual(self.state.durations[MUTANT.id], 2.5)
        self.assertEqual(self.state.done_count(), 1)
        self.assertEqual(self.state.remaining_seconds(), 2.5)
        self.assertEqual(self.state.sources["src/clamp.cpp"], SOURCE)

    def test_totals_count_a_mutant_once_over_every_test(self) -> None:
        self.state.record_result(killed(MUTANT, "sample_tests"), SOURCE, LINE_COVERAGE)
        self.state.record_result(survived(MUTANT, "other_tests"), SOURCE, LINE_COVERAGE)
        self.state.record_result(survived(SECOND_MUTANT), SOURCE, LINE_COVERAGE)
        self.assertEqual(self.state.counts(), {MutantState.KILLED: 1, MutantState.SURVIVED: 1})
        self.assertEqual(self.state.score(), 0.5)
        self.assertEqual([entry.mutant for entry in self.state.survivors()], [SECOND_MUTANT])

    def test_a_mutant_killed_by_a_later_test_leaves_the_survivors(self) -> None:
        self.state.record_result(survived(MUTANT), SOURCE, LINE_COVERAGE)
        self.assertEqual(len(self.state.survivors()), 1)
        self.state.record_result(killed(MUTANT), SOURCE, LINE_COVERAGE)
        self.assertEqual(self.state.survivors(), [])
        self.assertEqual(self.state.score_history, [0.0, 1.0])

    def test_findings_and_statistics(self) -> None:
        self.state.record_result(survived(MUTANT), SOURCE, LINE_COVERAGE)
        self.state.record_result(killed(SECOND_MUTANT), SOURCE, LINE_COVERAGE)
        self.assertIn("src/clamp.cpp:3 `<` → `<=` survived (not_detected)", "\n".join(self.state.findings()))
        [operator] = self.state.operator_tallies()
        self.assertEqual((operator.name, operator.mutants, operator.detected, operator.survived), ("ror", 2, 1, 1))
        tests = {tally.name: tally for tally in self.state.test_tallies()}
        self.assertEqual(tests["sample_tests"].survived, 1)
        self.assertEqual(tests["other_tests"].detected, 1)
        [file] = self.state.file_scores()
        self.assertEqual(file.score, 0.5)

    def test_contexts_keep_each_tests_coverage_of_the_line(self) -> None:
        self.state.record_result(survived(MUTANT), SOURCE, LINE_COVERAGE)
        self.state.record_result(survived(MUTANT, "other_tests"), SOURCE, None)
        self.assertEqual(self.state.contexts[MUTANT.id].executions, {"sample_tests": LINE_COVERAGE})

    def test_starting_a_test_drops_the_previous_tests_queue(self) -> None:
        self.state.start_mutant(1, SOURCE, SECOND_MUTANT)
        plan = self.state.plan_version
        self.state.set_test("other_tests", 2, 2)
        self.assertEqual(self.state.queue, [])
        self.assertIsNone(self.state.current)
        self.assertEqual(self.state.test, "Test 2/2: other_tests")
        self.assertGreater(self.state.plan_version, plan)

    def test_finishing_the_run_lists_every_mutant_once_in_its_combined_state(self) -> None:
        self.state.record_result(killed(MUTANT, "sample_tests"), SOURCE, LINE_COVERAGE)
        self.state.set_test("other_tests", 2, 2)
        self.state.plan([SECOND_MUTANT])
        self.state.record_result(survived(MUTANT, "other_tests"), SOURCE, LINE_COVERAGE)
        self.state.record_result(survived(SECOND_MUTANT, "other_tests"), SOURCE, LINE_COVERAGE)
        self.clock.now += 9
        self.state.finish_run()
        self.assertTrue(self.state.done)
        self.assertEqual(self.state.test_name, "all tests")
        self.assertEqual(
            [(entry.mutant, entry.state) for entry in self.state.queue],
            [(MUTANT, MutantState.KILLED), (SECOND_MUTANT, MutantState.SURVIVED)],
        )
        self.assertEqual(self.state.elapsed(), 9)
        self.clock.now += 50
        self.assertEqual(self.state.elapsed(), 9)
        self.assertIsNone(self.state.remaining_seconds())

    def test_eta_counts_down_from_the_runners_cost_estimate(self) -> None:
        self.state.start_mutant(0, SOURCE, MUTANT)
        self.clock.now += 1
        self.state.finish_mutant(0, MutantState.KILLED)
        # The second mutant's tests cost far more than the first's, which the average per mutant cannot know.
        self.state.set_estimate(30.0)
        self.clock.now += 10
        self.assertEqual(self.state.remaining_seconds(), 20.0)
        self.clock.now += 40
        self.assertEqual(self.state.remaining_seconds(), 0.0)

    def test_a_new_test_forgets_the_estimate_of_the_previous_one(self) -> None:
        self.state.set_estimate(30.0)
        self.state.set_test("other_tests", 2, 2)
        self.assertIsNone(self.state.remaining_seconds())

    def test_build_log_counts_every_line_and_keeps_the_latest(self) -> None:
        self.state.start_build("cmake --build build")
        for number in range(3000):
            self.state.build_output(f"line {number}\twith a tab")
        self.assertEqual(self.state.build_lines_seen, 3000)
        self.assertEqual(self.state.build_log[-1], "line 2999   with a tab")
        self.assertEqual(len(self.state.build_log), 2000)
        build = self.state.build_version
        self.state.start_build("cmake --build build")
        self.assertEqual(len(self.state.build_log), 0)
        self.assertGreater(self.state.build_version, build)

    def test_every_change_bumps_the_version(self) -> None:
        version = self.state.version
        self.state.set_status("[1/2] x  building…")
        self.state.add_line("✓ step")
        self.assertEqual(self.state.version, version + 2)
        self.assertEqual(self.state.phase(), "")
        self.assertEqual(self.state.log, ["✓ step"])


class SafetyNetStateTest(unittest.TestCase):
    def test_the_relaxed_nets_that_hide_bugs_are_told_apart(self) -> None:
        state = LiveState(FakeClock())
        self.assertIsNone(state.relaxed_nets)
        self.assertEqual(state.bug_hiding_nets(), [])
        asan = RelaxedSafetyNet(SafetyNet.ASAN, Level.WARN, "fails in 1 translation unit(s), e.g. a.cpp")
        lto = RelaxedSafetyNet(
            SafetyNet.LTO, Level.IGNORE, "fails for the whole build (CMAKE_INTERPROCEDURAL_OPTIMIZATION=ON)"
        )
        version = state.version
        state.set_safety_nets([asan, lto], [SafetyNet.UBSAN])
        self.assertEqual(state.bug_hiding_nets(), [asan])
        self.assertEqual(state.holding_nets, [SafetyNet.UBSAN])
        self.assertGreater(state.version, version)


if __name__ == "__main__":
    unittest.main()
