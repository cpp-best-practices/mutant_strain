import unittest
from pathlib import Path

from strain.build import CompileCommand
from strain.safety_nets import Level, SafetyNet, missing_safety_nets, resolve_levels

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
        bare = command("b.cpp", ("g++", "-fsanitize=address", "-c", "b.cpp"))
        separate = command("c.cpp", ("g++", "-fsanitize=undefined", "-fsanitize=address", "-D_GLIBCXX_DEBUG", "-c", "c.cpp"))
        self.assertEqual(
            missing_safety_nets([command("a.cpp", FULL_FLAGS), bare, separate]),
            {
                SafetyNet.UBSAN: [Path("/s/b.cpp")],
                SafetyNet.STDLIB_ASSERTIONS: [Path("/s/b.cpp")],
                SafetyNet.AUTO_VAR_INIT: [Path("/s/b.cpp"), Path("/s/c.cpp")],
            },
        )

    def test_libcxx_hardening_counts_as_stdlib_assertions(self) -> None:
        flags = ("clang++", "-fsanitize=address,undefined", "-D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_FAST",
                 "-ftrivial-auto-var-init=pattern", "-c", "a.cpp")
        self.assertEqual(missing_safety_nets([command("a.cpp", flags)]), {})


class ResolveLevelsTest(unittest.TestCase):
    def test_default_is_error(self) -> None:
        self.assertEqual(resolve_levels(config={}, cli=None), dict.fromkeys(SafetyNet, Level.ERROR))

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
