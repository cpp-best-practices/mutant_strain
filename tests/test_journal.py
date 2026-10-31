import tempfile
import unittest
from pathlib import Path

from strain.journal import Journal


class JournalTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = self.root / "a.cpp"
        self.source.write_bytes(b"int f() { return 1 + 2; }\n")
        self.journal = Journal(self.root / "journal")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_file_is_mutated_inside_and_restored_after(self) -> None:
        with self.journal.mutated(self.source, b"int f() { return 1 - 2; }\n"):
            self.assertEqual(self.source.read_bytes(), b"int f() { return 1 - 2; }\n")
            self.assertEqual(len(list((self.root / "journal").iterdir())), 1)
        self.assertEqual(self.source.read_bytes(), b"int f() { return 1 + 2; }\n")
        self.assertEqual(list((self.root / "journal").iterdir()), [])

    def test_file_is_restored_on_keyboard_interrupt(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            with self.journal.mutated(self.source, b"broken"):
                raise KeyboardInterrupt
        self.assertEqual(self.source.read_bytes(), b"int f() { return 1 + 2; }\n")

    def test_recover_restores_files_left_by_a_crashed_run(self) -> None:
        crashed = self.journal.mutated(self.source, b"left over")
        crashed.__enter__()
        self.assertEqual(self.source.read_bytes(), b"left over")

        restored = Journal(self.root / "journal").recover()

        self.assertEqual(restored, [self.source])
        self.assertEqual(self.source.read_bytes(), b"int f() { return 1 + 2; }\n")
        self.assertEqual(list((self.root / "journal").iterdir()), [])

    def test_recover_with_no_journal_is_a_no_op(self) -> None:
        self.assertEqual(Journal(self.root / "missing").recover(), [])


if __name__ == "__main__":
    unittest.main()
