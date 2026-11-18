import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from strain.ctest import MEGABYTE, MemoryLimit, executables_by_test, run_test, uses_asan
from strain.results import MutantState, classify_run

FIXTURE = Path(__file__).parent / "fixtures" / "sample_project"


@unittest.skipUnless(
    shutil.which("cmake") and shutil.which("ctest") and shutil.which("g++"), "needs cmake, ctest and g++"
)
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


GROWING_TEST = """
#include <vector>
int main() { std::vector<std::vector<char>> all; for (;;) { all.emplace_back(1 << 20, 'x'); } }
"""
LIMIT_MEGABYTES = 256
# ASan checks the resident set size about ten times a second, so it can overshoot by what one tenth of a second adds.
MARGIN_MEGABYTES = 256


class MemoryLimitTest(unittest.TestCase):
    def test_asan_programs_get_the_limit_through_asan_options_after_the_users_own(self) -> None:
        with mock.patch.dict(os.environ, {"ASAN_OPTIONS": "detect_leaks=0:hard_rss_limit_mb=99"}):
            limit = MemoryLimit(512, asan=True)
            self.assertEqual(limit.wrap(["ctest"]), ["ctest"])
            self.assertEqual(
                limit.environment()["ASAN_OPTIONS"],  # type: ignore[index]
                "hard_rss_limit_mb=512:allocator_may_return_null=0:detect_leaks=0:hard_rss_limit_mb=99",
            )

    def test_other_programs_get_a_data_segment_limit_in_kilobytes(self) -> None:
        limit = MemoryLimit(512, asan=False)
        self.assertEqual(limit.wrap(["ctest", "-R", "t"])[3:], [str(512 * 1024), "ctest", "-R", "t"])
        self.assertIsNone(limit.environment())

    def test_asan_programs_are_told_apart_by_the_asan_runtime_they_call(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            plain, asan = Path(folder) / "plain", Path(folder) / "asan"
            plain.write_bytes(b"\x7fELF main printf")
            asan.write_bytes(b"\x7fELF main __asan_init_v8 printf")
            self.assertFalse(uses_asan(plain))
            self.assertTrue(uses_asan(asan))
            self.assertFalse(uses_asan(Path(folder) / "missing"))
            self.assertFalse(uses_asan(Path(folder)))


@unittest.skipUnless(sys.platform.startswith("linux"), "ASan's RSS limit and RLIMIT_DATA are enforced on Linux")
@unittest.skipUnless(shutil.which("cmake") and shutil.which("ctest") and shutil.which("g++"), "needs a C++ toolchain")
class RunawayAllocationTest(unittest.TestCase):
    """A test that allocates forever stops at its memory limit, with and without AddressSanitizer."""

    def build(self, flags: str) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        source, build = Path(self._tmp.name) / "src", Path(self._tmp.name) / "build"
        source.mkdir()
        (source / "grow.cpp").write_text(GROWING_TEST)
        (source / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.14)\nproject(grow CXX)\nenable_testing()\n"
            "add_executable(grow grow.cpp)\nadd_test(NAME grow COMMAND grow)\n"
        )
        configure = ["cmake", "-S", str(source), "-B", str(build), f"-DCMAKE_CXX_FLAGS={flags}"]
        subprocess.run([*configure, f"-DCMAKE_EXE_LINKER_FLAGS={flags}"], check=True, capture_output=True)
        subprocess.run(["cmake", "--build", str(build)], check=True, capture_output=True)
        return build

    def assert_stopped_at_the_limit(self, build: Path) -> None:
        limit = MemoryLimit(LIMIT_MEGABYTES, asan=uses_asan(build / "grow"))
        run = run_test(build, "grow", timeout=60, memory=limit)
        self.assertFalse(run.timed_out, run.output)
        self.assertEqual(
            classify_run(run, LIMIT_MEGABYTES * MEGABYTE), MutantState.KILLED_BY_RESOURCE_LIMIT, run.output
        )
        assert run.peak_memory is not None
        self.assertLess(run.peak_memory, (LIMIT_MEGABYTES + MARGIN_MEGABYTES) * MEGABYTE)

    def test_with_asan(self) -> None:
        build = self.build("-fsanitize=address")
        self.assertTrue(uses_asan(build / "grow"))
        self.assert_stopped_at_the_limit(build)

    def test_without_asan(self) -> None:
        build = self.build("-O0")
        self.assertFalse(uses_asan(build / "grow"))
        self.assert_stopped_at_the_limit(build)

    def test_the_baseline_measures_the_peak_memory(self) -> None:
        build = self.build("-O0")
        (build / "CTestTestfile.cmake").write_text(
            f'add_test(grow "{sys.executable}" "-c" "bytearray(200 * {MEGABYTE})[::4096]")\n'
        )
        run = run_test(build, "grow", timeout=60)
        assert run.peak_memory is not None
        self.assertGreater(run.peak_memory, 190 * MEGABYTE, run.output)


if __name__ == "__main__":
    unittest.main()
