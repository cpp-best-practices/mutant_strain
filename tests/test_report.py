import io
import json
import tempfile
import unittest
from pathlib import Path

from strain.mutators import Mutant
from strain.report import EventLog, action_items, compiler_warning, mutation_testing_elements, summary_line
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
        self.assertEqual([(entry.mutant, entry.state) for entry in combined],
                         [(LESS, MutantState.KILLED), (PLUS, MutantState.SURVIVED)])
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
