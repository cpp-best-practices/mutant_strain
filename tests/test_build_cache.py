import json
import tempfile
import unittest
from pathlib import Path

from strain.build_cache import CACHE_DIRECTORY, BuildFailureCache, cache_path
from strain.mutators import Mutant

BROKEN = Mutant("src/a.cpp", 3, 5, 40, "aor", "*", "/")
SAME_ID_ELSEWHERE = Mutant("src/b.cpp", 3, 5, 40, "aor", "*", "/")


class CachePathTest(unittest.TestCase):
    def test_is_named_after_the_commit_and_the_compilation_database(self) -> None:
        path = cache_path(Path("build"), "abc123", b"[]")
        self.assertEqual(path.parent, Path("build") / CACHE_DIRECTORY)
        self.assertTrue(path.name.startswith("abc123-"))

    def test_a_reconfigured_build_gets_a_different_cache(self) -> None:
        self.assertNotEqual(cache_path(Path("build"), "abc123", b"[]"), cache_path(Path("build"), "abc123", b"[{}]"))

    def test_a_new_commit_gets_a_different_cache(self) -> None:
        self.assertNotEqual(cache_path(Path("build"), "abc123", b"[]"), cache_path(Path("build"), "def456", b"[]"))


class BuildFailureCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / CACHE_DIRECTORY / "commit-fingerprint.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_missing_file_is_an_empty_cache(self) -> None:
        cache = BuildFailureCache.load(self.path)
        self.assertEqual(len(cache), 0)
        self.assertIsNone(cache.failure(BROKEN))

    def test_failures_survive_a_reload(self) -> None:
        BuildFailureCache.load(self.path).add(BROKEN, "build failed")

        reloaded = BuildFailureCache.load(self.path)

        self.assertEqual(reloaded.failure(BROKEN), "build failed")
        self.assertIsNone(reloaded.failure(SAME_ID_ELSEWHERE))
        self.assertEqual(len(reloaded), 1)

    def test_an_in_memory_cache_writes_nothing(self) -> None:
        cache = BuildFailureCache()
        cache.add(BROKEN, "build failed")
        self.assertEqual(cache.failure(BROKEN), "build failed")
        self.assertFalse(self.path.parent.exists())

    def test_a_corrupt_file_is_an_empty_cache(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{ not json")
        self.assertEqual(len(BuildFailureCache.load(self.path)), 0)

    def test_another_format_version_is_an_empty_cache(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"version": 0, "build_failures": {BROKEN.key: "build failed"}}))
        self.assertEqual(len(BuildFailureCache.load(self.path)), 0)


if __name__ == "__main__":
    unittest.main()
