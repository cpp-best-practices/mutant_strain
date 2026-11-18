import random
import unittest

from strain.estimate import Estimate, MutantCost, format_eta


class EstimateTest(unittest.TestCase):
    def test_nothing_to_go_on_until_a_mutant_was_built(self) -> None:
        estimate = Estimate([MutantCost("a.cpp", 1.0), MutantCost("a.cpp", 1.0)])
        self.assertIsNone(estimate.remaining_seconds())
        estimate.skip(0)
        self.assertIsNone(estimate.remaining_seconds())
        estimate.record(1, build_seconds=2.0, test_seconds=1.0)
        self.assertEqual(estimate.remaining_seconds(), 0.0)

    def test_each_file_has_its_own_build_time_and_unbuilt_files_take_the_average(self) -> None:
        estimate = Estimate([MutantCost(file, 0.0) for file in ("a.cpp", "b.cpp", "a.cpp", "b.cpp", "c.cpp")])
        estimate.record(0, build_seconds=10.0, test_seconds=0.0)
        estimate.record(1, build_seconds=2.0, test_seconds=0.0)
        self.assertEqual(estimate.remaining_seconds(), 10.0 + 2.0 + 6.0)

    def test_tests_take_the_fraction_of_their_baseline_seen_so_far(self) -> None:
        # Early kills skip covering tests, so the baseline durations are an upper bound.
        estimate = Estimate([MutantCost("a.cpp", 10.0), MutantCost("a.cpp", 4.0), MutantCost("a.cpp", 20.0)])
        estimate.record(0, build_seconds=1.0, test_seconds=5.0)
        estimate.record(1, build_seconds=1.0, test_seconds=2.0)
        self.assertAlmostEqual(estimate.remaining_seconds(), 1.0 + 0.5 * 20.0)  # type: ignore[arg-type]

    def test_a_mutant_that_did_not_build_teaches_only_the_build_time(self) -> None:
        estimate = Estimate([MutantCost("a.cpp", 10.0), MutantCost("a.cpp", 10.0), MutantCost("a.cpp", 10.0)])
        estimate.record(0, build_seconds=3.0, test_seconds=10.0)
        estimate.record(1, build_seconds=1.0, test_seconds=None)
        self.assertEqual(estimate.remaining_seconds(), 2.0 + 10.0)

    def test_after_a_tenth_of_the_mutants_the_estimate_is_within_a_fifth_of_the_real_time(self) -> None:
        """The acceptance in #42: a synthetic run whose costs vary by file and by covering tests, cheapest first."""
        rng = random.Random(42)  # noqa: S311 - a seeded, reproducible simulation
        build = {"parser.cpp": 30.0, "lexer.cpp": 8.0, "main.cpp": 2.0}
        costs = sorted(
            (MutantCost(rng.choice(list(build)), rng.choice([0.5, 2.0, 60.0, 85.0])) for _ in range(500)),
            key=lambda cost: cost.test_seconds,
        )

        def real(cost: MutantCost) -> tuple[float, float]:
            # Tests take 40-80% of their baseline bound; builds vary by 20% around the file's time.
            return build[cost.file] * rng.uniform(0.8, 1.2), cost.test_seconds * rng.uniform(0.4, 0.8)

        spent = [real(cost) for cost in costs]
        estimate = Estimate(costs)
        tenth = len(costs) // 10
        for index in range(tenth):
            estimate.record(index, *spent[index])
        actual = sum(build + tests for build, tests in spent[tenth:])
        remaining = estimate.remaining_seconds()
        assert remaining is not None
        self.assertLess(abs(remaining - actual) / actual, 0.2, (remaining, actual))


class FormatEtaTest(unittest.TestCase):
    def test_short_waits_in_seconds_long_ones_in_hours_and_minutes(self) -> None:
        self.assertEqual(format_eta(44.6), "ETA 45s")
        self.assertEqual(format_eta(750), "ETA 12m30s")
        self.assertEqual(format_eta(7800), "ETA 2h10m")


if __name__ == "__main__":
    unittest.main()
