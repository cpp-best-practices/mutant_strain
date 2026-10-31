import contextlib
import io
import unittest
from pathlib import Path

from strain.cli import build_parser, options_from_arguments
from strain.console import UiMode
from strain.safety_nets import Level


def parse(*argv: str):
    return options_from_arguments(build_parser().parse_args(["run", *argv]))


class RunOptionsTest(unittest.TestCase):
    def test_defaults_match_the_readme(self) -> None:
        options = parse()
        self.assertEqual(options.build_dir, Path("."))
        self.assertEqual(options.include, None)
        self.assertEqual(options.exclude, None)
        self.assertEqual(options.since, None)
        self.assertFalse(options.skip_killed)
        self.assertIsNone(options.safety_nets)
        self.assertIsNone(options.threshold)
        self.assertFalse(options.strict)
        self.assertIsNone(options.max_per_line)
        self.assertFalse(options.resume)
        self.assertFalse(options.show_all)
        self.assertIs(options.ui, UiMode.AUTO)
        self.assertEqual(options.report_dir, Path("strain-report"))

    def test_every_option(self) -> None:
        options = parse(
            "--build-dir", "build-strain", "-R", "parser", "-E", "slow", "--since", "main", "--strategy", "per-test",
            "--skip-killed", "--engine", "lexer", "--coverage", "gcov", "--ui", "json", "--safety-nets", "warn",
            "--threshold", "75.5", "--strict", "--max-per-line", "3", "--resume", "--all", "--report-dir", "out",
        )
        self.assertEqual(options.build_dir, Path("build-strain"))
        self.assertEqual((options.include, options.exclude, options.since), ("parser", "slow", "main"))
        self.assertTrue(options.skip_killed)
        self.assertIs(options.ui, UiMode.JSON)
        self.assertIs(options.safety_nets, Level.WARN)
        self.assertEqual(options.threshold, 75.5)
        self.assertTrue(options.strict)
        self.assertEqual(options.max_per_line, 3)
        self.assertTrue(options.resume)
        self.assertTrue(options.show_all)
        self.assertEqual(options.report_dir, Path("out"))

    def test_live_dashboard(self) -> None:
        self.assertIs(parse("--ui", "live").ui, UiMode.LIVE)

    def test_backends_that_do_not_exist_yet_are_rejected(self) -> None:
        for argv in (["--engine", "cppcheck"], ["--coverage", "llvm-cov"], ["--strategy", "per-mutant"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parse(*argv)


if __name__ == "__main__":
    unittest.main()
