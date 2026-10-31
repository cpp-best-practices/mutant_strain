import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from strain.ctest import executables_by_test

FIXTURE = Path(__file__).parent / "fixtures" / "sample_project"


@unittest.skipUnless(shutil.which("cmake") and shutil.which("ctest") and shutil.which("g++"), "needs cmake, ctest and g++")
class TestExecutablesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.build_dir = Path(self._tmp.name).resolve()
        subprocess.run(["cmake", "-S", str(FIXTURE), "-B", str(self.build_dir)], check=True, capture_output=True)
        subprocess.run(["cmake", "--build", str(self.build_dir)], check=True, capture_output=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_each_test_maps_to_the_program_it_runs(self) -> None:
        self.assertEqual(
            executables_by_test(self.build_dir),
            {"sample_tests": self.build_dir / "sample_tests", "other_tests": self.build_dir / "other_tests"},
        )


if __name__ == "__main__":
    unittest.main()
