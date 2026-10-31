import io
import os
import unittest
from unittest import mock

from strain.console import CLEAR_LINE, RESET, STATE_WIDTH, Console, UiMode, paint, plural, score_style, wants_color
from strain.results import MutantState


class TerminalBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class WantsColorTest(unittest.TestCase):
    def test_terminals_get_color(self) -> None:
        self.assertTrue(wants_color(TerminalBuffer(), {}))
        self.assertFalse(wants_color(io.StringIO(), {}))

    def test_no_color_and_force_color(self) -> None:
        self.assertFalse(wants_color(TerminalBuffer(), {"NO_COLOR": ""}))
        self.assertTrue(wants_color(io.StringIO(), {"FORCE_COLOR": "1"}))


class HelpersTest(unittest.TestCase):
    def test_paint(self) -> None:
        self.assertEqual(paint("x", "\033[1m", color=False), "x")
        self.assertEqual(paint("x", "\033[1m", color=True), f"\033[1mx{RESET}")

    def test_plural(self) -> None:
        self.assertEqual(plural(1, "test"), "1 test")
        self.assertEqual(plural(0, "test"), "0 tests")

    def test_score_style_follows_report_thresholds(self) -> None:
        self.assertIn("32m", score_style(80.0))
        self.assertIn("33m", score_style(60.0))
        self.assertIn("31m", score_style(59.9))


class ConsoleTest(unittest.TestCase):
    def setUp(self) -> None:
        environment = mock.patch.dict(os.environ, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_plain_output_off_a_terminal(self) -> None:
        out = io.StringIO()
        console = Console(out, io.StringIO())
        console.heading("Preparing")
        console.step("working tree is clean")
        console.status("building…")
        console.line(f"{console.state(MutantState.SURVIVED)}|")
        self.assertEqual(out.getvalue(), "\n▸ Preparing\n  ✓ working tree is clean\nsurvived           |\n")

    def test_live_status_is_cleared_before_the_next_line(self) -> None:
        out = TerminalBuffer()
        console = Console(out, io.StringIO())
        console.status("building…")
        console.line("done")
        console.status("running…")
        console.finish()
        self.assertEqual(
            out.getvalue().replace(console.paint("building…", "\033[2m"), "<building>")
                          .replace(console.paint("running…", "\033[2m"), "<running>"),
            f"{CLEAR_LINE}<building>{CLEAR_LINE}done\n{CLEAR_LINE}<running>{CLEAR_LINE}",
        )

    def test_log_mode_has_no_color_or_status_on_a_terminal(self) -> None:
        out = TerminalBuffer()
        console = Console(out, io.StringIO(), UiMode.LOG)
        console.status("building…")
        console.line(console.state(MutantState.KILLED))
        self.assertEqual(out.getvalue(), f"{'killed':<{STATE_WIDTH}}\n")

    def test_json_mode_prints_only_warnings(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        console = Console(out, err, UiMode.JSON)
        console.heading("Preparing")
        console.status("building…")
        console.warn("test t fails without mutation")
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "strain: warning: test t fails without mutation\n")


if __name__ == "__main__":
    unittest.main()
