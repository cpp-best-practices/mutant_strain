import io
import json
import tempfile
import unittest
from pathlib import Path

from strain.mutators import Mutant
from strain.html_report import html_report
from strain.report import (
    NOT_REACHED_HINT,
    EventLog,
    FileScore,
    action_items,
    compiler_warning,
    file_scores,
    markdown_summary,
    mutation_testing_elements,
    source_excerpt,
    summary_line,
)
from strain.results import MutantResult, MutantState, SurvivorKind, combine

LESS = Mutant(file="src/math.cpp", line=4, column=9, offset=30, operator="ror", original="<", replacement="<=")
PLUS = Mutant(file="src/math.cpp", line=9, column=12, offset=90, operator="aor", original="+", replacement="-")
AND = Mutant(file="src/math.cpp", line=13, column=7, offset=140, operator="lcr", original="&&", replacement="||")


def result(
    mutant: Mutant,
    test: str,
    state: MutantState,
    survivor: SurvivorKind | None = None,
    function: str | None = "abs(int)",
) -> MutantResult:
    return MutantResult(mutant=mutant, test=test, state=state, survivor=survivor, function=function)


class CombineTest(unittest.TestCase):
    def test_strongest_state_wins_across_tests(self) -> None:
        combined = combine(
            [
                result(LESS, "a", MutantState.SURVIVED, SurvivorKind.NOT_DETECTED),
                result(LESS, "b", MutantState.KILLED),
                result(PLUS, "a", MutantState.SURVIVED, SurvivorKind.NOT_REACHED),
                result(PLUS, "b", MutantState.SURVIVED, SurvivorKind.NOT_DETECTED),
            ]
        )
        self.assertEqual(
            [(entry.mutant, entry.state) for entry in combined],
            [(LESS, MutantState.KILLED), (PLUS, MutantState.SURVIVED)],
        )
        self.assertEqual(combined[0].killed_by, ("b",))
        self.assertEqual(combined[0].covered_by, ("a", "b"))
        self.assertIs(combined[1].survivor, SurvivorKind.NOT_DETECTED)


class ConsoleOutputTest(unittest.TestCase):
    def test_summary_line(self) -> None:
        results = (
            [result(LESS, "parser_tests", MutantState.KILLED)] * 31
            + [result(LESS, "parser_tests", MutantState.KILLED_BY_SANITIZER)] * 4
            + [result(LESS, "parser_tests", MutantState.TIMEOUT)]
            + [result(LESS, "parser_tests", MutantState.SURVIVED)] * 5
        )
        self.assertEqual(
            summary_line("parser_tests", results, width=12),
            "parser_tests   score 87.8%   killed 31  sanitizer 4  timeout 1  survived 5",
        )

    def test_summary_without_scored_mutants(self) -> None:
        self.assertEqual(
            summary_line("t", [result(LESS, "t", MutantState.COMPILE_ERROR)], width=1),
            "t   score n/a   killed 0  sanitizer 0  timeout 0  survived 0",
        )

    def test_compiler_style_warning(self) -> None:
        combined = combine([result(LESS, "t", MutantState.SURVIVED, SurvivorKind.NOT_REACHED)])[0]
        self.assertEqual(
            compiler_warning(combined, Path("/src")),
            "/src/src/math.cpp:4:9: warning: survived [ror] `<` → `<=` (not_reached)",
        )

    def test_action_items(self) -> None:
        combined = combine(
            [
                result(LESS, "t1", MutantState.SURVIVED, SurvivorKind.NOT_REACHED, function="clamp(int)"),
                result(PLUS, "t1", MutantState.SURVIVED, SurvivorKind.NOT_DETECTED, function="offset(int)"),
                result(PLUS, "t2", MutantState.KILLED, function="offset(int)"),
                result(AND, "t2", MutantState.SURVIVED, function=None),
            ]
        )
        self.assertEqual(
            action_items(combined, limit=None),
            [
                "clamp() is pseudo-tested: nothing checks its result",
                "test t1 kills nothing: its assertions never fail on a mutant",
                "src/math.cpp:4 `<` → `<=` survived (not_reached): add a test input that takes the other branch",
                "src/math.cpp:13 `&&` → `||` survived: add a test where `&&` and `||` give different results",
            ],
        )

    def test_action_items_are_limited(self) -> None:
        combined = combine([result(LESS, "t1", MutantState.SURVIVED, function=None)])
        self.assertEqual(len(action_items(combined, limit=1)), 1)


