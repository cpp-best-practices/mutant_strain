import tempfile
import unittest
from pathlib import Path

from strain.config import Config, load_config

STRAIN_TOML = """\
exclude = ["tests/**", "third_party/**"]
arid = ["LOG_*", "spdlog::*"]

[safety_nets]
asan = "error"
auto_var_init = "warn"

[ignore]
mutants = ["a1b2c3d4"]
"""


class LoadConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_file_gives_defaults(self) -> None:
        self.assertEqual(load_config(self.root), Config())

    def test_readme_example(self) -> None:
        (self.root / "strain.toml").write_text(STRAIN_TOML)
        self.assertEqual(
            load_config(self.root),
            Config(
                exclude=("tests/**", "third_party/**"),
                arid=("LOG_*", "spdlog::*"),
                safety_nets={"asan": "error", "auto_var_init": "warn"},
                ignored_mutants=frozenset({"a1b2c3d4"}),
            ),
        )

    def test_unknown_key_is_rejected(self) -> None:
        (self.root / "strain.toml").write_text('exlude = ["tests/**"]\n')
        with self.assertRaisesRegex(ValueError, "exlude"):
            load_config(self.root)


class ExcludeTest(unittest.TestCase):
    def test_globs_match_relative_paths(self) -> None:
        config = Config(exclude=("tests/**", "third_party/**"))
        self.assertTrue(config.is_excluded("tests/unit/a.cpp"))
        self.assertTrue(config.is_excluded("third_party/x.hpp"))
        self.assertFalse(config.is_excluded("src/tests.cpp"))


if __name__ == "__main__":
    unittest.main()
