import unittest
from pathlib import Path

from strain.coverage import FileCoverage, FunctionRange, LineCoverage, llvm_gcov_report, merge_gcov_json

GCOV_JSON = {
    "format_version": "2",
    "current_working_directory": "/work/build",
    "data_file": "CMakeFiles/lib.dir/src/math.cpp.gcda",
    "files": [
        {
            "file": "/work/src/math.cpp",
            "functions": [
                {"name": "_Z3absi", "demangled_name": "abs(int)", "start_line": 3, "end_line": 6, "execution_count": 2},
                {
                    "name": "_Z4zeroi",
                    "demangled_name": "zero(int)",
                    "start_line": 8,
                    "end_line": 8,
                    "execution_count": 0,
                },
            ],
            "lines": [
                {"line_number": 3, "count": 2, "branches": []},
                {
                    "line_number": 4,
                    "count": 2,
                    "branches": [
                        {"count": 2, "fallthrough": True, "throw": False},
                        {"count": 0, "fallthrough": False, "throw": False},
                        {"count": 0, "fallthrough": False, "throw": True},
                    ],
                },
                {"line_number": 8, "count": 0, "branches": []},
            ],
        },
        {"file": "../include/math.hpp", "functions": [], "lines": [{"line_number": 2, "count": 1, "branches": []}]},
    ],
}

LLVM_GCOV_INTERMEDIATE = """file:/work/src/math.cpp
function:3,2,abs(int)
function:8,0,clamp(int, int)
lcount:3,2
lcount:4,2
branch:4,taken
branch:4,nottaken
lcount:5,0
branch:5,notexec
branch:5,notexec
lcount:6,2
lcount:8,0
lcount:9,0
file:../include/math.hpp
lcount:2,1
"""


class MergeGcovJsonTest(unittest.TestCase):
    def test_lines_branches_and_functions_are_collected(self) -> None:
        coverage: dict[Path, FileCoverage] = {}
        merge_gcov_json(coverage, GCOV_JSON)

        math = coverage[Path("/work/src/math.cpp")]
        self.assertEqual(math.executed_lines(), {3, 4})
        self.assertEqual(math.lines[4], LineCoverage(count=2, branches=2, branches_taken=1))
        self.assertEqual(math.function_at(4), "abs(int)")
        self.assertIsNone(math.function_at(20))

    def test_relative_paths_are_resolved_against_the_gcov_working_directory(self) -> None:
        coverage: dict[Path, FileCoverage] = {}
        merge_gcov_json(coverage, GCOV_JSON)
        self.assertIn(Path("/work/include/math.hpp"), coverage)

    def test_counts_from_several_translation_units_are_summed(self) -> None:
        coverage: dict[Path, FileCoverage] = {}
        merge_gcov_json(coverage, GCOV_JSON)
        merge_gcov_json(coverage, GCOV_JSON)
        math = coverage[Path("/work/src/math.cpp")]
        self.assertEqual(math.lines[4], LineCoverage(count=4, branches=2, branches_taken=1))
        self.assertEqual(len(math.functions), 2)


class LlvmGcovReportTest(unittest.TestCase):
    def test_lines_and_branches_are_collected(self) -> None:
        coverage: dict[Path, FileCoverage] = {}
        merge_gcov_json(coverage, llvm_gcov_report(LLVM_GCOV_INTERMEDIATE, Path("/work/build")))

        math = coverage[Path("/work/src/math.cpp")]
        self.assertEqual(math.executed_lines(), {3, 4, 6})
        self.assertEqual(math.lines[4], LineCoverage(count=2, branches=2, branches_taken=1))
        self.assertEqual(math.lines[5], LineCoverage(count=0, branches=2, branches_taken=0))

    def test_a_function_ends_at_its_last_line_before_the_next_function(self) -> None:
        coverage: dict[Path, FileCoverage] = {}
        merge_gcov_json(coverage, llvm_gcov_report(LLVM_GCOV_INTERMEDIATE, Path("/work/build")))

        math = coverage[Path("/work/src/math.cpp")]
        self.assertEqual(math.functions, [FunctionRange("abs(int)", 3, 6), FunctionRange("clamp(int, int)", 8, 9)])
        self.assertIsNone(math.function_at(7))

    def test_relative_paths_are_resolved_against_the_given_working_directory(self) -> None:
        coverage: dict[Path, FileCoverage] = {}
        merge_gcov_json(coverage, llvm_gcov_report(LLVM_GCOV_INTERMEDIATE, Path("/work/build")))
        self.assertEqual(coverage[Path("/work/include/math.hpp")].executed_lines(), {2})


if __name__ == "__main__":
    unittest.main()
