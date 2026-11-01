import io
import os
import re
import threading
import unittest
from unittest import mock

from strain.console import UiMode
from strain.dashboard import (
    ALTERNATE_SCREEN,
    MAIN_SCREEN,
    Dashboard,
    LiveConsole,
    RUN_VIEW,
    SURVIVORS_VIEW,
    code_snippet,
    create_console,
    fit,
)
from strain.coverage import LineCoverage
from strain.mutators import Mutant
from strain.results import MutantResult, MutantState, SurvivorKind

SOURCE = "int clamp(int a, int b)\n{\n    if (a < b)\n        return a;\n    return b;\n}\n"
MUTANT = Mutant("src/clamp.cpp", 3, 11, SOURCE.index("<"), "ror", "<", "<=")
SECOND_MUTANT = Mutant("src/clamp.cpp", 3, 11, SOURCE.index("<"), "ror", "<", ">=")
LINE_COVERAGE = LineCoverage(count=12, branches=2, branches_taken=2)
ESCAPE = re.compile(r"\033\[[0-9;?]*[A-Za-z]")


class TerminalBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


def terminal(columns: int, lines: int):
    return lambda: os.terminal_size((columns, lines))


def no_keys(on_key):
    return None


class FakeKeys:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def plain(text: str) -> str:
    return ESCAPE.sub("", text)


class FitTest(unittest.TestCase):
    def test_pads_and_truncates_by_visible_width(self) -> None:
        self.assertEqual(fit("abc", 5), "abc  ")
        self.assertEqual(fit("abcdef", 4), "abc…")
        self.assertEqual(plain(fit("\033[1mabcdef\033[0m", 4)), "abc…")
        self.assertEqual(plain(fit("\033[1mab\033[0m", 4)), "ab  ")


class CodeSnippetTest(unittest.TestCase):
    def test_shows_the_original_and_mutated_line_in_context(self) -> None:
        self.assertEqual(
            code_snippet(SOURCE, MUTANT, context=1),
            [
                "  2 │ {",
                "- 3 │     if (a < b)",
                "+ 3 │     if (a <= b)",
                "  4 │         return a;",
            ],
        )

    def test_context_is_clipped_at_the_file_edges(self) -> None:
        first = Mutant("f.cpp", 1, 1, 0, "aor", "+", "-")
        self.assertEqual(code_snippet("+x\ny\n", first, context=2), ["- 1 │ +x", "+ 1 │ -x", "  2 │ y"])


class DashboardTest(unittest.TestCase):
    def make_dashboard(self, columns: int = 60, lines: int = 30) -> Dashboard:
        dashboard = Dashboard(color=False)
        dashboard.set_test("sample_tests", 1, 2)
        dashboard.set_plan([f"src/clamp.cpp:{line}:1 [ror] `<` → `<=`" for line in range(1, 21)])
        return dashboard

    def test_frame_has_every_panel_and_fits_the_terminal(self) -> None:
        dashboard = self.make_dashboard()
        dashboard.start_mutant(0, SOURCE, MUTANT)
        dashboard.build_output("[1/2] Building CXX object clamp.cpp.o")
        dashboard.set_status("building…")
        frame = dashboard.render(60, 30)
        self.assertEqual(len(frame), 30)
        self.assertTrue(all(len(line) == 60 for line in frame))
        text = "\n".join(frame)
        self.assertIn("Test 1/2: sample_tests", text)
        self.assertIn("mutant 1/20", text)
        self.assertIn("- 3 │     if (a < b)", text)
        self.assertIn("+ 3 │     if (a <= b)", text)
        self.assertIn("[1/2] Building CXX object clamp.cpp.o", text)
        self.assertIn("building…", frame[-1])

    def test_queue_shows_previous_results_and_upcoming_mutants(self) -> None:
        dashboard = self.make_dashboard()
        dashboard.start_mutant(0, SOURCE, MUTANT)
        dashboard.finish_mutant(0, MutantState.KILLED)
        dashboard.start_mutant(1, SOURCE, MUTANT)
        dashboard.finish_mutant(1, MutantState.SURVIVED)
        dashboard.start_mutant(2, SOURCE, MUTANT)
        text = "\n".join(dashboard.render(60, 30))
        self.assertRegex(text, r"killed +src/clamp.cpp:1:1")
        self.assertRegex(text, r"survived +src/clamp.cpp:2:1")
        self.assertRegex(text, r"▶ running +src/clamp.cpp:3:1")
        self.assertRegex(text, r"pending +src/clamp.cpp:4:1")
        self.assertIn("killed 1  survived 1  score 50.0%", text)

    def test_queue_scrolls_to_keep_the_current_mutant_visible(self) -> None:
        dashboard = self.make_dashboard()
        for index in range(15):
            dashboard.start_mutant(index, SOURCE, MUTANT)
            dashboard.finish_mutant(index, MutantState.KILLED)
        dashboard.start_mutant(15, SOURCE, MUTANT)
        text = "\n".join(dashboard.render(60, 30))
        self.assertIn("src/clamp.cpp:16:1", text)
        self.assertNotIn("src/clamp.cpp:1:1 ", text)
        self.assertRegex(text, r"↑ \d+ earlier")

    def test_build_log_keeps_the_latest_lines_of_the_current_build(self) -> None:
        dashboard = self.make_dashboard()
        dashboard.start_build("cmake --build build")
        for number in range(100):
            dashboard.build_output(f"line {number}")
        text = "\n".join(dashboard.render(60, 30))
        self.assertIn("line 99", text)
        self.assertNotRegex(text, r"\bline 0\b")
        dashboard.start_build("cmake --build build")
        self.assertNotIn("line 99", "\n".join(dashboard.render(60, 30)))

    def test_tiny_terminals_still_render(self) -> None:
        dashboard = self.make_dashboard()
        dashboard.start_mutant(0, SOURCE, MUTANT)
        for columns, lines in ((10, 3), (1, 1), (80, 8)):
            with self.subTest(size=(columns, lines)):
                frame = dashboard.render(columns, lines)
                self.assertEqual(len(frame), lines)
                self.assertTrue(all(len(line) == columns for line in frame))


