import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from strain.compile_time import (
    NotMutated,
    compile_time_check_failure,
    constant_functions,
    constant_lines,
    not_mutated,
)
from strain.mutators import Mutant
from strain.results import MutantState
from strain.runner import build_failure_state, failure_description, relative_paths

SOURCE = """\
template <typename T> constexpr T twice(T x) noexcept(true)
{
  if constexpr (sizeof(T) > 1) { return x + x; }
  return x;
}
consteval int square(int value) { return value * value; }
constexpr int answer = 42;
static constexpr int table[] = { 1, 2 };
constexpr Point origin{ 0, 0 };
constexpr int declared_only(int value);
constexpr auto lambda = [](int value) { return value; };
struct S
{
  constexpr S &operator=(const S &) { return *this; }
};
constexpr int outer(int v)
{
  constexpr auto inner = [](int w) constexpr { return w; };
  // constexpr int commented(int v) { return v; }
  return inner(v);
}
"""


class ConstantFunctionsTest(unittest.TestCase):
    def test_finds_function_bodies_and_skips_variables_declarations_and_if_constexpr(self) -> None:
        self.assertEqual(
            [(function.name, function.keyword, sorted(function.body_lines)) for function in constant_functions(SOURCE)],
            [
                ("twice", "constexpr", [3, 4]),
                ("square", "consteval", [6]),
                ("operator", "constexpr", [14]),
                ("outer", "constexpr", [18, 20]),
            ],
        )

    def test_each_line_names_its_function(self) -> None:
        self.assertEqual(constant_lines(SOURCE)[3], "twice")
        self.assertNotIn(7, constant_lines(SOURCE))


class CompileTimeCheckFailureTest(unittest.TestCase):
    def test_failed_checks_from_gcc_and_clang(self) -> None:
        for diagnostic in (
            "a.cpp:2:33: error: static assertion failed",
            "a.cpp:4:22: error: non-constant condition for static assertion",
            "a.cpp:3:38: error: '(10 / 0)' is not a constant expression",
            "a.cpp:2:15: error: static assertion failed due to requirement 'clamp(9, 4) == 3'",
            "a.cpp:2:15: error: static_assert expression is not an integral constant expression",
            "a.cpp:5:17: error: constexpr variable 'x' must be initialized by a constant expression",
        ):
            with self.subTest(diagnostic=diagnostic):
                self.assertEqual(
                    compile_time_check_failure(f"[1/2] Building\n{diagnostic}\nninja: build stopped"), diagnostic
                )

    def test_invalid_code_is_not_a_failed_check(self) -> None:
        self.assertIsNone(compile_time_check_failure("b.cpp:1:27: error: expected primary-expression before '/' token"))

    def test_a_failed_check_kills_the_mutant_and_any_other_failure_is_a_compile_error(self) -> None:
        killed = failure_description("a.cpp:2:33: error: static assertion failed", "build failed")
        self.assertEqual(killed, "killed at compile time: a.cpp:2:33: error: static assertion failed")
        self.assertIs(build_failure_state(killed), MutantState.KILLED_AT_COMPILE_TIME)
        # The build cache keeps the description, so a cached failure keeps its state.
        self.assertIs(build_failure_state(f"{killed}, known from the cache"), MutantState.KILLED_AT_COMPILE_TIME)
        self.assertIs(
            build_failure_state(failure_description("error: expected ';'", "build failed")), MutantState.COMPILE_ERROR
        )


class RelativePathsTest(unittest.TestCase):
    def test_paths_into_the_project_are_shortened_through_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            real = Path(folder, "real", "project")
            (real / "tests").mkdir(parents=True)
            (real / "tests" / "a.cpp").write_text("")
            link = Path(folder, "link")
            link.symlink_to(real.parent)
            source_dir = real.resolve()
            diagnostics = (
                f"{link}/project/tests/a.cpp:3:15: error: static assertion failed\n"
                f"{source_dir}/tests/a.cpp:4:1: note: here\n"
                "/usr/include/c++/v1/vector:10:2: note: elsewhere"
            )
            self.assertEqual(
                relative_paths(diagnostics, source_dir),
                "tests/a.cpp:3:15: error: static assertion failed\n"
                "tests/a.cpp:4:1: note: here\n"
                "/usr/include/c++/v1/vector:10:2: note: elsewhere",
            )


class NotMutatedTest(unittest.TestCase):
    def test_groups_by_file_and_function_with_a_line_range(self) -> None:
        def planned(line: int, column: int) -> SimpleNamespace:
            return SimpleNamespace(
                mutant=Mutant("include/limits.hpp", line, column, line * 100 + column, "ror", "<", "<=")
            )

        found = [
            (planned(7, 13), "clamp"),
            (planned(7, 20), "clamp"),
            (planned(8, 13), "clamp"),
            (planned(12, 5), "square"),
        ]
        self.assertEqual(
            not_mutated(found),  # type: ignore[arg-type]
            [
                NotMutated("include/limits.hpp", "clamp", (7, 8), 3),
                NotMutated("include/limits.hpp", "square", (12,), 1),
            ],
        )
        self.assertEqual(not_mutated(found)[0].summary(), "include/limits.hpp:7-8 clamp(): 3 mutants on 2 lines")  # type: ignore[arg-type]
        self.assertEqual(not_mutated(found)[1].summary(), "include/limits.hpp:12 square(): 1 mutant on 1 line")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
