import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from collections.abc import Callable
from pathlib import Path

from strain import cppcheck_engine, tree_sitter_engine
from strain.cli import main
from strain.console import Console
from strain.mutators import Engine, fallback_engine
from strain.coverage import LineCoverage
from strain.results import MutantResult, MutantState
from strain.runner import Runner, RunOptions, StrainError, run

FIXTURE = Path(__file__).parent / "fixtures" / "sample_project"
TOOLS = ("cmake", "ctest", "g++", "gcov", "git")
CXX_FLAGS = "--coverage -fsanitize=address,undefined -fno-omit-frame-pointer -D_GLIBCXX_ASSERTIONS -ftrivial-auto-var-init=pattern"
LINKER_FLAGS = "--coverage -fsanitize=address,undefined"


def run_checked(*command: str | Path, cwd: Path) -> None:
    subprocess.run([str(part) for part in command], cwd=cwd, check=True, capture_output=True)


def configure_project(
    root: Path, build_dir: Path, cxx_flags: str = CXX_FLAGS, build_type: str = "Debug", linker_flags: str = LINKER_FLAGS
) -> None:
    run_checked("git", "init", "-q", cwd=root)
    run_checked("git", "add", ".", cwd=root)
    run_checked(
        "git", "-c", "user.name=strain", "-c", "user.email=strain@example.com", "commit", "-qm", "fixture", cwd=root
    )
    run_checked(
        "cmake",
        "-S",
        root,
        "-B",
        build_dir,
        f"-DCMAKE_BUILD_TYPE={build_type}",
        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        f"-DCMAKE_CXX_FLAGS={cxx_flags}",
        f"-DCMAKE_EXE_LINKER_FLAGS={linker_flags}",
        cwd=root,
    )
    run_checked("cmake", "--build", build_dir, cwd=root)


class RecordingConsole(Console):
    def __init__(self) -> None:
        self.transcript = io.StringIO()
        super().__init__(self.transcript, io.StringIO())
        self.evaluated: list[tuple[MutantResult, str, LineCoverage | None]] = []
        self.finished_runs = 0

    def mutant_evaluated(self, result: MutantResult, source: str, line: LineCoverage | None) -> None:
        self.evaluated.append((result, source, line))

    def run_finished(self) -> None:
        self.finished_runs += 1


class LiveConsole(RecordingConsole):
    """Records whether the run reached the point where `--ui live` takes over the terminal."""

    def __init__(self) -> None:
        super().__init__()
        self.took_over = False

    def run(self, work: Callable[[], int]) -> int:
        self.took_over = True
        return super().run(work)


class InterruptingConsole(RecordingConsole):
    """Presses Ctrl-C once `limit` mutants have been evaluated."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit

    def mutant_evaluated(self, result: MutantResult, source: str, line: LineCoverage | None) -> None:
        super().mutant_evaluated(result, source, line)
        if len(self.evaluated) == self.limit:
            raise KeyboardInterrupt


PARSED_STATES = [
    (3, "aor", "-", "killed"),
    (7, "ror", "<=", "survived"),
    (7, "ror", ">=", "survived"),
    (11, "lcr", "||", "survived"),
    (13, "aor", "/", "killed_by_sanitizer"),
    (15, "aor", "-", "ignored"),
]
# A macro from a header that cppcheck, which analyzes each source alone, cannot expand and so cannot parse around.
UNKNOWN_MACRO_DEFINITION = "#define SAMPLE_LOG(value)\n"
UNKNOWN_MACRO_USE = "\nint logged(int value) { SAMPLE_LOG(value) return value; }\n"
SINGLE_TEST_STATES = [
    (3, "aor", "-", "killed"),
    (7, "ror", "<=", "survived"),
    (7, "ror", ">=", "survived"),
    (11, "lcr", "||", "survived"),
    (13, "aor", "/", "killed_by_sanitizer"),
    (15, "aor", "-", "ignored"),
    (17, "aor", "/", "compile_error"),
    (17, "aor", "/", "compile_error"),
]


RECOGNISER_HEADER = "[[nodiscard]] bool is_identifier_start(char c);\n"
RECOGNISER_SOURCE = """#include "sample.hpp"

