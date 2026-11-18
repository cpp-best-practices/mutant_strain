import unittest

from strain.mutators import Mutant
from strain.patterns import (
    AND_TO_OR,
    BOUNDARY,
    find_hotspots,
    find_patterns,
    pattern_lines,
    patterns_json,
)
from strain.results import CombinedResult, MutantState, SurvivorKind


def entry(
    original: str,
    replacement: str,
    operator: str = "ror",
    file: str = "src/parser.cpp",
    state: MutantState = MutantState.SURVIVED,
    function: str | None = "accept(char)",
    survivor: SurvivorKind | None = SurvivorKind.NOT_DETECTED,
    offset: int = 0,
) -> CombinedResult:
    mutant = Mutant(file, 1, offset + 1, offset, operator, original, replacement)
    return CombinedResult(mutant, state, survivor, function, (), ("t",))


class PatternsTest(unittest.TestCase):
    def test_survivors_group_by_the_kind_of_change_and_file(self) -> None:
        combined = [
            entry("&&", "||", "lcr", offset=1),
            entry("&&", "||", "lcr", offset=2),
            entry("&&", "||", "lcr", offset=3),
            entry("<", "<=", offset=4),
            entry(">=", ">", offset=5),
            entry("&&", "||", "lcr", file="src/lexer.cpp", offset=6),
            entry("&&", "||", "lcr", state=MutantState.KILLED, offset=7),
        ]
        patterns = find_patterns(combined)
        self.assertEqual(
            [(pattern.family, pattern.file, len(pattern.mutant_ids)) for pattern in patterns],
            [(AND_TO_OR, "src/parser.cpp", 3), (BOUNDARY, "src/parser.cpp", 2)],
        )
        self.assertEqual(
            patterns[0].summary(),
            "3 survivors turn `&&` into `||` in parser.cpp: add inputs that must be rejected, "
            "since the tests check only what is accepted",
        )

    def test_each_survivor_is_in_one_primary_pattern_at_most(self) -> None:
        combined = [
            entry(original, replacement, offset=index)
            for index, (original, replacement) in enumerate(
                [("<", "<="), ("<", ">="), ("<=", "<"), ("<=", ">"), ("==", "!="), ("!=", "==")]
            )
        ]
        ids = [mutant_id for pattern in find_patterns(combined) for mutant_id in pattern.mutant_ids]
        self.assertEqual(sorted(ids), sorted(item.mutant.id for item in combined))
        self.assertEqual(len(ids), len(set(ids)))

    def test_hotspots_split_new_inputs_from_stronger_assertions(self) -> None:
        combined = [
            entry("+", "-", "aor", function="scale(int)", survivor=SurvivorKind.NOT_REACHED, offset=1),
            entry("*", "/", "aor", function="scale(int)", survivor=SurvivorKind.NOT_REACHED, offset=2),
            entry("<", "<=", function="scale(int)", offset=3),
            entry("<", "<=", function="clamp(int)", offset=4),
        ]
        [hotspot] = find_hotspots(combined)
        self.assertEqual(hotspot.summary(), "scale() in parser.cpp has 3 survivors: 2 not_reached, 1 not_detected")

    def test_report_json_marks_hotspots_as_not_primary(self) -> None:
        combined = [entry("&&", "||", "lcr", offset=1), entry("&&", "||", "lcr", offset=2)]
        pattern, hotspot = patterns_json(combined)
        self.assertEqual(
            {key: pattern[key] for key in ("kind", "primary", "file", "survivors")},
            {"kind": "and-to-or", "primary": True, "file": "src/parser.cpp", "survivors": 2},
        )
        self.assertEqual(pattern["mutants"], [item.mutant.id for item in combined])
        self.assertEqual((hotspot["kind"], hotspot["primary"], hotspot["function"]), ("hotspot", False, "accept(char)"))

    def test_a_single_survivor_is_not_a_pattern(self) -> None:
        self.assertEqual(pattern_lines([entry("&&", "||", "lcr", function=None)]), [])


if __name__ == "__main__":
    unittest.main()
