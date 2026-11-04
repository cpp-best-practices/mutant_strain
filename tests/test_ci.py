import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


class ProjectValidatorJobTest(unittest.TestCase):
    def test_ci_checks_out_the_validator_main_branch(self) -> None:
        text = workflow_text()
        self.assertIn("repository: cpp-best-practices/project_validator", text)
        self.assertRegex(text, r"\bref: main\b")

    def test_ci_runs_the_validator_on_this_project(self) -> None:
        self.assertRegex(workflow_text(), r"python3? -m project_validator\b[^\n]*--exclude tests/fixtures")


class ValidatorStandardsTest(unittest.TestCase):
    def test_ci_runs_the_tests_with_warnings_as_errors(self) -> None:
        self.assertRegex(workflow_text(), r"python -W error -m pytest")

    def test_ci_measures_branch_coverage(self) -> None:
        self.assertRegex(workflow_text(), r"coverage run --branch -m pytest")

    def test_ci_enforces_formatting(self) -> None:
        self.assertIn("ruff format --check", workflow_text())
        self.assertRegex((ROOT / "pyproject.toml").read_text(encoding="utf-8"), r"(?m)^\[tool\.ruff\]")

    def test_ci_covers_several_operating_systems_and_python_versions(self) -> None:
        matrix = re.search(r"os: \[(?P<os>[^\]]*)\]\s+python: \[(?P<python>[^\]]*)\]", workflow_text())
        self.assertIsNotNone(matrix)
        assert matrix is not None
        self.assertGreaterEqual(len(matrix["os"].split(",")), 2)
        self.assertGreaterEqual(len(matrix["python"].split(",")), 2)

    def test_dependencies_are_updated_automatically(self) -> None:
        self.assertTrue((ROOT / ".github" / "dependabot.yml").is_file())


if __name__ == "__main__":
    unittest.main()