bool is_identifier_start(char c)
{
  return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '_';
}
"""
# Weak on purpose: only inputs that must be accepted, none at a limit, none that must be rejected.
RECOGNISER_TEST = """#include "sample.hpp"

int main() { return is_identifier_start('m') && is_identifier_start('M') && is_identifier_start('_') ? 0 : 1; }
"""
OTHER_TESTS_CHECKING_ADD = '#include "sample.hpp"\n\nint main() { return add(1, 1) == 2 ? 0 : 1; }\n'
# square is checked only by a static_assert, so no test executes it; first_of's `*` mutated to `/` is invalid code.
COMPILE_TIME_HEADER = "[[nodiscard]] int add(int lhs, int rhs);\n"
COMPILE_TIME_SOURCE = """#include "sample.hpp"

int add(int lhs, int rhs) { return lhs + rhs; }

using Value = int;

constexpr int square(int value) { return value * value; }

constexpr int first_of(const Value *values) { return values[0]; }

constexpr Value table[] = { 4, 5 };
static_assert(square(3) == 9);
static_assert(first_of(table) == 4);
"""
COMPILE_TIME_TEST = '#include "sample.hpp"\n\nint main() { return add(2, 3) == 5 ? 0 : 1; }\n'


def run_strain(*argv: str) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = main(["run", *argv])
    return exit_code, stdout.getvalue(), stderr.getvalue()


@unittest.skipUnless(all(shutil.which(tool) for tool in TOOLS), "needs cmake, ctest, g++, gcov and git")
class EndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "project"
        shutil.copytree(FIXTURE, self.root)
        self.build_dir = self.tmp / "build"
        self.report_dir = self.tmp / "report"
        self.original_source = (self.root / "src" / "sample.cpp").read_bytes()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def mutant_states(self) -> list[tuple[int, str, str, str]]:
        report = json.loads((self.report_dir / "report.json").read_text())
        mutants = report["files"]["src/sample.cpp"]["mutants"]
        return [
            (
                mutant["location"]["start"]["line"],
                mutant["mutatorName"],
                mutant["replacement"],
                mutant.get("statusReason", mutant["status"]),
            )
            for mutant in mutants
        ]

    def test_single_test_run(self) -> None:
        configure_project(self.root, self.build_dir)

        exit_code, stdout, _ = run_strain(
            "--build-dir",
            str(self.build_dir),
            "--report-dir",
            str(self.report_dir),
            "-R",
            "^sample_tests$",
            "--threshold",
            "50",
            "--engine",
            "lexer",
        )

        self.assertEqual(exit_code, 1, stdout)
        self.assertEqual(self.mutant_states(), SINGLE_TEST_STATES)
        self.assert_single_test_log(stdout)
        self.assertEqual((self.root / "src" / "sample.cpp").read_bytes(), self.original_source)
        self.assertTrue((self.report_dir / "events.jsonl").is_file())

    def assert_single_test_log(self, stdout: str) -> None:
        self.assertIn("sample_tests   score 40.0%   killed 1  sanitizer 1  timeout 0  survived 3", stdout)
        self.assertIn("✓ working tree is clean", stdout)
        self.assertIn("✓ selected 1 test: sample_tests", stdout)
        self.assertIn("▸ Test 1/1: sample_tests", stdout)
        self.assertIn("✓ baseline passed in", stdout)
        self.assertIn("· src/sample.cpp: 8 mutants on 6 lines", stdout)
        self.assertIn("[2/8] survived", stdout)
        self.assertIn("rejected by -fsyntax-only", stdout)
        self.assertIn("✓ sources restored and rebuilt", stdout)
        self.assertIn("mutation score 40.0%   ✗ below --threshold 50%", stdout)
        self.assertIn("clamp_to_zero() is pseudo-tested: nothing checks its result", stdout)
        self.assertIn(f"{self.root.resolve() / 'src' / 'sample.cpp'}:7:13: warning: survived [ror]", stdout)
        self.assertIn("    7 |   if (value < 0) { return 0; }\n      |             ^\n      |             <=\n", stdout)
        self.assertIn("▸ Files", stdout)
        self.assertRegex(stdout, r"src/sample\.cpp\s+40\.0%\s+8 mutants\s+2 detected\s+3 survived")
        self.assertIn("summary.md", stdout)
        self.assertIn(
            "**Mutation score: 40.0%** ❌ below the 50% threshold", (self.report_dir / "summary.md").read_text()
        )
        self.assertIn("clamp_to_zero", (self.report_dir / "index.html").read_text())
        self.assertEqual((self.root / "src" / "sample.cpp").read_bytes(), self.original_source)
        self.assertTrue((self.report_dir / "events.jsonl").is_file())

    def assert_parsing_engine_skips_declarators(self, engine: str) -> None:
        configure_project(self.root, self.build_dir)

        exit_code, stdout, _ = run_strain(
            "--build-dir",
            str(self.build_dir),
            "--report-dir",
            str(self.report_dir),
            "-R",
            "^sample_tests$",
            "--engine",
            engine,
        )

        self.assertEqual(exit_code, 0, stdout)
        self.assertIn(f"✓ mutation engine: {engine}", stdout)
        self.assertEqual(self.mutant_states(), PARSED_STATES)

    @unittest.skipUnless(tree_sitter_engine.is_available(), "needs the tree-sitter and tree-sitter-cpp packages")
    def test_tree_sitter_engine(self) -> None:
        self.assert_parsing_engine_skips_declarators("tree-sitter")

    @unittest.skipUnless(cppcheck_engine.is_available(), "needs cppcheck on PATH")
    def test_cppcheck_engine(self) -> None:
        self.assert_parsing_engine_skips_declarators("cppcheck")

    def assert_test_code_is_not_mutated(self, engine: str) -> None:
        (self.root / "strain.toml").unlink()
        configure_project(self.root, self.build_dir)

        exit_code, stdout, _ = run_strain(
            "--build-dir", str(self.build_dir), "--report-dir", str(self.report_dir), "--engine", engine
        )

        self.assertEqual(exit_code, 0, stdout)
        report = json.loads((self.report_dir / "report.json").read_text())
        self.assertEqual(list(report["files"]), ["src/sample.cpp"])
        self.assertNotIn("tests/sample_tests.cpp", stdout)

    @unittest.skipUnless(cppcheck_engine.is_available(), "needs cppcheck on PATH")
    def test_cppcheck_engine_does_not_mutate_test_code(self) -> None:
        self.assert_test_code_is_not_mutated("cppcheck")

    def test_lexer_engine_does_not_mutate_test_code(self) -> None:
        self.assert_test_code_is_not_mutated("lexer")

    def use_a_macro_cppcheck_cannot_expand(self) -> None:
        header = self.root / "include" / "sample.hpp"
        header.write_text(
            header.read_text().replace("#define SAMPLE_HPP\n", "#define SAMPLE_HPP\n" + UNKNOWN_MACRO_DEFINITION)
        )
        source = self.root / "src" / "sample.cpp"
        source.write_text(source.read_text() + UNKNOWN_MACRO_USE)
        configure_project(self.root, self.build_dir)

    def run_sample_tests(self, engine: str) -> tuple[int, str, str]:
        return run_strain(
            "--build-dir",
            str(self.build_dir),
            "--report-dir",
            str(self.report_dir),
            "-R",
            "^sample_tests$",
            "--engine",
            engine,
        )

    @unittest.skipUnless(cppcheck_engine.is_available(), "needs cppcheck on PATH")
    def test_auto_engine_falls_back_for_a_source_cppcheck_cannot_parse(self) -> None:
        self.use_a_macro_cppcheck_cannot_expand()
        fallback = fallback_engine(Engine.CPPCHECK)

        exit_code, stdout, stderr = self.run_sample_tests("auto")

        self.assertEqual(exit_code, 0, stdout + stderr)
        self.assertIn("✓ mutation engine: cppcheck", stdout)
        self.assertIn("src/sample.cpp: cppcheck cannot parse it, line 22:", stderr)
        self.assertIn(f"[unknownMacro]; using the {fallback} engine for this file", stderr)
        self.assertEqual(self.mutant_states(), SINGLE_TEST_STATES if fallback is Engine.LEXER else PARSED_STATES)

    @unittest.skipUnless(cppcheck_engine.is_available(), "needs cppcheck on PATH")
    def test_cppcheck_engine_stops_at_a_source_it_cannot_parse(self) -> None:
        self.use_a_macro_cppcheck_cannot_expand()

        exit_code, stdout, stderr = self.run_sample_tests("cppcheck")

        self.assertEqual(exit_code, 2, stdout + stderr)
        self.assertIn("src/sample.cpp: cppcheck cannot parse it, line 22:", stderr)
        self.assertIn("[unknownMacro], or pass --engine lexer", stderr)

    def test_per_test_strategy_combines_results(self) -> None:
        configure_project(self.root, self.build_dir)

        exit_code, stdout, _ = run_strain("--build-dir", str(self.build_dir), "--report-dir", str(self.report_dir))

        self.assertEqual(exit_code, 0, stdout)
        self.assertIn("other_tests    score 0.0%   killed 0  sanitizer 0  timeout 0  survived 1", stdout)
        self.assertIn("test other_tests kills nothing", stdout)
        self.assertEqual(self.mutant_states()[0], (3, "aor", "-", "killed"))

    def mutant_events(self) -> list[dict[str, object]]:
        lines = (self.report_dir / "events.jsonl").read_text().splitlines()
        return [event for event in map(json.loads, lines) if event["event"] == "mutant"]

    def run_per_mutant(self, *argv: str) -> tuple[int, str, str]:
        return run_strain(
            "--build-dir",
            str(self.build_dir),
            "--report-dir",
            str(self.report_dir),
            "--engine",
            "lexer",
            "--strategy",
            "per-mutant",
            *argv,
        )

    def test_cost_order_checks_the_same_mutants_as_source_order_with_the_same_results_cheapest_first(self) -> None:
        configure_project(self.root, self.build_dir)
        runs: dict[str, list[dict[str, object]]] = {}
        for order in ("source", "cost"):
            with self.subTest(order=order):
                exit_code, stdout, _ = self.run_per_mutant("--order", order)
                self.assertEqual(exit_code, 0, stdout)
                self.assertRegex(stdout, r"id [0-9a-f]+  ETA \d+s\n")
                runs[order] = self.mutant_events()
                # The same results; which test kills add varies with the baselines, in either order.
                self.assertEqual(self.mutant_states(), SINGLE_TEST_STATES)
        checked = {order: sorted({str(event["id"]) for event in events}) for order, events in runs.items()}
        self.assertEqual(checked["cost"], checked["source"])
        self.assertEqual([event["line"] for event in runs["source"]][0], 3)
        # Both tests cover add, the other mutants only sample_tests, so the mutant of add costs the most.
        self.assertEqual([event["line"] for event in runs["cost"]][-1], 3)
        # Every result carries the estimated seconds left; none are left after the last mutant.
        self.assertTrue(all(isinstance(event["eta"], float) for event in runs["cost"]), runs["cost"])
        self.assertEqual(runs["cost"][-1]["eta"], 0.0)

    def test_per_mutant_strategy_builds_each_mutant_once_and_stops_at_the_first_kill(self) -> None:
        (self.root / "tests" / "other_tests.cpp").write_text(OTHER_TESTS_CHECKING_ADD)
        configure_project(self.root, self.build_dir)

        exit_code, stdout, _ = self.run_per_mutant()

        self.assertEqual(exit_code, 0, stdout)
        self.assertEqual(self.mutant_states(), SINGLE_TEST_STATES)
        self.assertIn("· src/sample.cpp: 8 mutants on 6 lines", stdout)
        # Both tests cover add, so its mutant costs the most and runs last.
        self.assertIn("[8/8] killed", stdout)
        # Either test may be the one that kills add, so the per-test lines vary; the combined score doesn't.
        self.assertIn("src/sample.cpp   40.0%   8 mutants  2 detected  3 survived", stdout)
        self.assertIn("✓ sources restored and rebuilt", stdout)
        # Both tests kill the mutant of add; whichever runs first is the only one to run.
        add = [event["test"] for event in self.mutant_events() if event["line"] == 3]
        self.assertEqual(len(add), 1, add)
        self.assertEqual((self.root / "src" / "sample.cpp").read_bytes(), self.original_source)

    def test_per_mutant_strategy_runs_the_fastest_covering_test_first(self) -> None:
        (self.root / "tests" / "other_tests.cpp").write_text(OTHER_TESTS_CHECKING_ADD)
        sample_tests = self.root / "tests" / "sample_tests.cpp"
        sample_tests.write_text(
            "#include <chrono>\n#include <thread>\n"
            + sample_tests.read_text().replace(
                "int main()\n{\n", "int main()\n{\n  std::this_thread::sleep_for(std::chrono::seconds(1));\n", 1
            )
        )
        configure_project(self.root, self.build_dir)

        exit_code, stdout, _ = self.run_per_mutant()

        self.assertEqual(exit_code, 0, stdout)
        # sample_tests is selected first, but other_tests is faster and kills the mutant of add on its own.
        add = [event["test"] for event in self.mutant_events() if event["line"] == 3]
        self.assertEqual(add, ["other_tests"])
        self.assertEqual(self.mutant_states(), SINGLE_TEST_STATES)

    def test_per_mutant_strategy_runs_every_covering_test_of_a_survivor(self) -> None:
        (self.root / "tests" / "sample_tests.cpp").write_text(
            '#include "sample.hpp"\n\nint main() { static_cast<void>(add(1, 1)); return 0; }\n'
        )
        configure_project(self.root, self.build_dir)

        exit_code, stdout, _ = self.run_per_mutant()

        self.assertEqual(exit_code, 0, stdout)
        self.assertEqual(self.mutant_states(), [(3, "aor", "-", "survived")])
        self.assertEqual(sorted(event["test"] for event in self.mutant_events()), ["other_tests", "sample_tests"])

    def test_jobs_runs_the_covering_tests_of_a_mutant_together_with_the_same_results(self) -> None:
        (self.root / "tests" / "other_tests.cpp").write_text(OTHER_TESTS_CHECKING_ADD)
        configure_project(self.root, self.build_dir)

        exit_code, stdout, _ = self.run_per_mutant("--jobs", "2")

        self.assertEqual(exit_code, 0, stdout)
        self.assertIn("--jobs 2: up to 2 covering tests of each mutant run at a time", stdout)
        self.assertEqual(self.mutant_states(), SINGLE_TEST_STATES)
        # Both tests covering add ran together, and both kill its mutant.
        add = sorted((event["test"], event["state"]) for event in self.mutant_events() if event["line"] == 3)
        self.assertEqual(add, [("other_tests", "killed"), ("sample_tests", "killed")])
        self.assertEqual((self.root / "src" / "sample.cpp").read_bytes(), self.original_source)

    def test_jobs_needs_the_per_mutant_strategy(self) -> None:
        configure_project(self.root, self.build_dir)

        exit_code, _, stderr = run_strain("--build-dir", str(self.build_dir), "--jobs", "2")

        self.assertEqual(exit_code, 2)
        self.assertIn("it needs --strategy per-mutant", stderr)

    def test_a_second_run_at_the_same_commit_does_not_rebuild_mutants_known_to_break_the_build(self) -> None:
        configure_project(self.root, self.build_dir)
        _, first, _ = self.run_sample_tests("lexer")

        exit_code, second, _ = self.run_sample_tests("lexer")

        self.assertEqual(exit_code, 0, second)
        self.assertIn("✓ build cache: 0 mutants known to break the build at this commit", first)
        self.assertNotIn("known from the cache", first)
        self.assertIn("✓ build cache: 2 mutants known to break the build at this commit", second)
        self.assertEqual(second.count("rejected by -fsyntax-only, known from the cache"), 2)
        self.assertEqual(self.mutant_states(), SINGLE_TEST_STATES)
        self.assertEqual((self.root / "src" / "sample.cpp").read_bytes(), self.original_source)

    def test_the_per_mutant_strategy_uses_the_build_cache(self) -> None:
        configure_project(self.root, self.build_dir)
        self.run_sample_tests("lexer")

        exit_code, stdout, _ = self.run_per_mutant()

        self.assertEqual(exit_code, 0, stdout)
        self.assertEqual(stdout.count("rejected by -fsyntax-only, known from the cache"), 2)

    def test_a_new_commit_starts_an_empty_build_cache(self) -> None:
        configure_project(self.root, self.build_dir)
        self.run_sample_tests("lexer")
        run_checked(
            "git",
            "-c",
            "user.name=strain",
            "-c",
            "user.email=strain@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "next",
            cwd=self.root,
        )

        _, stdout, _ = self.run_sample_tests("lexer")

        self.assertIn("✓ build cache: 0 mutants known to break the build at this commit", stdout)

    def test_no_build_cache_ignores_earlier_runs(self) -> None:
        configure_project(self.root, self.build_dir)
        self.run_sample_tests("lexer")

        _, stdout, _ = run_strain(
            "--build-dir",
            str(self.build_dir),
            "--report-dir",
            str(self.report_dir),
            "-R",
            "^sample_tests$",
            "--engine",
            "lexer",
            "--no-build-cache",
        )

        self.assertIn("--no-build-cache: build failures are remembered for this run only", stdout)
        self.assertNotIn("known from the cache", stdout)
        self.assertEqual(self.mutant_states(), SINGLE_TEST_STATES)

    def test_json_ui_prints_only_events(self) -> None:
        configure_project(self.root, self.build_dir)

        exit_code, stdout, _ = run_strain(
            "--build-dir",
            str(self.build_dir),
            "--report-dir",
            str(self.report_dir),
            "-R",
            "^other_tests$",
            "--ui",
            "json",
        )

        self.assertEqual(exit_code, 0, stdout)
        events = [json.loads(line)["event"] for line in stdout.splitlines()]
        self.assertEqual(events, ["baseline", "mutant", "finished"])

    def test_missing_safety_nets_abort_the_run(self) -> None:
        configure_project(self.root, self.build_dir, cxx_flags="--coverage -fsanitize=address,undefined")

        exit_code, _, stderr = run_strain("--build-dir", str(self.build_dir), "--report-dir", str(self.report_dir))

        self.assertEqual(exit_code, 2)
        self.assertIn("stdlib_assertions", stderr)
        self.assertIn("auto_var_init", stderr)
        self.assertIn("add -D_GLIBCXX_ASSERTIONS", stderr)
        self.assertIn("add -ftrivial-auto-var-init=pattern", stderr)

    def test_failed_checks_stop_the_run_before_the_live_tui_takes_over_the_terminal(self) -> None:
        configure_project(self.root, self.build_dir, cxx_flags="--coverage -fsanitize=address,undefined")
        console = LiveConsole()

        with mock.patch("strain.runner.create_console", return_value=console):
            with self.assertRaisesRegex(StrainError, "stdlib_assertions"):
                run(RunOptions(build_dir=self.build_dir, report_dir=self.report_dir), io.StringIO(), io.StringIO())

        self.assertFalse(console.took_over)
        self.assertIn("working tree is clean", console.transcript.getvalue())

    def test_release_build_is_refused_because_ndebug_removes_assertions(self) -> None:
        configure_project(self.root, self.build_dir, build_type="Release")

        exit_code, _, stderr = run_strain("--build-dir", str(self.build_dir), "--report-dir", str(self.report_dir))

        self.assertEqual(exit_code, 2)
        self.assertIn("error:", stderr)
        self.assertIn("safety net ndebug fails", stderr)
        self.assertIn("compiles out every assert()", stderr)
        self.assertIn("warning: safety net optimization fails", stderr)

    def test_build_without_coverage_fails_even_when_safety_nets_are_ignored(self) -> None:
        configure_project(
            self.root,
            self.build_dir,
            cxx_flags="-fsanitize=address,undefined",
            linker_flags="-fsanitize=address,undefined",
        )

        exit_code, _, stderr = run_strain(
            "--build-dir",
            str(self.build_dir),
            "--report-dir",
            str(self.report_dir),
            "--safety-nets",
            "ignore",
            "--threshold",
            "50",
        )

        self.assertEqual(exit_code, 2)
        self.assertIn("no project translation unit is built with coverage instrumentation", stderr)
        self.assertIn("cannot be ignored", stderr)

    def test_mutants_rebuild_only_the_target_of_their_test(self) -> None:
        configure_project(self.root, self.build_dir)
        runner = Runner(RunOptions(build_dir=self.build_dir), io.StringIO(), io.StringIO())

        runner.prepare()

        self.assertEqual(runner.target_for("sample_tests"), "sample_tests")
        self.assertEqual(runner.target_for("other_tests"), "other_tests")
        self.assertIsNone(runner.target_for("unknown_test"))

    def test_survivors_reach_the_console_with_their_source_and_line_coverage(self) -> None:
        configure_project(self.root, self.build_dir)
        runner = Runner(
            RunOptions(build_dir=self.build_dir, report_dir=self.report_dir, include="^sample_tests$"),
            io.StringIO(),
            io.StringIO(),
        )
        console = RecordingConsole()
        runner.console = console

        runner.run()

        survivors = [(result, line) for result, _, line in console.evaluated if result.state is MutantState.SURVIVED]
        self.assertTrue(survivors)
        self.assertTrue(all(source.startswith('#include "sample.hpp"') for _, source, _ in console.evaluated))
        clamp = next(line for result, line in survivors if result.mutant.line == 7)
        assert clamp is not None
        self.assertEqual(clamp.count, 1)
        self.assertEqual(console.finished_runs, 1)

    def test_interrupted_run_still_writes_the_reports(self) -> None:
        configure_project(self.root, self.build_dir)
        runner = Runner(
            RunOptions(
                build_dir=self.build_dir, report_dir=self.report_dir, include="^sample_tests$", engine=Engine.LEXER
            ),
            io.StringIO(),
            io.StringIO(),
        )
        console = InterruptingConsole(limit=3)
        runner.console = console

        with self.assertRaises(KeyboardInterrupt):
            runner.run()

        self.assertEqual(self.mutant_states(), SINGLE_TEST_STATES[:3])
        events = [json.loads(line) for line in (self.report_dir / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["event"], "interrupted")
        self.assertEqual(events[-1]["score"], 1 / 3)
        transcript = console.transcript.getvalue()
        self.assertIn("▸ Survivors (2)", transcript)
        self.assertIn("mutation score 33.3%", transcript)
        self.assertIn("interrupted after", transcript)
        self.assertEqual(console.finished_runs, 0)
        self.assertEqual((self.root / "src" / "sample.cpp").read_bytes(), self.original_source)

    def run_since(self, ref: str) -> tuple[int, str, str]:
        return run_strain(
            "--build-dir",
            str(self.build_dir),
            "--report-dir",
            str(self.report_dir),
            "--since",
            ref,
            "--threshold",
            "80",
        )

    def test_since_without_changes_says_nothing_was_tested(self) -> None:
        configure_project(self.root, self.build_dir)

        exit_code, stdout, _ = self.run_since("HEAD")

        # A change that touches no source is not a failure, but it must not read like a clean run.
        self.assertEqual(exit_code, 0, stdout)
        reason = "No source line changed since HEAD, so there was nothing to mutate."
        self.assertIn(reason, stdout)
        self.assertEqual(
            (self.report_dir / "summary.md").read_text(),
            f"## mutant_strain mutation testing\n\n**Nothing was mutation tested.** {reason}\n",
        )

    def test_since_with_only_unexecuted_changes_says_nothing_was_tested(self) -> None:
        configure_project(self.root, self.build_dir)
        source = self.root / "src" / "sample.cpp"
        source.write_text("// a comment\n" + source.read_text())
        run_checked(
            "git", "-c", "user.name=s", "-c", "user.email=s@example.com", "commit", "-qam", "comment", cwd=self.root
        )

        exit_code, stdout, _ = self.run_since("HEAD~1")

        self.assertEqual(exit_code, 0, stdout)
        self.assertIn(
            "**Nothing was mutation tested.** No mutant was generated on the 1 line changed since HEAD~1: "
            "the selected tests don't execute them, or they hold nothing to mutate.",
            (self.report_dir / "summary.md").read_text(),
        )

    def test_survivors_of_weak_tests_group_into_the_patterns_that_name_the_missing_tests(self) -> None:
        (self.root / "include" / "sample.hpp").write_text(RECOGNISER_HEADER)
        (self.root / "src" / "sample.cpp").write_text(RECOGNISER_SOURCE)
        (self.root / "tests" / "sample_tests.cpp").write_text(RECOGNISER_TEST)
        (self.root / "tests" / "other_tests.cpp").write_text("int main() { return 0; }\n")
        configure_project(self.root, self.build_dir)

        exit_code, stdout, _ = run_strain(
            *("--build-dir", str(self.build_dir), "--report-dir", str(self.report_dir)),
            *("-R", "^sample_tests$", "--engine", "lexer"),
        )

        self.assertEqual(exit_code, 0, stdout)
        boundary = "4 survivors move a comparison's limit by one in sample.cpp: add a test exactly at the limit"
        and_to_or = (
            "2 survivors turn `&&` into `||` in sample.cpp: add inputs that must be rejected, "
            "since the tests check only what is accepted"
        )
        hotspot = "hotspot: is_identifier_start() in sample.cpp has 6 survivors"
        self.assertIn(f"▸ Patterns\n  · {boundary}\n  · {and_to_or}\n  · {hotspot}", stdout)
        summary = (self.report_dir / "summary.md").read_text()
        self.assertIn(f"### Patterns\n\n- {boundary}\n- {and_to_or}\n- {hotspot}", summary)
        report = json.loads((self.report_dir / "report.json").read_text())
        primary = [pattern for pattern in report["patterns"] if pattern["primary"]]
        self.assertEqual(
            [(pattern["kind"], pattern["survivors"]) for pattern in primary], [("boundary", 4), ("and-to-or", 2)]
        )
        ids = [mutant for pattern in primary for mutant in pattern["mutants"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("<h2>Patterns</h2>", (self.report_dir / "index.html").read_text())

    def use_code_evaluated_only_at_compile_time(self) -> None:
        (self.root / "include" / "sample.hpp").write_text(COMPILE_TIME_HEADER)
        (self.root / "src" / "sample.cpp").write_text(COMPILE_TIME_SOURCE)
        (self.root / "tests" / "sample_tests.cpp").write_text(COMPILE_TIME_TEST)
        (self.root / "tests" / "other_tests.cpp").write_text("int main() { return 0; }\n")
        configure_project(self.root, self.build_dir)

    def test_lines_evaluated_only_at_compile_time_are_reported_as_not_mutated(self) -> None:
        self.use_code_evaluated_only_at_compile_time()

        exit_code, stdout, _ = self.run_sample_tests("lexer")

        self.assertEqual(exit_code, 0, stdout)
        self.assertEqual(self.mutant_states(), [(3, "aor", "-", "killed")])
        self.assertIn("src/sample.cpp:7: not mutated: evaluated at compile time only", stdout)
        self.assertIn("src/sample.cpp:9: not mutated: evaluated at compile time only", stdout)
        self.assertNotIn("src/sample.cpp:13: not mutated", stdout)
        self.assertIn("2 lines not mutated: evaluated at compile time only; mutate them with --compile-time", stdout)
        self.assertIn(
            "2 lines not mutated: evaluated at compile time only", (self.report_dir / "summary.md").read_text()
        )

    def test_compile_time_mutants_failing_a_static_assert_are_killed_at_compile_time(self) -> None:
        self.use_code_evaluated_only_at_compile_time()

        for strategy in ("per-test", "per-mutant"):
            with self.subTest(strategy=strategy):
                exit_code, stdout, _ = run_strain(
                    *("--build-dir", str(self.build_dir), "--report-dir", str(self.report_dir)),
                    *("-R", "^sample_tests$", "--engine", "lexer", "--compile-time", "--strategy", strategy),
                )

                self.assertEqual(exit_code, 0, stdout)
                self.assertEqual(
                    self.mutant_states(),
                    [
                        (3, "aor", "-", "killed"),
                        (7, "aor", "/", "killed_at_compile_time"),
                        (9, "aor", "/", "compile_error"),
                    ],
                )
                self.assertIn("mutation score 100.0%", stdout)
                self.assertNotIn("not mutated: evaluated at compile time only", stdout)
                self.assertEqual((self.root / "src" / "sample.cpp").read_text(), COMPILE_TIME_SOURCE)
        # The second run knows both outcomes from the build cache.
        # Clang adds the failed requirement to the message; GCC does not.
        self.assertRegex(stdout, r"static assertion failed[^\n]*, known from the cache")
        self.assertIn("rejected by -fsyntax-only, known from the cache", stdout)

    def test_dirty_working_tree_is_refused(self) -> None:
        configure_project(self.root, self.build_dir)
        (self.root / "src" / "sample.cpp").write_text("// local edit\n")

        exit_code, _, stderr = run_strain("--build-dir", str(self.build_dir), "--report-dir", str(self.report_dir))

        self.assertEqual(exit_code, 2)
        self.assertIn("clean working tree", stderr)


if __name__ == "__main__":
    unittest.main()
