import unittest

from strain.ctest import RunResult
from strain.results import MutantState, baseline_problem, classify_run, mutation_score

ASAN_REPORT = "==1==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x602000000014\n"
UBSAN_REPORT = "math.cpp:4:12: runtime error: signed integer overflow: 2147483647 + 1\n"


def run(returncode: int = 0, output: str = "", timed_out: bool = False) -> RunResult:
    return RunResult(returncode=returncode, output=output, duration=0.1, timed_out=timed_out)


class ClassifyRunTest(unittest.TestCase):
    def test_passing_test_means_survived(self) -> None:
        self.assertIs(classify_run(run()), MutantState.SURVIVED)

    def test_failing_test_means_killed(self) -> None:
        self.assertIs(classify_run(run(returncode=8)), MutantState.KILLED)

    def test_sanitizer_report_means_killed_by_sanitizer(self) -> None:
        self.assertIs(classify_run(run(returncode=8, output=ASAN_REPORT)), MutantState.KILLED_BY_SANITIZER)

    def test_recovered_ubsan_report_counts_even_when_test_passes(self) -> None:
        self.assertIs(classify_run(run(output=UBSAN_REPORT)), MutantState.KILLED_BY_SANITIZER)

    def test_timeout_wins(self) -> None:
        self.assertIs(classify_run(run(returncode=8, output=ASAN_REPORT, timed_out=True)), MutantState.TIMEOUT)


class BaselineProblemTest(unittest.TestCase):
    def test_clean_baseline(self) -> None:
        self.assertIsNone(baseline_problem(run()))

    def test_failing_baseline(self) -> None:
        self.assertEqual(baseline_problem(run(returncode=8)), "fails without mutation")

    def test_baseline_with_undefined_behaviour(self) -> None:
        self.assertEqual(baseline_problem(run(output=UBSAN_REPORT)), "reports a sanitizer error without mutation")

    def test_baseline_timeout(self) -> None:
        self.assertEqual(baseline_problem(run(timed_out=True)), "times out without mutation")


class MutationScoreTest(unittest.TestCase):
    def test_score_follows_the_readme_formula(self) -> None:
        states = (
            [MutantState.KILLED] * 31
            + [MutantState.KILLED_BY_SANITIZER] * 4
            + [MutantState.TIMEOUT]
            + [MutantState.SURVIVED] * 5
            + [MutantState.COMPILE_ERROR, MutantState.IGNORED, MutantState.SKIPPED_UNEVALUATED]
        )
        score = mutation_score(states)
        assert score is not None
        self.assertAlmostEqual(score, 36 / 41)

    def test_score_without_scored_mutants_is_none(self) -> None:
        self.assertIsNone(mutation_score([MutantState.COMPILE_ERROR]))


if __name__ == "__main__":
    unittest.main()
