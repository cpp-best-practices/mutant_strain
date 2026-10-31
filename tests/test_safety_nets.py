import unittest
from pathlib import Path

from strain.build import CompileCommand
from strain.safety_nets import (
    DEFAULT_LEVELS,
    EXPLANATIONS,
    Level,
    SafetyNet,
    build_wide_problems,
    describe_build_wide,
    describe_missing,
    missing_safety_nets,
    resolve_levels,
)

FULL_FLAGS = (
    "g++",
    "--coverage",
    "-fsanitize=address,undefined",
    "-D_GLIBCXX_ASSERTIONS",
    "-ftrivial-auto-var-init=pattern",
    "-c",
    "a.cpp",
)


def command(name: str, arguments: tuple[str, ...]) -> CompileCommand:
    return CompileCommand(directory=Path("/b"), file=Path("/s") / name, arguments=arguments)


class MissingSafetyNetsTest(unittest.TestCase):
    def test_fully_protected_translation_unit(self) -> None:
        self.assertEqual(missing_safety_nets([command("a.cpp", FULL_FLAGS)]), {})

    def test_each_missing_check_lists_its_translation_units(self) -> None:
        bare = command("b.cpp", ("g++", "--coverage", "-fsanitize=address", "-c", "b.cpp"))
        separate = command(
            "c.cpp",
            ("g++", "--coverage", "-fsanitize=undefined", "-fsanitize=address", "-D_GLIBCXX_DEBUG", "-c", "c.cpp"),
        )
        self.assertEqual(
            missing_safety_nets([command("a.cpp", FULL_FLAGS), bare, separate]),
            {
                SafetyNet.UBSAN: [Path("/s/b.cpp")],
                SafetyNet.STDLIB_ASSERTIONS: [Path("/s/b.cpp")],
                SafetyNet.AUTO_VAR_INIT: [Path("/s/b.cpp"), Path("/s/c.cpp")],
            },
        )

    def test_libcxx_hardening_counts_as_stdlib_assertions(self) -> None:
        flags = ("clang++", "--coverage", "-fsanitize=address,undefined",
                 "-D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_FAST", "-ftrivial-auto-var-init=pattern", "-c", "a.cpp")
        self.assertEqual(missing_safety_nets([command("a.cpp", flags)]), {})

    def failing_nets(self, *extra: str) -> set[SafetyNet]:
        return set(missing_safety_nets([command("a.cpp", (*FULL_FLAGS, *extra))]))

    def test_ndebug_fails_unless_undefined_later(self) -> None:
        self.assertEqual(self.failing_nets("-DNDEBUG"), {SafetyNet.NDEBUG})
        self.assertEqual(self.failing_nets("-D", "NDEBUG=1"), {SafetyNet.NDEBUG})
        self.assertEqual(self.failing_nets("-DNDEBUG", "-UNDEBUG"), set())
        self.assertEqual(self.failing_nets("-UNDEBUG", "-DNDEBUG"), {SafetyNet.NDEBUG})
        self.assertEqual(self.failing_nets("-DNDEBUGGING"), set())

    def test_last_optimization_flag_decides(self) -> None:
        self.assertEqual(self.failing_nets("-O0"), set())
        self.assertEqual(self.failing_nets("-Og"), set())
        self.assertEqual(self.failing_nets("-O0", "-O2"), {SafetyNet.OPTIMIZATION})
        self.assertEqual(self.failing_nets("-O3", "-O0"), set())
        self.assertEqual(self.failing_nets("-O"), {SafetyNet.OPTIMIZATION})
        self.assertEqual(self.failing_nets("-Os"), {SafetyNet.OPTIMIZATION})

    def test_link_time_optimization_fails(self) -> None:
        self.assertEqual(self.failing_nets("-flto=auto"), {SafetyNet.LTO})
        self.assertEqual(self.failing_nets("-flto", "-fno-lto"), set())
        self.assertEqual(self.failing_nets("-flto-partition=none"), set())

    def test_translation_unit_without_coverage_is_partial_coverage(self) -> None:
        flags = tuple(flag for flag in FULL_FLAGS if flag != "--coverage")
        self.assertEqual(missing_safety_nets([command("a.cpp", flags)]), {SafetyNet.PARTIAL_COVERAGE: [Path("/s/a.cpp")]})
        self.assertEqual(
            missing_safety_nets([command("a.cpp", (*flags, "-fprofile-arcs", "-ftest-coverage"))]), {}
        )


