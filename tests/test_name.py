import ast
import contextlib
import io
import re
import tomllib
import unittest
from pathlib import Path

from strain import NAME
from strain.cli import build_parser

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "strain"

# The Python package, `strain.toml` and the report, cache and journal folders keep the short name.
OLD_NAME = re.compile(r"(?<![-./\w])strain\b(?![-.]\w)")


def docstrings(tree: ast.Module) -> set[int]:
    nodes = [tree, *(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.ClassDef))]
    return {id(node.body[0].value) for node in nodes if ast.get_docstring(node) is not None}


def messages(source: Path) -> list[tuple[int, str]]:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    skipped = docstrings(tree)
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skipped
    ]


def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


class ProjectNameTest(unittest.TestCase):
    def test_the_project_is_named_mutant_strain(self) -> None:
        self.assertEqual(NAME, "mutant_strain")

    def test_the_distribution_is_named_mutant_strain(self) -> None:
        self.assertEqual(pyproject()["project"]["name"], NAME)

    def test_the_command_is_installed_as_mutant_strain(self) -> None:
        self.assertEqual(pyproject()["project"]["scripts"][NAME], "strain.cli:main")

    def test_the_help_names_mutant_strain(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            build_parser().parse_args(["--help"])
        self.assertTrue(out.getvalue().startswith(f"usage: {NAME} "))

    def test_no_message_uses_the_old_name(self) -> None:
        for source in sorted(SOURCES.glob("*.py")):
            for number, text in messages(source):
                with self.subTest(file=source.name, line=number):
                    self.assertIsNone(OLD_NAME.search(text), text)

    def test_messages_take_the_name_from_strain_name(self) -> None:
        for source in sorted(SOURCES.glob("*.py")):
            if source.name == "__init__.py":
                continue
            for number, text in messages(source):
                with self.subTest(file=source.name, line=number):
                    self.assertNotIn(NAME, text)


if __name__ == "__main__":
    unittest.main()
