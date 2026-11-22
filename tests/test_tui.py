import importlib.util
import io
import threading
import time
import unittest
from unittest import mock

from rich.console import Console as RichConsole

from strain.coverage import LineCoverage
from strain.mutators import Mutant
from strain.results import MutantResult, MutantState, SurvivorKind

HAS_TEXTUAL = importlib.util.find_spec("textual") is not None
if HAS_TEXTUAL:
    from textual.widgets import DataTable, Static, TabbedContent

    from strain.safety_nets import Level, RelaxedSafetyNet, SafetyNet
    from strain.tui import CodeView, MutationMap, RunHeader, StrainApp, TuiConsole, diff_view

SOURCE = "int clamp(int a, int b)\n{\n    if (a < b)\n        return a;\n    return b;\n}\n"
MUTANT = Mutant("src/clamp.cpp", 3, 11, SOURCE.index("<"), "ror", "<", "<=")
SECOND_MUTANT = Mutant("src/clamp.cpp", 3, 11, SOURCE.index("<"), "ror", "<", ">=")
LINE_COVERAGE = LineCoverage(count=12, branches=2, branches_taken=2)
SIZE = (120, 36)


def survived(mutant: Mutant, test: str = "sample_tests") -> MutantResult:
    return MutantResult(mutant, test, MutantState.SURVIVED, SurvivorKind.NOT_DETECTED, "clamp(int, int)")


def killed(mutant: Mutant, test: str = "other_tests") -> MutantResult:
    return MutantResult(mutant, test, MutantState.KILLED, None, "clamp(int, int)")


def text_of(renderable: object, width: int = 120) -> str:
    console = RichConsole(width=width, file=io.StringIO(), color_system=None)
    console.print(renderable)
    return console.file.getvalue()  # type: ignore[attr-defined]


def make_console() -> "TuiConsole":
    return TuiConsole(io.StringIO(), io.StringIO(), headless=True)


def run_two_mutants(console: "TuiConsole") -> None:
    """The console hooks a per-test run makes for one killed and one surviving mutant."""
    console.test_started("sample_tests", 1, 2)
    console.plan([MUTANT, SECOND_MUTANT])
    console.mutant_started(0, SOURCE, MUTANT)
    console.mutant_evaluated(killed(MUTANT, "sample_tests"), SOURCE, LINE_COVERAGE)
    console.mutant_finished(0, MutantState.KILLED)
    console.mutant_started(1, SOURCE, SECOND_MUTANT)
    console.mutant_evaluated(survived(SECOND_MUTANT), SOURCE, LINE_COVERAGE)
    console.mutant_finished(1, MutantState.SURVIVED)


@unittest.skipUnless(HAS_TEXTUAL, "the TUI needs Textual")
class DiffViewTest(unittest.TestCase):
    def test_shows_the_original_and_mutated_line_in_context(self) -> None:
        self.assertEqual(
            text_of(diff_view(SOURCE, MUTANT, context=1)).splitlines(),
            ["  2 │ {", "- 3 │     if (a < b)", "+ 3 │     if (a <= b)", "  4 │         return a;"],
        )

    def test_context_is_clipped_at_the_file_edges(self) -> None:
        first = Mutant("f.cpp", 1, 1, 0, "aor", "+", "-")
        self.assertEqual(
            text_of(diff_view("+x\ny\n", first, context=2)).splitlines(), ["- 1 │ +x", "+ 1 │ -x", "  2 │ y"]
        )


