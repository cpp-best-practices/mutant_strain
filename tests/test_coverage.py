import unittest
from pathlib import Path

from strain.coverage import FileCoverage, LineCoverage, merge_gcov_json

GCOV_JSON = {
    "format_version": "2",
    "current_working_directory": "/work/build",
    "data_file": "CMakeFiles/lib.dir/src/math.cpp.gcda",
    "files": [
        {
            "file": "/work/src/math.cpp",
            "functions": [
                {"name": "_Z3absi", "demangled_name": "abs(int)", "start_line": 3, "end_line": 6, "execution_count": 2},
                {"name": "_Z4zeroi", "demangled_name": "zero(int)", "start_line": 8, "end_line": 8, "execution_count": 0},
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


if __name__ == "__main__":
    unittest.main()