class BuildWideProblemsTest(unittest.TestCase):
    def test_clean_build_has_no_problems(self) -> None:
        cache = {"CMAKE_UNITY_BUILD": "OFF", "CMAKE_INTERPROCEDURAL_OPTIMIZATION": ""}
        self.assertEqual(build_wide_problems(cache, [command("a.cpp", FULL_FLAGS)]), {})

    def test_cache_settings_are_reported_with_their_value(self) -> None:
        cache = {"CMAKE_UNITY_BUILD": "ON", "CMAKE_INTERPROCEDURAL_OPTIMIZATION": "TRUE"}
        self.assertEqual(
            build_wide_problems(cache, []),
            {
                SafetyNet.UNITY_BUILD: "CMAKE_UNITY_BUILD=ON",
                SafetyNet.LTO: "CMAKE_INTERPROCEDURAL_OPTIMIZATION=TRUE",
            },
        )

    def test_per_target_unity_build_is_found_in_the_compilation_database(self) -> None:
        unity = CompileCommand(
            directory=Path("/b"), file=Path("/b/CMakeFiles/lib.dir/Unity/unity_0_cxx.cxx"), arguments=FULL_FLAGS
        )
        problems = build_wide_problems({}, [command("a.cpp", FULL_FLAGS), unity])
        self.assertEqual(set(problems), {SafetyNet.UNITY_BUILD})
        self.assertIn("1 unity source(s), e.g. /b/CMakeFiles/lib.dir/Unity/unity_0_cxx.cxx", problems[SafetyNet.UNITY_BUILD])


class DescribeMissingTest(unittest.TestCase):
    def test_every_safety_net_explains_why_it_matters(self) -> None:
        self.assertEqual(set(EXPLANATIONS), set(SafetyNet))
        for net, explanation in EXPLANATIONS.items():
            with self.subTest(net=net):
                self.assertIn("mutant", explanation)

    def test_message_names_the_flag_to_add_and_the_reason(self) -> None:
        message = describe_missing(SafetyNet.AUTO_VAR_INIT, [Path("/s/b.cpp"), Path("/s/c.cpp")])
        self.assertIn("auto_var_init fails in 2 translation unit(s), e.g. /s/b.cpp", message)
        self.assertIn("add -ftrivial-auto-var-init=pattern", message)
        self.assertIn("uninitialized", message)

    def test_build_wide_message_names_the_evidence_and_the_fix(self) -> None:
        message = describe_build_wide(SafetyNet.UNITY_BUILD, "CMAKE_UNITY_BUILD=ON")
        self.assertIn("unity_build fails for the whole build (CMAKE_UNITY_BUILD=ON)", message)
        self.assertIn("-DCMAKE_UNITY_BUILD=OFF", message)


class ResolveLevelsTest(unittest.TestCase):
    def test_defaults_error_on_hidden_bugs_and_warn_on_lost_precision_or_speed(self) -> None:
        self.assertEqual(resolve_levels(config={}, cli=None), DEFAULT_LEVELS)
        self.assertEqual(set(DEFAULT_LEVELS), set(SafetyNet))
        errors = {net for net, level in DEFAULT_LEVELS.items() if level is Level.ERROR}
        self.assertEqual(
            errors,
            {SafetyNet.ASAN, SafetyNet.UBSAN, SafetyNet.STDLIB_ASSERTIONS, SafetyNet.AUTO_VAR_INIT, SafetyNet.NDEBUG},
        )

    def test_config_can_raise_a_warning_to_an_error(self) -> None:
        levels = resolve_levels(config={"optimization": "error"}, cli=None)
        self.assertIs(levels[SafetyNet.OPTIMIZATION], Level.ERROR)

    def test_config_overrides_individual_checks(self) -> None:
        levels = resolve_levels(config={"auto_var_init": "warn"}, cli=None)
        self.assertIs(levels[SafetyNet.AUTO_VAR_INIT], Level.WARN)
        self.assertIs(levels[SafetyNet.ASAN], Level.ERROR)

    def test_command_line_overrides_everything(self) -> None:
        levels = resolve_levels(config={"asan": "error"}, cli=Level.IGNORE)
        self.assertEqual(levels, dict.fromkeys(SafetyNet, Level.IGNORE))

    def test_unknown_config_key_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            resolve_levels(config={"msan": "error"}, cli=None)


if __name__ == "__main__":
    unittest.main()
