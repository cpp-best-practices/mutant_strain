import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from strain import cppcheck_engine, tree_sitter_engine
from strain.cli import main
from strain.console import Console
from strain.mutators import Engine, fallback_engine
from strain.coverage import LineCoverage
from strain.results import MutantResult, MutantState
from strain.runner import Runner, RunOptions

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
        super().__init__(io.StringIO(), io.StringIO())
        self.evaluated: list[tuple[MutantResult, str, LineCoverage | None]] = []
        self.finished_runs = 0

    def mutant_evaluated(self, result: MutantResult, source: str, line: LineCoverage | None) -> None:
        self.evaluated.append((result, source, line))

    def run_finished(self) -> None:
        self.finished_runs += 1


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
        self.assertEqual(clamp.count, 1)
        self.assertEqual(console.finished_runs, 1)

    def test_dirty_working_tree_is_refused(self) -> None:
        configure_project(self.root, self.build_dir)
        (self.root / "src" / "sample.cpp").write_text("// local edit\n")

        exit_code, _, stderr = run_strain("--build-dir", str(self.build_dir), "--report-dir", str(self.report_dir))

        self.assertEqual(exit_code, 2)
        self.assertIn("clean working tree", stderr)


if __name__ == "__main__":
    unittest.main()
