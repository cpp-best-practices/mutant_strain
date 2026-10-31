import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from strain.build import (
    CompileCommand,
    CompilationDatabase,
    CompilerIdentity,
    build,
    cmake_is_true,
    read_cmake_cache,
    read_cxx_compiler,
    target_artifacts,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_project"

ENTRIES = [
    {
        "directory": "/work/build",
        "command": "/usr/bin/g++ -I/work/include -MD -MT obj.o -MF obj.o.d --coverage -o obj.o -c /work/src/math.cpp",
        "file": "/work/src/math.cpp",
    },
    {
        "directory": "/work/build",
        "arguments": ["/usr/bin/g++", "-fsanitize=address", "-o", "t.o", "-c", "../tests/t.cpp"],
        "file": "../tests/t.cpp",
    },
]


class CompilationDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.database = CompilationDatabase.from_entries(ENTRIES)

    def test_lookup_by_absolute_path(self) -> None:
        command = self.database.command_for(Path("/work/tests/t.cpp"))
        assert command is not None
        self.assertEqual(command.arguments[1], "-fsanitize=address")

    def test_unknown_file(self) -> None:
        self.assertIsNone(self.database.command_for(Path("/work/include/math.hpp")))

    def test_syntax_only_arguments_drop_outputs_and_dependency_files(self) -> None:
        command = self.database.command_for(Path("/work/src/math.cpp"))
        assert command is not None
        self.assertEqual(
            command.syntax_only_arguments(),
            ["/usr/bin/g++", "-I/work/include", "--coverage", "/work/src/math.cpp", "-fsyntax-only"],
        )

    def test_commands_are_listed(self) -> None:
        self.assertEqual(
            [command.file for command in self.database.commands],
            [Path("/work/src/math.cpp"), Path("/work/tests/t.cpp")],
        )

    def test_compile_command_is_a_value(self) -> None:
        command = CompileCommand(directory=Path("/b"), file=Path("/b/a.cpp"), arguments=("g++", "-c", "a.cpp"))
        self.assertEqual(command.syntax_only_arguments(), ["g++", "a.cpp", "-fsyntax-only"])


def compile_command(*arguments: str) -> CompileCommand:
    return CompileCommand(directory=Path("/b"), file=Path("/b/a.cpp"), arguments=("g++", *arguments, "-c", "a.cpp"))


class CoverageFlagsTest(unittest.TestCase):
    def test_gcov_instrumentation(self) -> None:
        self.assertTrue(compile_command("--coverage").has_gcov_instrumentation())
        self.assertTrue(compile_command("-fprofile-arcs", "-ftest-coverage").has_gcov_instrumentation())
        self.assertFalse(compile_command("-fprofile-arcs").has_gcov_instrumentation())
        self.assertFalse(compile_command().has_gcov_instrumentation())

    def test_source_based_coverage(self) -> None:
        self.assertTrue(compile_command("-fprofile-instr-generate", "-fcoverage-mapping").has_source_based_coverage())
        self.assertFalse(compile_command("--coverage").has_source_based_coverage())


class BuildFolderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.build_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cache_entries_are_keyed_by_name(self) -> None:
        (self.build_dir / "CMakeCache.txt").write_text(
            "# comment\n// help text\n\nCMAKE_UNITY_BUILD:BOOL=ON\nCMAKE_HOME_DIRECTORY:INTERNAL=/src/a=b\n"
        )
        self.assertEqual(
            read_cmake_cache(self.build_dir), {"CMAKE_UNITY_BUILD": "ON", "CMAKE_HOME_DIRECTORY": "/src/a=b"}
        )

    def test_missing_cache_is_empty(self) -> None:
        self.assertEqual(read_cmake_cache(self.build_dir), {})

    def test_cmake_truth(self) -> None:
        for value in ("ON", "on", "1", "YES", "TRUE", "Y", "2"):
            with self.subTest(value=value):
                self.assertTrue(cmake_is_true(value))
        for value in ("", "OFF", "0", "NO", "FALSE", "NOTFOUND", "IGNORE"):
            with self.subTest(value=value):
                self.assertFalse(cmake_is_true(value))

    def test_compiler_is_read_from_cmake_files(self) -> None:
        compiler_dir = self.build_dir / "CMakeFiles" / "3.28.1"
        compiler_dir.mkdir(parents=True)
        (compiler_dir / "CMakeCXXCompiler.cmake").write_text(
            'set(CMAKE_CXX_COMPILER "/usr/bin/c++")\nset(CMAKE_CXX_COMPILER_ID "GNU")\n'
            'set(CMAKE_CXX_COMPILER_VERSION "13.2.0")\n'
        )
        self.assertEqual(read_cxx_compiler(self.build_dir), CompilerIdentity("GNU", "13.2.0"))

    def test_unknown_compiler_is_none(self) -> None:
        self.assertIsNone(read_cxx_compiler(self.build_dir))


@unittest.skipUnless(shutil.which("cmake") and shutil.which("g++"), "needs cmake and g++")
class TargetBuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.build_dir = Path(self._tmp.name).resolve()
        subprocess.run(["cmake", "-S", str(FIXTURE), "-B", str(self.build_dir)], check=True, capture_output=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_artifacts_map_to_their_targets(self) -> None:
        artifacts = target_artifacts(self.build_dir)
        self.assertEqual(artifacts.get(self.build_dir / "sample_tests"), "sample_tests")
        self.assertEqual(artifacts.get(self.build_dir / "other_tests"), "other_tests")

    def test_building_a_target_leaves_other_targets_alone(self) -> None:
        self.assertTrue(build(self.build_dir, "sample_tests"))
        self.assertTrue((self.build_dir / "sample_tests").is_file())
        self.assertFalse((self.build_dir / "other_tests").exists())


if __name__ == "__main__":
    unittest.main()
