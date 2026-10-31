import unittest

from strain.mutators import Mutant, apply_mutant, generate_mutants, suppressed_lines


def summary(source: str) -> list[tuple[str, str, str]]:
    return [(mutant.operator, mutant.original, mutant.replacement) for mutant in generate_mutants("f.cpp", source)]


class GenerateMutantsTest(unittest.TestCase):
    def test_relational_operator_replacement(self) -> None:
        self.assertEqual(summary("bool f(int a, int b) { return a < b; }"), [("ror", "<", "<="), ("ror", "<", ">=")])

    def test_equality_operator_replacement(self) -> None:
        self.assertEqual(summary("x == y; x != y;"), [("ror", "==", "!="), ("ror", "!=", "==")])

    def test_logical_connector_replacement(self) -> None:
        self.assertEqual(summary("a && b || c;"), [("lcr", "&&", "||"), ("lcr", "||", "&&")])

    def test_arithmetic_operator_replacement(self) -> None:
        self.assertEqual(
            summary("r = a + b - c * d / e % f;"),
            [("aor", "+", "-"), ("aor", "-", "+"), ("aor", "*", "/"), ("aor", "/", "*"), ("aor", "%", "*")],
        )

    def test_assignment_operator_replacement(self) -> None:
        self.assertEqual(summary("a += 1; b *= 2;"), [("asr", "+=", "-="), ("asr", "*=", "/=")])

    def test_unary_operators_are_not_mutated(self) -> None:
        self.assertEqual(summary("return -x; y = *p; z = (-1);"), [])

    def test_template_brackets_are_not_mutated(self) -> None:
        source = "std::vector<std::pair<int, long>> v; auto n = static_cast<int>(x); template <typename T> void g();"
        self.assertEqual(summary(source), [])

    def test_comparison_that_looks_like_a_template_start_is_mutated(self) -> None:
        self.assertEqual(summary("for (i = 0; i < n; ++i) {}"), [("ror", "<", "<="), ("ror", "<", ">=")])

    def test_comparison_with_call_is_mutated(self) -> None:
        self.assertEqual(len(summary("if (i < v.size()) {}")), 2)

    def test_operator_overload_names_are_not_mutated(self) -> None:
        self.assertEqual(summary("bool operator<(const T &) const; bool operator==(const T &) const;"), [])

    def test_line_filter(self) -> None:
        mutants = generate_mutants("f.cpp", "a + b;\nc - d;\n", lines={2})
        self.assertEqual([(mutant.line, mutant.original) for mutant in mutants], [(2, "-")])

    def test_locations(self) -> None:
        mutant = generate_mutants("f.cpp", "int x;\n  y == z;")[0]
        self.assertEqual((mutant.line, mutant.column, mutant.end_column, mutant.offset), (2, 5, 7, 11))

    def test_max_per_line(self) -> None:
        mutants = generate_mutants("f.cpp", "a < b && c < d;\ne + f;", max_per_line=2)
        self.assertEqual([(mutant.line, mutant.replacement) for mutant in mutants], [(1, "<="), (1, ">="), (2, "-")])


class MutantIdTest(unittest.TestCase):
    def test_id_is_eight_hex_digits_and_stable(self) -> None:
        mutant = Mutant(file="src/a.cpp", line=3, column=7, offset=40, operator="ror", original="<", replacement="<=")
        same = Mutant(file="src/a.cpp", line=3, column=7, offset=40, operator="ror", original="<", replacement="<=")
        self.assertRegex(mutant.id, r"^[0-9a-f]{8}$")
        self.assertEqual(mutant.id, same.id)

    def test_id_depends_on_replacement(self) -> None:
        first, second = generate_mutants("f.cpp", "a < b;")
        self.assertNotEqual(first.id, second.id)


class ApplyMutantTest(unittest.TestCase):
    def test_replaces_only_the_operator(self) -> None:
        source = "int f() { return a < b; }"
        mutant = generate_mutants("f.cpp", source)[0]
        self.assertEqual(apply_mutant(source, mutant), "int f() { return a <= b; }")


class SuppressedLinesTest(unittest.TestCase):
    def test_mutate_off_comment(self) -> None:
        self.assertEqual(suppressed_lines("a + b; // mutate-off\nc + d;\n", arid=()), {1})

    def test_arid_calls(self) -> None:
        source = "LOG_INFO(a + b);\nspdlog::info(\"{}\", c - d);\nkeep(e * f);\n"
        self.assertEqual(suppressed_lines(source, arid=("LOG_*", "spdlog::*")), {1, 2})


if __name__ == "__main__":
    unittest.main()
