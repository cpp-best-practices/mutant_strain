import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from strain.objects import ObjectFingerprints, object_fingerprint

# The instrumentation the end-to-end fixture builds with: gcov stamps each object, the sanitizers add their own data.
FLAGS = ("-g", "--coverage", "-fsanitize=address,undefined", "-ftrivial-auto-var-init=pattern")
SOURCE = "int offset(int v) { return v + 0; }\nbool less(int a, int b) { return a < b; }\n"


@unittest.skipUnless(shutil.which("g++"), "needs g++")
class ObjectFingerprintTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.object = self.tmp / "a.o"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def compile(self, source: str) -> str | None:
        """Compiles `source` to the same object path every time, as a build does."""
        (self.tmp / "a.cpp").write_text(source)
        subprocess.run(["g++", *FLAGS, "-c", "a.cpp", "-o", "a.o"], cwd=self.tmp, check=True, capture_output=True)
        return object_fingerprint(self.object)

    def test_compiling_the_same_source_again_gives_the_same_fingerprint(self) -> None:
        first = self.compile(SOURCE)
        # gcov's stamp changes with the time of the compile.
        time.sleep(1.1)
        self.assertIsNotNone(first)
        self.assertEqual(self.compile(SOURCE), first)

    def test_a_mutant_that_compiles_to_the_same_code_gives_the_same_fingerprint(self) -> None:
        original = self.compile(SOURCE)
        self.assertEqual(self.compile(SOURCE.replace("v + 0", "v - 0")), original)

    def test_a_mutant_that_changes_the_code_gives_another_fingerprint(self) -> None:
        original = self.compile(SOURCE)
        # Same length, so only the code differs, not the columns in the debug information or sanitizer data.
        self.assertNotEqual(self.compile(SOURCE.replace("a < b", "a > b")), original)
        self.assertNotEqual(self.compile(SOURCE.replace("v + 0", "v + 1")), original)

    def test_a_mutant_in_a_global_initializer_gives_another_fingerprint(self) -> None:
        original = self.compile("int limit = 2 + 3;\n")
        self.assertNotEqual(self.compile("int limit = 2 - 3;\n"), original)

    def test_rebuilt_objects_are_named_and_compared_with_the_originals(self) -> None:
        self.compile(SOURCE)
        fingerprints = ObjectFingerprints.of([self.object])
        fingerprints.snapshot()
        self.assertEqual(fingerprints.rebuilt(), [])

        self.compile(SOURCE.replace("v + 0", "v - 0"))
        self.assertEqual(fingerprints.rebuilt(), [self.object])
        self.assertTrue(fingerprints.all_original([self.object]))

        fingerprints.snapshot()
        self.compile(SOURCE.replace("a < b", "a > b"))
        self.assertFalse(fingerprints.all_original(fingerprints.rebuilt()))


class UnreadableObjectTest(unittest.TestCase):
    def test_files_that_are_not_64_bit_elf_objects_have_no_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name, content in (("text.o", b"not an object"), ("elf32.o", b"\x7fELF\x01\x01" + bytes(58))):
                with self.subTest(name=name):
                    path = Path(tmp) / name
                    path.write_bytes(content)
                    self.assertIsNone(object_fingerprint(path))
            self.assertIsNone(object_fingerprint(Path(tmp) / "missing.o"))

    def test_an_object_without_a_fingerprint_is_never_original(self) -> None:
        fingerprints = ObjectFingerprints({Path("/missing.o"): None})
        self.assertFalse(fingerprints.all_original([Path("/missing.o")]))
        self.assertFalse(fingerprints.all_original([Path("/unknown.o")]))
        self.assertTrue(fingerprints.all_original([]))


if __name__ == "__main__":
    unittest.main()
