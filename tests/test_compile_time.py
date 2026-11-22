import unittest

from strain.compile_time import compile_time_only_lines, constexpr_function_lines, is_constant_evaluation_failure
from strain.coverage import FileCoverage, LineCoverage

SOURCE = """#include "sample.hpp"

int add(int lhs, int rhs) { return lhs + rhs; }

constexpr int square(int value) { return value * value; }

template <typename T>
consteval T twice(T value)
{
  return value + value;
}

constexpr int limit = square(3) + 1;
constexpr int table[] = { 1, 2 };
static_assert(square(3) == 9);

int pick(int value)
{
  if constexpr (sizeof(int) == 4) { return value - 1; }
  return value;
}

struct Point
{
  constexpr Point(int x, int y) : m_x{x}, m_y(y + 1) {}
  constexpr Point &operator=(const Point &) = default;
  int m_x;
  int m_y;
};
"""

GCC_STATIC_ASSERT = """sample.cpp:12:25: error: static assertion failed
   12 | static_assert(square(3) == 9);
      |               ~~~~~~~~~~^~~~
sample.cpp:12:25: note: the comparison reduces to '(1 == 9)'
"""
GCC_NOT_CONSTANT = """sample.cpp:12:22: error: non-constant condition for static assertion
sample.cpp:12:21: error: 'twice(3)' is not a constant expression
sample.cpp:7:3: note: in 'constexpr' expansion of 'twice(3)'
"""
CLANG_STATIC_ASSERT = "sample.cpp:12:15: error: static assertion failed due to requirement 'square(3) == 9'\n"
CLANG_CONSTEVAL = "sample.cpp:9:10: error: call to consteval function 'twice' is not a constant expression\n"
INVALID_EXPRESSION = """sample.cpp:9:34: error: expected ',' or '...' before '/' token
sample.cpp:13:15: error: non-constant condition for static assertion
"""
BUILD_FOOTER = "gmake[2]: *** [CMakeFiles/sample.dir/build.make:76: sample.cpp.o] Error 1\n"


class ConstexprFunctionLinesTest(unittest.TestCase):
    def test_spans_the_constexpr_and_consteval_functions_only(self) -> None:
        self.assertEqual(constexpr_function_lines(SOURCE), {5, 8, 9, 10, 11, 25})

    def test_an_unterminated_function_spans_nothing(self) -> None:
        self.assertEqual(constexpr_function_lines("constexpr int broken(int value) { return value"), set())


class CompileTimeOnlyLinesTest(unittest.TestCase):
    def test_keeps_the_lines_of_constexpr_functions_with_operators_and_no_runtime_coverage(self) -> None:
        coverage = FileCoverage(lines={3: LineCoverage(1, 0, 0), 5: LineCoverage(0, 0, 0), 25: LineCoverage(2, 0, 0)})
        self.assertEqual(compile_time_only_lines(SOURCE, coverage, {3, 5, 10, 12, 15, 19, 25}), {5, 10})


class ConstantEvaluationFailureTest(unittest.TestCase):
    def test_a_failed_static_assert_or_constant_evaluation_is_a_detection(self) -> None:
        for diagnostics in (GCC_STATIC_ASSERT, GCC_NOT_CONSTANT, CLANG_STATIC_ASSERT, CLANG_CONSTEVAL + BUILD_FOOTER):
            with self.subTest(diagnostics=diagnostics):
                self.assertTrue(is_constant_evaluation_failure(diagnostics))

    def test_invalid_code_is_not_a_detection_even_when_a_static_assert_fails_after_it(self) -> None:
        self.assertFalse(is_constant_evaluation_failure(INVALID_EXPRESSION))

    def test_no_diagnostics_is_not_a_detection(self) -> None:
        self.assertFalse(is_constant_evaluation_failure(BUILD_FOOTER))
        self.assertFalse(is_constant_evaluation_failure(""))


if __name__ == "__main__":
    unittest.main()
