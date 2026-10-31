import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from strain.cli import main

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
    run_checked("git", "-c", "user.name=strain", "-c", "user.email=strain@example.com", "commit", "-qm", "fixture", cwd=root)
    run_checked(
        "cmake", "-S", root, "-B", build_dir,
        f"-DCMAKE_BUILD_TYPE={build_type}",
        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        f"-DCMAKE_CXX_FLAGS={cxx_flags}",
        f"-DCMAKE_EXE_LINKER_FLAGS={linker_flags}",
        cwd=root,
    )
    run_checked("cmake", "--build", build_dir, cwd=root)


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
            (mutant["location"]["start"]["line"], mutant["mutatorName"], mutant["replacement"],
             mutant.get("statusReason", mutant["status"]))
            for mutant in mutants
        ]

    def test_single_test_run(self) -> None:
        configure_project(self.root, self.build_dir)

        exit_code, stdout, _ = run_strain(
            "--build-dir", str(self.build_dir), "--report-dir", str(self.report_dir), "-R", "^sample_tests$",
            "--threshold", "50",
        )

        self.assertEqual(exit_code, 1, stdout)
        self.assertEqual(
            self.mutant_states(),
            [
                (3, "aor", "-", "killed"),
                (7, "ror", "<=", "survived"),
                (7, "ror", ">=", "survived"),
                (11, "lcr", "||", "survived"),
                (13, "aor", "/", "killed_by_sanitizer"),
                (15, "aor", "-", "ignored"),
                (17, "aor", "/", "compile_error"),
                (17, "aor", "/", "compile_error"),
            ],
        )
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
        self.assertIn(f"{self.root / 'src' / 'sample.cpp'}:7:13: warning: survived [ror]", stdout)
        self.assertEqual((self.root / "src" / "sample.cpp").read_bytes(), self.original_source)
        self.assertTrue((self.report_dir / "events.jsonl").is_file())

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
            "--build-dir", str(self.build_dir), "--report-dir", str(self.report_dir), "-R", "^other_tests$",
            "--ui", "json",
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
            self.root, self.build_dir, cxx_flags="-fsanitize=address,undefined", linker_flags="-fsanitize=address,undefined"
        )

        exit_code, _, stderr = run_strain(
            "--build-dir", str(self.build_dir), "--report-dir", str(self.report_dir), "--safety-nets", "ignore",
            "--threshold", "50",
        )

        self.assertEqual(exit_code, 2)
        self.assertIn("no project translation unit is built with coverage instrumentation", stderr)
        self.assertIn("cannot be ignored", stderr)

    def test_dirty_working_tree_is_refused(self) -> None:
        configure_project(self.root, self.build_dir)
        (self.root / "src" / "sample.cpp").write_text("// local edit\n")

        exit_code, _, stderr = run_strain("--build-dir", str(self.build_dir), "--report-dir", str(self.report_dir))

        self.assertEqual(exit_code, 2)
        self.assertIn("clean working tree", stderr)


if __name__ == "__main__":
    unittest.main()
