import random
import unittest

from strain.estimate import BuildTimes, Eta, format_eta

# The ETA after 10% of the mutants must be within this fraction of the time the rest really take.
ETA_TOLERANCE = 0.25
BUILD_SECONDS = {"src/lexer.cpp": 2.0, "src/parser.cpp": 5.0, "src/codegen.cpp": 9.0}
SURVIVOR_SHARE = 0.3


class SimulatedMutant:
    """A mutant of a simulated run: its covering tests' baselines, and what building and testing it really take."""

    def __init__(self, generator: random.Random) -> None:
        self.file = generator.choice(sorted(BUILD_SECONDS))
        self.test_seconds = sum(generator.uniform(0.1, 30.0) for _ in range(generator.randint(1, 6)))
        self.build = BUILD_SECONDS[self.file] * generator.uniform(0.9, 1.1)
        # A survivor runs every covering test; a killed mutant stops early, at a test somewhere along the way.
        survives = generator.random() < SURVIVOR_SHARE
        self.testing = self.test_seconds * (generator.uniform(0.95, 1.05) if survives else generator.uniform(0.1, 0.8))


class BuildTimesTest(unittest.TestCase):
    def test_a_file_takes_the_average_of_its_builds(self) -> None:
        times = BuildTimes()
        times.observe("a.cpp", 2.0)
        times.observe("a.cpp", 4.0)
        self.assertEqual(times.estimate("a.cpp"), 3.0)

    def test_a_file_never_built_takes_as_long_as_the_average_file(self) -> None:
        times = BuildTimes()
        self.assertEqual(times.estimate("a.cpp"), 0.0)
        times.observe("a.cpp", 2.0)
        times.observe("a.cpp", 2.0)
        times.observe("b.cpp", 6.0)
        self.assertEqual(times.estimate("c.cpp"), 4.0)


class EtaTest(unittest.TestCase):
    def test_no_estimate_before_the_first_mutant(self) -> None:
        self.assertIsNone(Eta([1.0, 2.0], ["a.cpp", "a.cpp"], BuildTimes()).remaining())

    def test_the_remaining_tests_run_at_the_pace_measured_so_far_and_builds_take_what_they_took(self) -> None:
        builds = BuildTimes()
        eta = Eta([2.0, 4.0, 6.0], ["a.cpp", "a.cpp", "b.cpp"], builds)
        builds.observe("a.cpp", 3.0)
        # The test of the first mutant took half its baseline: the rest take half theirs, plus a 3s build each.
        eta.finish(0, 4.0, build_seconds=3.0)
        self.assertEqual(eta.pace(), 0.5)
        self.assertEqual(eta.remaining(), 3.0 + 2.0 + 3.0 + 3.0)

    def test_a_mutant_finished_twice_counts_once(self) -> None:
        eta = Eta([2.0, 2.0], ["a.cpp", "a.cpp"], BuildTimes())
        eta.finish(0, 1.0)
        eta.finish(0, 1.0)
        self.assertEqual(eta.remaining(), 1.0)
        eta.finish(1, 1.0)
        self.assertEqual(eta.remaining(), 0.0)

    def test_eta_after_a_tenth_of_the_mutants_is_close_to_the_real_remaining_time(self) -> None:
        generator = random.Random(42)  # noqa: S311 - a reproducible simulation, not a secret
        mutants = sorted((SimulatedMutant(generator) for _ in range(1000)), key=lambda mutant: mutant.test_seconds)
        builds = BuildTimes()
        eta = Eta([mutant.test_seconds for mutant in mutants], [mutant.file for mutant in mutants], builds)
        tenth = len(mutants) // 10
        for index, mutant in enumerate(mutants[:tenth]):
            builds.observe(mutant.file, mutant.build)
            eta.finish(index, mutant.build + mutant.testing, build_seconds=mutant.build)
        actual = sum(mutant.build + mutant.testing for mutant in mutants[tenth:])
        estimate = eta.remaining()
        assert estimate is not None
        self.assertLess(abs(estimate - actual) / actual, ETA_TOLERANCE, f"ETA {estimate:.0f}s, really {actual:.0f}s")


class FormatEtaTest(unittest.TestCase):
    def test_hours_minutes_and_seconds(self) -> None:
        self.assertEqual(format_eta(12.4), "12s")
        self.assertEqual(format_eta(245), "4m05s")
        self.assertEqual(format_eta(2 * 3600 + 10 * 60 + 20), "2h10m")
        self.assertEqual(format_eta(3599.6), "1h00m")


if __name__ == "__main__":
    unittest.main()
