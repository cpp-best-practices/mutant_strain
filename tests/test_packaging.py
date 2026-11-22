import email
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from email.message import Message
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# What a wheel or an sdist is built from: everything else in the checkout stays out of the distributions.
DISTRIBUTED = ["pyproject.toml", "README.md", "LICENSE.txt", "strain"]


def build(kind: str, out: Path) -> Path:
    """Build a wheel or sdist from a clean copy of the project, as `python -m build` would, and return its path."""
    source = out / "source"
    source.mkdir()
    for name in DISTRIBUTED:
        if (ROOT / name).is_dir():
            shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(ROOT / name, source / name)
    script = f"import sys; from setuptools import build_meta; print(build_meta.build_{kind}(sys.argv[1]))"
    result = subprocess.run(
        [sys.executable, "-c", script, str(out)], cwd=source, capture_output=True, text=True, check=True
    )
    return out / result.stdout.strip().splitlines()[-1]


class WheelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        wheel = build("wheel", Path(cls.tmp.name))
        with zipfile.ZipFile(wheel) as archive:
            cls.names = archive.namelist()
            info = next(name for name in cls.names if name.endswith(".dist-info/METADATA"))
            cls.metadata: Message = email.message_from_bytes(archive.read(info))
            points = next(name for name in cls.names if name.endswith(".dist-info/entry_points.txt"))
            cls.entry_points = archive.read(points).decode("utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_the_distribution_name_is_free_on_pypi(self) -> None:
        # "strain" belongs to an unrelated project on PyPI, so the distribution is named after the repository.
        self.assertEqual(self.metadata["Name"], "mutant-strain")

    def test_the_wheel_installs_the_strain_package_and_command(self) -> None:
        self.assertIn("strain/cli.py", self.names)
        self.assertFalse([name for name in self.names if name.startswith("tests/")])
        self.assertRegex(self.entry_points, r"(?m)^mutant_strain = strain\.cli:main$")

    def test_the_metadata_declares_the_license(self) -> None:
        self.assertEqual(self.metadata["License-Expression"], "Unlicense")
        self.assertTrue(any(name.endswith("LICENSE.txt") for name in self.names))

    def test_the_readme_is_the_long_description(self) -> None:
        self.assertEqual(self.metadata["Description-Content-Type"], "text/markdown")
        self.assertIn("Mutation testing for C++", self.metadata.get_payload())

    def test_the_metadata_links_to_the_repository(self) -> None:
        urls = self.metadata.get_all("Project-URL") or []
        self.assertTrue(any("github.com/cpp-best-practices/mutant_strain" in url for url in urls), urls)

    def test_the_metadata_has_classifiers_and_keywords(self) -> None:
        classifiers = self.metadata.get_all("Classifier") or []
        self.assertIn("Programming Language :: Python :: 3", classifiers)
        self.assertIn("Programming Language :: C++", classifiers)
        self.assertIn("mutation-testing", self.metadata["Keywords"])


class SdistTest(unittest.TestCase):
    def test_the_sdist_carries_the_license_and_readme(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sdist = build("sdist", Path(tmp))
            self.assertTrue(sdist.name.startswith("mutant_strain-"), sdist.name)
            with tarfile.open(sdist) as archive:
                names = archive.getnames()
            self.assertTrue(any(name.endswith("/LICENSE.txt") for name in names))
            self.assertTrue(any(name.endswith("/README.md") for name in names))


if __name__ == "__main__":
    unittest.main()