SOURCE = (
    "#include <cmath>\n"
    "\n"
    "int abs(int value)\n"
    "{\tif (value < 0) { return -value; }\n"
    "  return value;\n"
    "}\n"
    "\n"
    "\n"
    "int add(int lhs, int rhs) { return lhs + rhs; }\n"
)
OTHER = Mutant(file="src/other.cpp", line=1, column=1, offset=0, operator="ror", original="==", replacement="!=")


class SourceExcerptTest(unittest.TestCase):
    def test_caret_points_at_the_operator_and_the_fix_it_shows_the_replacement(self) -> None:
        mutant = Mutant(
            file="src/math.cpp", line=4, column=13, offset=0, operator="ror", original="<", replacement="<="
        )
        self.assertEqual(
            source_excerpt(mutant, SOURCE),
            [
                "    4 | {   if (value < 0) { return -value; }",
                "      |               ^",
                "      |               <=",
            ],
        )

    def test_multi_character_operators_are_underlined(self) -> None:
        mutant = Mutant(
            file="src/math.cpp", line=1, column=1, offset=0, operator="lcr", original="&&", replacement="||"
        )
        self.assertEqual(source_excerpt(mutant, "&& x\n"), ["    1 | && x", "      | ^~", "      | ||"])

    def test_lines_past_the_end_of_the_source_give_no_excerpt(self) -> None:
        self.assertEqual(source_excerpt(AND, "one line\n"), [])


class FileScoresTest(unittest.TestCase):
    def test_scores_are_grouped_by_file(self) -> None:
        combined = combine(
            [
                result(LESS, "t", MutantState.KILLED),
                result(PLUS, "t", MutantState.SURVIVED),
                result(AND, "t", MutantState.COMPILE_ERROR),
                result(OTHER, "t", MutantState.TIMEOUT),
            ]
        )
        self.assertEqual(
            file_scores(combined),
            [
                FileScore(file="src/math.cpp", mutants=3, detected=1, survived=1),
                FileScore(file="src/other.cpp", mutants=1, detected=1, survived=0),
            ],
        )
        self.assertEqual(file_scores(combined)[0].score, 0.5)
        self.assertIsNone(FileScore(file="f", mutants=1, detected=0, survived=0).score)


class MarkdownSummaryTest(unittest.TestCase):
    def test_summary_has_score_files_and_survivors(self) -> None:
        combined = combine(
            [
                result(LESS, "t", MutantState.KILLED),
                result(PLUS, "t", MutantState.SURVIVED, SurvivorKind.NOT_DETECTED),
                result(OTHER, "t", MutantState.SURVIVED, function=None),
            ]
        )
        self.assertEqual(
            markdown_summary(combined, threshold=50.0).splitlines(),
            [
                "## strain mutation testing",
                "",
                "**Mutation score: 33.3%** ❌ below the 50% threshold",
                "",
                "3 mutants: killed 1, survived 2",
                "",
                "| File | Mutants | Detected | Survived | Score |",
                "| --- | ---: | ---: | ---: | ---: |",
                "| `src/math.cpp` | 2 | 1 | 1 | 50.0% |",
                "| `src/other.cpp` | 1 | 0 | 1 | 0.0% |",
                "",
                "### Survivors (2)",
                "",
                "- `src/math.cpp:9:12` `+` → `-` (not_detected): assert the computed value",
                "- `src/other.cpp:1:1` `==` → `!=`: add a test where `==` and `!=` give different results",
            ],
        )

    def test_summary_without_survivors_meets_the_threshold(self) -> None:
        markdown = markdown_summary(combine([result(LESS, "t", MutantState.KILLED)]), threshold=90.0)
        self.assertIn("**Mutation score: 100.0%** ✅ meets the 90% threshold\n", markdown)
        self.assertNotIn("Survivors", markdown)

    def test_summary_without_mutants(self) -> None:
        self.assertIn("No mutants were generated.", markdown_summary([], threshold=None))

    def test_pipes_in_file_names_do_not_break_the_table(self) -> None:
        pipe = Mutant(file="a|b.cpp", line=1, column=1, offset=0, operator="lcr", original="||", replacement="&&")
        markdown = markdown_summary(combine([result(pipe, "t", MutantState.SURVIVED)]), threshold=None)
        self.assertIn("| `a\\|b.cpp` |", markdown)


