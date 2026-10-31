import unittest
from pathlib import Path

from strain.build import CompileCommand, CompilerIdentity
from strain.prerequisites import (
    GCOV_MISSING,
    NO_COVERAGE,
    SOURCE_BASED_COVERAGE,
    coverage_problem,
    gcov_identity,
    gcov_problem,
    no_coverage_data,
    prerequisite_problems,
)

GNU_GCOV = "gcov (Ubuntu 13.2.0-23ubuntu4) 13.2.0\nCopyright (C) 2023 Free Software Foundation, Inc.\n"
LLVM_GCOV = "LLVM (http://llvm.org/):\n  LLVM version 17.0.6\n  Optimized build.\n"
GCC_13 = CompilerIdentity("GNU", "13.2.0")


def command(*arguments: str) -> CompileCommand:
    return CompileCommand(directory=Path("/b"), file=Path("/s/a.cpp"), arguments=("c++", *arguments, "-c", "a.cpp"))


class CoverageProblemTest(unittest.TestCase):
    def test_one_instrumented_translation_unit_is_enough(self) -> None:
        self.assertIsNone(coverage_problem([command(), command("--coverage")]))

    def test_no_instrumentation_explains_the_false_pass(self) -> None:
        self.assertEqual(coverage_problem([command()]), NO_COVERAGE)
        self.assertIn("add --coverage", NO_COVERAGE)
        self.assertIn("report a pass", NO_COVERAGE)

    def test_source_based_coverage_asks_for_gcov_coverage(self) -> None:
        self.assertEqual(coverage_problem([command("-fprofile-instr-generate", "-fcoverage-mapping")]), SOURCE_BASED_COVERAGE)
        self.assertIn("--coverage", SOURCE_BASED_COVERAGE)


class GcovProblemTest(unittest.TestCase):
    def test_version_output_is_parsed(self) -> None:
        self.assertEqual(gcov_identity(GNU_GCOV), CompilerIdentity("GNU", "13.2.0"))
        self.assertEqual(gcov_identity(LLVM_GCOV), CompilerIdentity("Clang", "17.0.6"))
        self.assertIsNone(gcov_identity("something else\n"))

    def test_matching_gcov_is_accepted(self) -> None:
        self.assertIsNone(gcov_problem(GCC_13, GNU_GCOV))
        self.assertIsNone(gcov_problem(CompilerIdentity("GNU", "13.1.0"), GNU_GCOV))
        self.assertIsNone(gcov_problem(CompilerIdentity("AppleClang", "17.0.0"), LLVM_GCOV))

    def test_missing_gcov_is_reported(self) -> None:
        self.assertEqual(gcov_problem(GCC_13, None), GCOV_MISSING)

    def test_other_gcc_version_names_the_matching_gcov(self) -> None:
        problem = gcov_problem(CompilerIdentity("GNU", "14.1.0"), GNU_GCOV)
        assert problem is not None
        self.assertIn("gcov is GNU 13.2.0 but the project is compiled by GNU 14.1.0", problem)
        self.assertIn("put gcov-14 first on PATH", problem)

    def test_clang_with_gnu_gcov_names_llvm_cov(self) -> None:
        problem = gcov_problem(CompilerIdentity("Clang", "18.1.3"), GNU_GCOV)
        assert problem is not None
        self.assertIn("llvm-cov-18 gcov", problem)

    def test_unknown_compiler_is_not_checked(self) -> None:
        self.assertIsNone(gcov_problem(None, GNU_GCOV))


class PrerequisiteProblemsTest(unittest.TestCase):
    def test_all_problems_are_reported_together(self) -> None:
        self.assertEqual(prerequisite_problems([command()], GCC_13, None), [NO_COVERAGE, GCOV_MISSING])

    def test_ready_build_has_no_problems(self) -> None:
        self.assertEqual(prerequisite_problems([command("--coverage")], GCC_13, GNU_GCOV), [])

    def test_missing_coverage_data_points_at_the_linker_flags(self) -> None:
        message = no_coverage_data("unit_tests")
        self.assertIn("test unit_tests passed but no coverage data", message)
        self.assertIn("CMAKE_EXE_LINKER_FLAGS", message)


if __name__ == "__main__":
    unittest.main()