@unittest.skipUnless(HAS_TEXTUAL, "the TUI needs Textual")
class StrainAppTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.console = make_console()
        self.app = StrainApp(self.console)

    async def settle(self, pilot: object) -> None:
        self.app.refresh_from_state()
        await pilot.pause()  # type: ignore[attr-defined]

    async def test_run_view_shows_the_test_the_queue_and_the_running_mutant(self) -> None:
        async with self.app.run_test(size=SIZE) as pilot:
            self.console.test_started("sample_tests", 1, 2)
            self.console.plan([MUTANT, SECOND_MUTANT])
            self.console.mutant_started(0, SOURCE, MUTANT)
            self.console.status("[1/2] src/clamp.cpp:3:11 [ror] `<` → `<=`  building…")
            self.console.build_started("cmake --build build")
            self.console.build_output("[1/2] Building CXX object clamp.cpp.o")
            await self.settle(pilot)
            header = text_of(self.app.query_one(RunHeader).content)
            self.assertIn("Test 1/2: sample_tests", header)
            self.assertIn("mutant 1/2", header)
            self.assertIn("building", header)
            queue = self.app.query_one("#queue", DataTable)
            self.assertEqual(queue.row_count, 2)
            self.assertIn("running", text_of(queue.get_row_at(0)[0]))
            self.assertIn("pending", text_of(queue.get_row_at(1)[0]))
            self.assertIn("+ 3 │     if (a <= b)", text_of(self.app.query_one(CodeView).content))

    async def test_header_totals_count_each_mutant_once(self) -> None:
        async with self.app.run_test(size=SIZE) as pilot:
            run_two_mutants(self.console)
            self.console.mutant_evaluated(survived(MUTANT, "other_tests"), SOURCE, LINE_COVERAGE)
            await self.settle(pilot)
            header = text_of(self.app.query_one(RunHeader).content)
            self.assertIn("score 50.0%", header)
            self.assertIn("killed 1", header)
            self.assertIn("survived 1", header)

    async def test_survivors_view_shows_the_selected_survivor_with_its_context(self) -> None:
        async with self.app.run_test(size=SIZE) as pilot:
            run_two_mutants(self.console)
            await self.settle(pilot)
            await pilot.press("2")
            await pilot.pause()
            self.assertEqual(self.app.query_one(TabbedContent).active, "survivors")
            self.assertEqual(self.app.query_one("#survivor-table", DataTable).row_count, 1)
            detail = text_of(self.app.query_one("#survivor-detail", Static).content)
            self.assertIn(f"id         {SECOND_MUTANT.id}", detail)
            self.assertIn("function   clamp(int, int)", detail)
            self.assertIn("line 3 executed 12 times by sample_tests; 2/2 branches taken", detail)
            self.assertIn("survived   not_detected under sample_tests", detail)
            self.assertIn("add a test where `<` and `>=` give different results", detail)
            self.assertIn("+ 3 │     if (a >= b)", detail)

    async def test_tabs_switch_with_numbers_tab_and_the_mouse(self) -> None:
        async with self.app.run_test(size=SIZE) as pilot:
            tabs = self.app.query_one(TabbedContent)
            await pilot.press("4")
            await pilot.pause()
            self.assertEqual(tabs.active, "stats")
            await pilot.press("tab")
            await pilot.pause()
            self.assertEqual(tabs.active, "log")
            await pilot.press("shift+tab")
            await pilot.pause()
            await pilot.press("shift+tab")
            await pilot.pause()
            self.assertEqual(tabs.active, "mutants")
            await pilot.click("#--content-tab-run")
            await pilot.pause()
            self.assertEqual(tabs.active, "run")

    async def test_switching_views_moves_focus_into_the_new_view(self) -> None:
        """Focus left in a hidden view would move to a neighbour there, which switches back to that view."""
        async with self.app.run_test(size=SIZE) as pilot:
            tabs = self.app.query_one(TabbedContent)
            self.assertIs(self.app.focused, self.app.query_one("#queue"))
            for key, view, widget in (
                ("5", "log", "#transcript"),
                ("4", "stats", "#stats-files"),
                ("tab", "log", "#transcript"),
                ("1", "run", "#queue"),
            ):
                await pilot.press(key)
                await pilot.pause()
                self.assertEqual(tabs.active, view, key)
                self.assertIs(self.app.focused, self.app.query_one(widget), key)

    async def test_a_late_activation_for_a_view_already_left_is_ignored(self) -> None:
        """A view's activation message can arrive after the user has moved on, as when tab follows 4 quickly.

        Focusing into that view would switch back to it.
        """
        async with self.app.run_test(size=SIZE) as pilot:
            tabs = self.app.query_one(TabbedContent)
            await pilot.press("5")
            await pilot.pause()
            tabs.post_message(TabbedContent.TabActivated(tabs, tabs.get_tab("stats")))
            await pilot.pause()
            self.assertEqual(tabs.active, "log")
            self.assertIs(self.app.focused, self.app.query_one("#transcript"))

    async def test_mutants_view_filters_by_state(self) -> None:
        async with self.app.run_test(size=SIZE) as pilot:
            run_two_mutants(self.console)
            await self.settle(pilot)
            await pilot.press("3")
            await pilot.pause()
            table = self.app.query_one("#mutant-table", DataTable)
            self.assertEqual(table.row_count, 2)
            await pilot.click("#filter-survived")
            await pilot.pause()
            self.assertEqual(table.row_count, 1)
            self.assertIn(SECOND_MUTANT.id, text_of(self.app.query_one("#mutant-detail", Static).content))
            await pilot.press("f")
            await pilot.pause()
            self.assertEqual(self.app.state_filter, None)

    async def test_selecting_a_queue_entry_opens_it_in_the_mutant_browser(self) -> None:
        async with self.app.run_test(size=SIZE) as pilot:
            run_two_mutants(self.console)
            await self.settle(pilot)
            await pilot.press("up")
            await pilot.pause()
            self.assertFalse(self.app.following)
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(self.app.query_one(TabbedContent).active, "mutants")
            self.assertIn(f"id         {MUTANT.id}", text_of(self.app.query_one("#mutant-detail", Static).content))

    async def test_clicking_the_mutation_map_shows_that_mutant_in_the_queue(self) -> None:
        async with self.app.run_test(size=SIZE) as pilot:
            run_two_mutants(self.console)
            await self.settle(pilot)
            await pilot.press("4")
            await pilot.click(MutationMap, offset=(0, 0))
            await pilot.pause()
            self.assertEqual(self.app.query_one(TabbedContent).active, "run")
            self.assertEqual(self.app.query_one("#queue", DataTable).cursor_row, 0)
            self.assertFalse(self.app.following)
            await pilot.press("f")
            self.assertTrue(self.app.following)

    async def test_p_pauses_the_run_before_the_next_mutant(self) -> None:
        async with self.app.run_test(size=SIZE) as pilot:
            await pilot.press("p")
            self.assertTrue(self.console.paused)
            self.assertFalse(self.console.resume.is_set())
            await pilot.press("p")
            self.assertTrue(self.console.resume.is_set())

    async def test_q_quits_only_once_the_run_has_finished(self) -> None:
        async with self.app.run_test(size=SIZE) as pilot:
            with mock.patch.object(self.app, "exit") as exit_app:
                await pilot.press("q")
                exit_app.assert_not_called()
                self.console.run_finished()
                await pilot.press("q")
                exit_app.assert_called_once()

    async def test_ctrl_c_asks_the_runner_to_stop(self) -> None:
        async with self.app.run_test(size=SIZE) as pilot:
            self.console.worker = threading.Thread(target=time.sleep, args=(5,), daemon=True)
            self.console.start_worker()
            await pilot.press("ctrl+c")
            self.assertTrue(self.console.stop_requested.is_set())
            self.assertTrue(self.console.live.stopping)

    async def test_finishing_the_run_opens_with_every_mutant_once(self) -> None:
        async with self.app.run_test(size=SIZE) as pilot:
            run_two_mutants(self.console)
            self.console.test_started("other_tests", 2, 2)
            self.console.plan([MUTANT])
            self.console.run_finished()
            await self.settle(pilot)
            self.assertEqual(self.app.query_one("#queue", DataTable).row_count, 2)
            header = text_of(self.app.query_one(RunHeader).content)
            self.assertIn("all tests", header)
            self.assertIn("finished", header)

    async def test_every_table_fits_its_panel_at_120_columns(self) -> None:
        async with self.app.run_test(size=SIZE) as pilot:
            run_two_mutants(self.console)
            self.console.run_finished()
            await self.settle(pilot)
            for key, tables in (
                ("2", ("#survivor-table", "#survivor-files")),
                ("3", ("#mutant-table",)),
                ("4", ("#stats-files", "#stats-operators")),
            ):
                await pilot.press(key)
                await self.settle(pilot)
                for table_id in tables:
                    table = self.app.query_one(table_id, DataTable)
                    self.assertEqual(table.max_scroll_x, 0, f"{table_id} scrolls sideways")

    async def test_small_terminals_still_render(self) -> None:
        async with self.app.run_test(size=(40, 12)) as pilot:
            run_two_mutants(self.console)
            await self.settle(pilot)
            tabs = self.app.query_one(TabbedContent)
            for key, view in zip("12345", ("run", "survivors", "mutants", "stats", "log"), strict=True):
                await pilot.press(key)
                await pilot.pause()
                self.assertEqual(tabs.active, view)


