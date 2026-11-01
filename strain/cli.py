"""The `strain` command line."""

import argparse
import signal
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import FrameType

from .console import BOLD, RED, YELLOW, UiMode, paint, wants_color
from .mutators import Engine
from .runner import RunOptions, StrainError, run
from .safety_nets import Level


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--build-dir", type=Path, default=Path("."), help="CMake build folder")
    parser.add_argument("-R", dest="include", metavar="REGEX", help="include tests by regex, as in ctest")
    parser.add_argument("-E", dest="exclude", metavar="REGEX", help="exclude tests by regex, as in ctest")
    parser.add_argument("--since", metavar="REF", help="mutate only lines changed since a git ref")
    parser.add_argument("--strategy", choices=["per-test"], default="per-test", help="mutation strategy")
    parser.add_argument("--skip-killed", action="store_true", help="skip mutants an earlier test already killed")
    parser.add_argument(
        "--engine", type=Engine, choices=list(Engine), default=Engine.AUTO,
        help="mutation engine: auto (the best installed), cppcheck, tree-sitter or lexer",
    )
    parser.add_argument("--coverage", choices=["auto", "gcov"], default="auto", help="coverage backend")
    parser.add_argument(
        "--ui", type=UiMode, choices=list(UiMode), default=UiMode.AUTO,
        help=(
            "console output: auto (live progress and color on a terminal), live (full-screen dashboard), "
            "log (plain lines) or json (events)"
        ),
    )
    parser.add_argument("--safety-nets", type=Level, choices=list(Level), help="level for every safety net check")
    parser.add_argument("--threshold", type=float, metavar="PERCENT", help="exit non-zero below this score")
    parser.add_argument("--strict", action="store_true", help="abort if any test is excluded")
    parser.add_argument("--max-per-line", type=_positive_int, metavar="N", help="cap mutants per source line")
    parser.add_argument("--resume", action="store_true", help="continue an interrupted run")
    parser.add_argument("--all", dest="show_all", action="store_true", help="show every survivor, not just the top actions")
    parser.add_argument("--report-dir", type=Path, default=Path("strain-report"), help="where reports are written")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="strain", description="Mutation testing for CMake/CTest C++ projects.")
    commands = parser.add_subparsers(dest="command", required=True)
    _add_run_arguments(commands.add_parser("run", help="run mutation testing"))
    return parser


def options_from_arguments(arguments: argparse.Namespace) -> RunOptions:
    return RunOptions(
        build_dir=arguments.build_dir,
        include=arguments.include,
        exclude=arguments.exclude,
        since=arguments.since,
        skip_killed=arguments.skip_killed,
        engine=arguments.engine,
        safety_nets=arguments.safety_nets,
        threshold=arguments.threshold,
        strict=arguments.strict,
        max_per_line=arguments.max_per_line,
        resume=arguments.resume,
        show_all=arguments.show_all,
        ui=arguments.ui,
        report_dir=arguments.report_dir,
    )


def _interrupt_on_terminate(signal_number: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def main(argv: Sequence[str] | None = None) -> int:
    options = options_from_arguments(build_parser().parse_args(argv))
    previous_handler = signal.signal(signal.SIGTERM, _interrupt_on_terminate)
    try:
        return run(options, sys.stdout, sys.stderr)
    except (StrainError, FileNotFoundError, ValueError, subprocess.CalledProcessError) as error:
        print(f"strain: {paint('error:', BOLD + RED, wants_color(sys.stderr))} {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        interrupted = paint("interrupted;", BOLD + YELLOW, wants_color(sys.stderr))
        print(f"strain: {interrupted} sources restored, continue with --resume", file=sys.stderr)
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
