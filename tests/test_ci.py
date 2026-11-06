import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "ci.yml"
ACTION_REFERENCE = re.compile(r"^\s*(?:-\s*)?uses:\s*(?P<reference>\S+)", re.MULTILINE)
FULL_COMMIT = re.compile(r"@[0-9a-f]{40}")


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


def action_references() -> list[str]:
    return [
        match["reference"]
        for workflow in sorted(WORKFLOWS.glob("*.yml"))
        for match in ACTION_REFERENCE.finditer(workflow.read_text(encoding="utf-8"))
    ]


class ProjectValidatorFindingsTest(unittest.TestCase):
    """Each test answers one finding `project_validator --strict` reported in CI."""

    def test_every_action_is_pinned_to_a_full_commit(self) -> None:
        references = action_references()
        self.assertTrue(references)
        self.assertEqual([reference for reference in references if not FULL_COMMIT.search(reference)], [])

    def test_ci_lints_its_workflows(self) -> None:
        self.assertRegex(workflow_text(), r"\bzizmor\b[^\n]*\.github")

    def test_ci_audits_the_dependencies(self) -> None:
        self.assertIn("pypa/gh-action-pip-audit@", workflow_text())

    def test_ci_scans_for_secrets(self) -> None:
        self.assertIn("trufflesecurity/trufflehog@", workflow_text())

    def test_ci_type_checks_strictly(self) -> None:
        self.assertRegex(workflow_text(), r"python -m mypy\b")
        self.assertRegex((ROOT / "pyproject.toml").read_text(encoding="utf-8"), r"(?m)^\[tool\.mypy\]\nstrict = true$")

    def test_security_policy_says_how_to_report_a_vulnerability(self) -> None:
        policy = ROOT / "SECURITY.md"
        self.assertTrue(policy.is_file())
        self.assertIn("/security/advisories/new", policy.read_text(encoding="utf-8"))

    def test_readme_shows_ci_and_quality_badges(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("actions/workflows/ci.yml/badge.svg", readme)
        self.assertRegex(readme, r"api\.scorecard\.dev/projects/\S+/badge")

    def test_scorecard_results_are_published_for_the_badge(self) -> None:
        scorecard = WORKFLOWS / "scorecard.yml"
        self.assertTrue(scorecard.is_file())
        self.assertIn("publish_results: true", scorecard.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