def survived(mutant: Mutant, test: str = "sample_tests") -> MutantResult:
    return MutantResult(mutant, test, MutantState.SURVIVED, SurvivorKind.NOT_DETECTED, "clamp(int, int)")


def killed(mutant: Mutant, test: str = "other_tests") -> MutantResult:
    return MutantResult(mutant, test, MutantState.KILLED, None, "clamp(int, int)")


class SurvivorBrowserTest(unittest.TestCase):
    def make_dashboard(self) -> Dashboard:
        dashboard = Dashboard(color=False, interactive=True)
        dashboard.set_test("sample_tests", 1, 2)
        dashboard.set_plan(["first", "second"])
        return dashboard

    def test_findings_panel_reports_survivors_while_the_run_goes_on(self) -> None:
        dashboard = self.make_dashboard()
        dashboard.record_result(survived(MUTANT), SOURCE, LINE_COVERAGE)
        text = "\n".join(dashboard.render(120, 40))
        self.assertIn("Findings", text)
        self.assertIn("clamp() is pseudo-tested", text)
        self.assertIn("src/clamp.cpp:3 `<` → `<=` survived (not_detected)", text)

    def test_survivors_view_shows_the_selected_survivor_with_its_context(self) -> None:
        dashboard = self.make_dashboard()
        dashboard.record_result(survived(MUTANT), SOURCE, LINE_COVERAGE)
        dashboard.press("tab")
        self.assertEqual(dashboard.view, SURVIVORS_VIEW)
        text = "\n".join(dashboard.render(120, 40))
        self.assertIn("Survivors (1)", text)
        self.assertRegex(text, r"▶ src/clamp.cpp:3:11 \[ror\] `<` → `<=`")
        self.assertIn(f"id {MUTANT.id}", text)
        self.assertIn("function   clamp(int, int)", text)
        self.assertIn("line 3 executed 12 times by sample_tests; 2/2 branches taken", text)
        self.assertIn("survived   not_detected under sample_tests", text)
        self.assertIn("add a test where `<` and `<=` give different results", text)
        self.assertIn("+ 3 │     if (a <= b)", text)

    def test_arrow_keys_move_the_selection_and_stay_in_range(self) -> None:
        dashboard = self.make_dashboard()
        dashboard.record_result(survived(MUTANT), SOURCE, LINE_COVERAGE)
        dashboard.record_result(survived(SECOND_MUTANT), SOURCE, LINE_COVERAGE)
        dashboard.press("tab")
        for key in ("down", "down", "down"):
            dashboard.press(key)
        text = "\n".join(dashboard.render(120, 40))
        self.assertRegex(text, r"▶ src/clamp.cpp:3:11 \[ror\] `<` → `>=`")
        self.assertIn(f"id {SECOND_MUTANT.id}", text)
        for key in ("up", "up", "up"):
            dashboard.press(key)
        self.assertIn(f"id {MUTANT.id}", "\n".join(dashboard.render(120, 40)))

    def test_execution_counts_add_up_over_every_test_that_ran_the_line(self) -> None:
        dashboard = self.make_dashboard()
        dashboard.record_result(survived(MUTANT), SOURCE, LINE_COVERAGE)
        dashboard.record_result(survived(MUTANT, "other_tests"), SOURCE, LineCoverage(3, 2, 1))
        dashboard.press("tab")
        text = "\n".join(dashboard.render(120, 40))
        self.assertIn("line 3 executed 15 times: sample_tests 12, other_tests 3; 2/2 branches taken", text)

    def test_a_mutant_killed_by_a_later_test_leaves_the_survivors(self) -> None:
        dashboard = self.make_dashboard()
        dashboard.record_result(survived(MUTANT), SOURCE, LINE_COVERAGE)
        dashboard.record_result(killed(MUTANT), SOURCE, LINE_COVERAGE)
        dashboard.press("tab")
        text = "\n".join(dashboard.render(120, 40))
        self.assertIn("Survivors (0)", text)
        self.assertIn("no surviving mutants", text)

    def test_tab_returns_to_the_run_view(self) -> None:
        dashboard = self.make_dashboard()
        dashboard.press("tab")
        dashboard.press("tab")
        self.assertEqual(dashboard.view, RUN_VIEW)

    def test_finishing_the_run_opens_the_survivors_and_tells_how_to_quit(self) -> None:
        dashboard = self.make_dashboard()
        dashboard.record_result(survived(MUTANT), SOURCE, LINE_COVERAGE)
        dashboard.finish_run()
        frame = dashboard.render(120, 40)
        self.assertEqual(dashboard.view, SURVIVORS_VIEW)
        self.assertIn("q quit", frame[0])

    def test_key_hints_are_only_shown_when_keys_are_read(self) -> None:
        self.assertNotIn("tab", Dashboard(color=False).render(120, 40)[0])
        self.assertIn("tab survivors", self.make_dashboard().render(120, 40)[0])

    def test_survivors_view_fits_tiny_terminals(self) -> None:
        dashboard = self.make_dashboard()
        dashboard.record_result(survived(MUTANT), SOURCE, LINE_COVERAGE)
        dashboard.press("tab")
        for columns, lines in ((10, 3), (1, 1), (80, 8)):
            with self.subTest(size=(columns, lines)):
                frame = dashboard.render(columns, lines)
                self.assertEqual(len(frame), lines)
                self.assertTrue(all(len(line) == columns for line in frame))


