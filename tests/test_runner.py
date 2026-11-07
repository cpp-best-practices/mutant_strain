import os
import unittest
from pathlib import Path

from strain.runner import short_path


class ShortPathTest(unittest.TestCase):
    def test_a_path_under_the_working_directory_is_shown_relative(self) -> None:
        self.assertEqual(short_path(Path.cwd() / "build" / "debug"), os.path.join("build", "debug"))

    def test_a_path_far_from_the_working_directory_stays_absolute(self) -> None:
        self.assertEqual(short_path(Path(Path.cwd().anchor)), str(Path(Path.cwd().anchor)))


if __name__ == "__main__":
    unittest.main()