class HtmlReportTest(unittest.TestCase):
    def test_report_shows_escaped_source_with_its_mutants(self) -> None:
        combined = combine(
            [
                result(LESS, "t1", MutantState.KILLED),
                result(PLUS, "t1", MutantState.SURVIVED, SurvivorKind.NOT_REACHED),
            ]
        )
        page = html_report(combined, {"src/math.cpp": SOURCE})
        self.assertTrue(page.startswith("<!DOCTYPE html>"))
        self.assertIn("<title>strain mutation report: 50.0%</title>", page)
        self.assertIn("#include &lt;cmath&gt;", page)
        self.assertIn('id="src/math.cpp:4"', page)
        self.assertIn('class="line survived"', page)
        self.assertIn('class="line killed"', page)
        self.assertIn("<code>&lt;</code> → <code>&lt;=</code>", page)
        self.assertIn("killed by t1", page)
        self.assertIn(NOT_REACHED_HINT, page)
        self.assertIn('href="#src/math.cpp:9"', page)
        self.assertNotIn("<script", page)

    def test_report_without_mutants(self) -> None:
        page = html_report([], {})
        self.assertIn("<title>strain mutation report: n/a</title>", page)
        self.assertIn("No mutants were generated", page)


class MutationTestingElementsTest(unittest.TestCase):
    def test_schema_shape(self) -> None:
        combined = combine(
            [
                result(LESS, "t1", MutantState.KILLED_BY_SANITIZER),
                result(PLUS, "t1", MutantState.COMPILE_ERROR),
            ]
        )
        report = mutation_testing_elements(combined, {"src/math.cpp": "source text"})
        self.assertEqual(report["schemaVersion"], "1")
        self.assertEqual(report["thresholds"], {"high": 80, "low": 60})
        file_report = report["files"]["src/math.cpp"]
        self.assertEqual(file_report["language"], "cpp")
        self.assertEqual(file_report["source"], "source text")
        self.assertEqual(
            file_report["mutants"][0],
            {
                "id": LESS.id,
                "mutatorName": "ror",
                "replacement": "<=",
                "location": {"start": {"line": 4, "column": 9}, "end": {"line": 4, "column": 10}},
                "status": "Killed",
                "statusReason": "killed_by_sanitizer",
                "killedBy": ["t1"],
                "coveredBy": ["t1"],
            },
        )
        self.assertEqual(file_report["mutants"][1]["status"], "CompileError")


class EventLogTest(unittest.TestCase):
    def test_events_are_written_as_json_lines_and_echoed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            echo = io.StringIO()
            with EventLog(path, echo=echo) as events:
                events.emit("baseline", test="t1", status="ok")
                events.emit("mutant", test="t1", id="abcd1234", state=MutantState.KILLED)
            lines = path.read_text().splitlines()
            self.assertEqual([json.loads(line)["event"] for line in lines], ["baseline", "mutant"])
            self.assertEqual(json.loads(lines[1])["state"], "killed")
            self.assertEqual(echo.getvalue().splitlines(), lines)

    def test_previous_results_are_read_back_for_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            with EventLog(path) as events:
                events.emit("mutant", test="t1", id="abcd1234", state=MutantState.SURVIVED, survivor="not_reached")
            with EventLog(path, resume=True) as events:
                self.assertEqual(
                    events.previous_results(),
                    {("t1", "abcd1234"): (MutantState.SURVIVED, SurvivorKind.NOT_REACHED)},
                )
                events.emit("mutant", test="t2", id="abcd1234", state=MutantState.KILLED)
            self.assertEqual(len(path.read_text().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