@unittest.skipUnless(HAS_TEXTUAL, "the TUI needs Textual")
class TuiConsoleTest(unittest.TestCase):
    def test_stopping_raises_keyboard_interrupt_in_the_runner_once(self) -> None:
        console = make_console()
        console.status("working")
        console.request_stop()
        with self.assertRaises(KeyboardInterrupt):
            console.build_output("[1/2] Building")
        console.line("the report after the interruption")
        self.assertIn("the report after the interruption", console.transcript)

    def test_a_paused_run_waits_before_the_next_mutant(self) -> None:
        console = make_console()
        console.plan([MUTANT])
        console.set_paused(True)
        runner = threading.Thread(target=console.mutant_started, args=(0, SOURCE, MUTANT))
        runner.start()
        runner.join(0.3)
        self.assertTrue(runner.is_alive())
        self.assertTrue(console.live.waiting)
        console.set_paused(False)
        runner.join(2)
        self.assertFalse(runner.is_alive())
        self.assertEqual(console.live.current, 0)

    def test_run_returns_what_the_work_returns_and_replays_the_transcript(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        console = TuiConsole(out, err, headless=True)

        def work() -> int:
            console.heading("Preparing")
            console.warn("test t fails without mutation")
            console.run_finished()
            console.app.call_from_thread(console.app.exit)  # type: ignore[union-attr]
            return 3

        self.assertEqual(console.run(work), 3)
        console.finish()
        console.finish()
        self.assertEqual(out.getvalue(), "\n▸ Preparing\n")
        self.assertEqual(err.getvalue(), "mutant_strain: warning: test t fails without mutation\n")

    def test_an_error_in_the_run_closes_the_tui_and_is_raised(self) -> None:
        console = make_console()

        def work() -> int:
            raise ValueError("no tests selected")

        with self.assertRaisesRegex(ValueError, "no tests selected"):
            console.run(work)

    def test_stopping_the_run_ends_it_with_keyboard_interrupt(self) -> None:
        console = make_console()

        def work() -> int:
            while True:
                console.status("running…")
                time.sleep(0.01)

        threading.Timer(0.5, console.request_stop).start()
        with self.assertRaises(KeyboardInterrupt):
            console.run(work)


ASAN_OFF = "fails in 2 translation unit(s), e.g. src/clamp.cpp"
LTO_ON = "fails for the whole build (CMAKE_INTERPROCEDURAL_OPTIMIZATION=ON)"


@unittest.skipUnless(HAS_TEXTUAL, "the TUI needs Textual")
class SafetyNetViewTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.console = make_console()
        self.app = StrainApp(self.console)

    def relax(self, *failures: "RelaxedSafetyNet") -> None:
        relaxed = {failure.net for failure in failures}
        self.console.safety_nets_checked(list(failures), [net for net in SafetyNet if net not in relaxed])

    async def settle(self, pilot: object) -> None:
        self.app.refresh_from_state()
        await pilot.pause()  # type: ignore[attr-defined]

    async def test_stats_explain_each_relaxed_net_and_why_it_mattered(self) -> None:
        async with self.app.run_test(size=SIZE) as pilot:
            self.relax(
                RelaxedSafetyNet(SafetyNet.ASAN, Level.WARN, ASAN_OFF),
                RelaxedSafetyNet(SafetyNet.LTO, Level.IGNORE, LTO_ON),
            )
            await self.settle(pilot)
            report = text_of(self.app.query_one("#safety", Static).content)
            self.assertIn("asan is off, so a mutant that breaks the code can still pass the tests", report)
            self.assertIn("asan  set to warn, so the run went on with a warning; by default it stops the run", report)
            self.assertIn("lto  set to ignore, so the run went on without a warning", report)
            self.assertIn("fails      in 2 translation unit(s), e.g. src/clamp.cpp", report)
            self.assertIn("why        without AddressSanitizer an out-of-bounds", report)
            self.assertIn("fix        add -fsanitize=address", report)
            self.assertIn("✔ hold: ubsan", report)
            self.assertEqual(self.app.query_one("#safety-box").border_title, "Safety nets: 2 off")

    async def test_nets_that_only_cost_speed_say_survivors_still_count(self) -> None:
        async with self.app.run_test(size=SIZE) as pilot:
            self.relax(RelaxedSafetyNet(SafetyNet.LTO, Level.WARN, LTO_ON))
            await self.settle(pilot)
            report = text_of(self.app.query_one("#safety", Static).content)
            self.assertIn("survivors still point at weak tests", report)

    async def test_every_net_holding_is_one_line(self) -> None:
        async with self.app.run_test(size=SIZE) as pilot:
            self.relax()
            await self.settle(pilot)
            self.assertIn(
                "✔ every safety net holds: asan, ubsan", text_of(self.app.query_one("#safety", Static).content)
            )
            self.assertNotIn("safety net", text_of(self.app.query_one(RunHeader).content))

    async def test_the_header_badge_counts_relaxed_nets_and_opens_their_report(self) -> None:
        async with self.app.run_test(size=SIZE) as pilot:
            self.relax(RelaxedSafetyNet(SafetyNet.ASAN, Level.IGNORE, ASAN_OFF))
            await self.settle(pilot)
            header = self.app.query_one(RunHeader)
            self.assertIn("! 1 safety net off", text_of(header.content))
            await pilot.click(RunHeader, offset=(header.safety_columns.start, 0))
            await pilot.pause()
            self.assertEqual(self.app.query_one(TabbedContent).active, "stats")

    async def test_survivors_carry_a_caution_while_a_bug_hiding_net_is_off(self) -> None:
        async with self.app.run_test(size=SIZE) as pilot:
            self.relax(RelaxedSafetyNet(SafetyNet.ASAN, Level.WARN, ASAN_OFF))
            run_two_mutants(self.console)
            await self.settle(pilot)
            await pilot.press("2")
            await pilot.pause()
            detail = text_of(self.app.query_one("#survivor-detail", Static).content)
            self.assertIn("caution    asan off: this mutant may survive only because of that", detail)


if __name__ == "__main__":
    unittest.main()