class LiveConsoleTest(unittest.TestCase):
    def setUp(self) -> None:
        environment = mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_live_mode_on_a_terminal_uses_the_dashboard(self) -> None:
        self.assertIsInstance(create_console(TerminalBuffer(), io.StringIO(), UiMode.LIVE), LiveConsole)

    def test_live_mode_off_a_terminal_falls_back_to_plain_lines(self) -> None:
        out = io.StringIO()
        console = create_console(out, io.StringIO(), UiMode.LIVE)
        self.assertNotIsInstance(console, LiveConsole)
        console.step("working tree is clean")
        self.assertEqual(out.getvalue(), "  ✓ working tree is clean\n")

    def test_draws_on_the_alternate_screen_and_replays_the_transcript_when_finished(self) -> None:
        out = TerminalBuffer()
        err = io.StringIO()
        console = LiveConsole(out, err, terminal(60, 20), no_keys)
        console.heading("Preparing")
        console.plan([MUTANT])
        console.mutant_started(0, SOURCE, MUTANT)
        console.build_started("cmake --build build")
        console.build_output("[1/1] Linking CXX executable sample_tests")
        console.warn("test t fails without mutation")
        self.assertTrue(out.getvalue().startswith(ALTERNATE_SCREEN))
        self.assertIn("Linking CXX executable sample_tests", out.getvalue())
        self.assertEqual(err.getvalue(), "")
        console.finish()
        console.finish()
        screen, _, transcript = out.getvalue().rpartition(MAIN_SCREEN)
        self.assertEqual(out.getvalue().count(MAIN_SCREEN), 1)
        self.assertEqual(transcript, "\n▸ Preparing\n")
        self.assertEqual(err.getvalue(), "strain: warning: test t fails without mutation\n")

    def test_run_finished_keeps_the_browser_open_until_q_is_pressed(self) -> None:
        keys = FakeKeys()
        console = LiveConsole(TerminalBuffer(), io.StringIO(), terminal(60, 20), lambda on_key: keys)
        console.mutant_evaluated(survived(MUTANT), SOURCE, LINE_COVERAGE)
        waiter = threading.Thread(target=console.run_finished)
        waiter.start()
        console.key_pressed("tab")
        waiter.join(0.2)
        self.assertTrue(waiter.is_alive())
        self.assertIn("Survivors (1)", console.out.getvalue())
        console.key_pressed("q")
        waiter.join(2)
        self.assertFalse(waiter.is_alive())
        console.finish()
        self.assertTrue(keys.closed)

    def test_run_finished_returns_at_once_without_a_keyboard(self) -> None:
        console = LiveConsole(TerminalBuffer(), io.StringIO(), terminal(60, 20), no_keys)
        console.run_finished()
        console.finish()


if __name__ == "__main__":
    unittest.main()
