import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from strain.build import (
    BuildClock,
    BuildTool,
    CompileCommand,
    CompilationDatabase,
    CompilerIdentity,
    build,
    build_command,
    cmake_is_true,
    direct_build_tool,
    direct_jobs,
    generator_warning,
    linker_advice,
    parallel_jobs,
    read_cmake_cache,
    read_cxx_compiler,
    target_artifacts,
    ToolKind,
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


class BuildClockTest(unittest.TestCase):
    def test_a_write_after_the_wait_lands_in_a_later_second_than_the_build(self) -> None:
        clock = BuildClock()
        clock.build_finished()

        clock.wait_until_writes_outdate_the_build()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "a.cpp"
            source.write_bytes(b"int f();\n")

            self.assertGreater(int(source.stat().st_mtime), int(clock.last_build_finished))

    def test_no_wait_before_the_first_build(self) -> None:
        start = time.monotonic()
        BuildClock().wait_until_writes_outdate_the_build()
        self.assertLess(time.monotonic() - start, 0.5)


class CompilationDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.database = CompilationDatabase.from_entries(ENTRIES)

    def test_lookup_by_absolute_path(self) -> None:
        command = self.database.command_for(Path("/work/tests/t.cpp"))
        assert command is not None
        self.assertEqual(command.arguments[1], "-fsanitize=address")

    def test_unknown_file(self) -> None:
        self.assertIsNone(self.database.command_for(Path("/work/include/math.hpp")))

    def test_the_object_each_command_writes(self) -> None:
        self.assertEqual(self.database.outputs(), [Path("/work/build/obj.o"), Path("/work/build/t.o")])

    def test_an_attached_output_flag(self) -> None:
        command = CompileCommand(Path("/b"), Path("/b/a.cpp"), ("g++", "-oout/a.o", "-c", "a.cpp"))
        self.assertEqual(command.output, Path("/b/out/a.o"))

    def test_a_command_without_an_output(self) -> None:
        self.assertIsNone(CompileCommand(Path("/b"), Path("/b/a.cpp"), ("g++", "-c", "a.cpp")).output)

    def test_every_command_of_a_source_built_into_two_targets(self) -> None:
        entry = {"directory": "/work/build", "file": "/work/src/a.cpp"}
        database = CompilationDatabase.from_entries(
            [{**entry, "command": "g++ -o one.o -c /work/src/a.cpp"}, {**entry, "command": "g++ -o two.o -c a.cpp"}]
        )
        outputs = [command.output for command in database.commands_for(Path("/work/src/a.cpp"))]
        self.assertEqual(outputs, [Path("/work/build/one.o"), Path("/work/build/two.o")])

    def test_commands_are_listed(self) -> None:
        self.assertEqual(
            [command.file for command in self.database.commands],
            [Path("/work/src/math.cpp"), Path("/work/tests/t.cpp")],
        )

    def test_compile_command_is_a_value(self) -> None:
        command = CompileCommand(directory=Path("/b"), file=Path("/b/a.cpp"), arguments=("g++", "-c", "a.cpp"))
        self.assertEqual(command, CompileCommand(Path("/b"), Path("/b/a.cpp"), ("g++", "-c", "a.cpp")))


class SymlinkedCompilationDatabaseTest(unittest.TestCase):
    """Like macOS, where the temporary directory /var/folders is a symlink into /private/var."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.real_dir = Path(self._tmp.name).resolve() / "private"
        (self.real_dir / "src").mkdir(parents=True)
        (self.real_dir / "src" / "a.cpp").write_text("int f() { return 1; }\n")
        self.link_dir = Path(self._tmp.name).resolve() / "linked"
        self.link_dir.symlink_to(self.real_dir, target_is_directory=True)
        self.database = CompilationDatabase.from_entries(
            [
                {
                    "directory": str(self.link_dir / "build"),
                    "arguments": ["g++", "-c", "../src/a.cpp"],
                    "file": "../src/a.cpp",
                }
            ]
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_files_are_listed_by_their_resolved_path(self) -> None:
        self.assertEqual([command.file for command in self.database.commands], [self.real_dir / "src" / "a.cpp"])

    def test_lookup_by_either_path(self) -> None:
        self.assertIsNotNone(self.database.command_for(self.real_dir / "src" / "a.cpp"))
        self.assertIsNotNone(self.database.command_for(self.link_dir / "src" / "a.cpp"))


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


class GeneratorTest(unittest.TestCase):
    def test_ninja_builds_need_no_warning(self) -> None:
        self.assertIsNone(generator_warning({"CMAKE_GENERATOR": "Ninja"}))
        self.assertIsNone(generator_warning({"CMAKE_GENERATOR": "Ninja Multi-Config"}))

    def test_other_generators_are_warned_about_with_the_fix(self) -> None:
        warning = generator_warning({"CMAKE_GENERATOR": "Unix Makefiles"})
        assert warning is not None
        self.assertIn("Unix Makefiles", warning)
        self.assertIn("-G Ninja", warning)

    def test_makefile_builds_run_one_job_per_cpu(self) -> None:
        self.assertEqual(parallel_jobs({"CMAKE_GENERATOR": "Unix Makefiles"}, {}, 8), 8)
        self.assertEqual(parallel_jobs({"CMAKE_GENERATOR": "MinGW Makefiles"}, {}, 4), 4)

    def test_generators_that_parallelize_on_their_own_are_left_alone(self) -> None:
        self.assertIsNone(parallel_jobs({"CMAKE_GENERATOR": "Ninja"}, {}, 8))
        self.assertIsNone(parallel_jobs({"CMAKE_GENERATOR": "NMake Makefiles"}, {}, 8))
        self.assertIsNone(parallel_jobs({}, {}, 8))

    def test_an_explicit_parallel_level_wins(self) -> None:
        cache = {"CMAKE_GENERATOR": "Unix Makefiles"}
        self.assertIsNone(parallel_jobs(cache, {"CMAKE_BUILD_PARALLEL_LEVEL": "2"}, 8))
        self.assertIsNone(parallel_jobs(cache, {"MAKEFLAGS": "-j3"}, 8))
        self.assertIsNone(parallel_jobs(cache, {"MAKEFLAGS": "--jobs=3"}, 8))
        self.assertEqual(parallel_jobs(cache, {"MAKEFLAGS": "-k"}, 8), 8)

    def test_build_command_passes_the_job_count(self) -> None:
        self.assertEqual(
            build_command(Path("b"), "t", jobs=6), ["cmake", "--build", "b", "--target", "t", "--parallel", "6"]
        )
        self.assertEqual(build_command(Path("b")), ["cmake", "--build", "b"])


class DirectBuildToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.build_dir = Path(self._tmp.name)
        self.program = self.build_dir / "tool"
        self.program.write_text("#!/bin/sh\n")
        self.program.chmod(0o755)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def cache(self, generator: str) -> dict[str, str]:
        return {"CMAKE_GENERATOR": generator, "CMAKE_MAKE_PROGRAM": str(self.program)}

    def test_ninja_and_make_run_directly(self) -> None:
        (self.build_dir / "build.ninja").touch()
        (self.build_dir / "Makefile").touch()
        program = str(self.program)
        self.assertEqual(direct_build_tool(self.build_dir, self.cache("Ninja")), BuildTool(ToolKind.NINJA, program))
        self.assertEqual(
            direct_build_tool(self.build_dir, self.cache("Unix Makefiles")), BuildTool(ToolKind.MAKE, program)
        )

    def test_generators_cmake_must_drive_stay_with_cmake_build(self) -> None:
        (self.build_dir / "build.ninja").touch()
        for generator in ("Ninja Multi-Config", "Visual Studio 17 2022", "Xcode", "NMake Makefiles", ""):
            with self.subTest(generator=generator):
                self.assertIsNone(direct_build_tool(self.build_dir, self.cache(generator)))

    def test_a_missing_build_file_or_program_stays_with_cmake_build(self) -> None:
        self.assertIsNone(direct_build_tool(self.build_dir, self.cache("Ninja")))
        (self.build_dir / "build.ninja").touch()
        missing = {"CMAKE_GENERATOR": "Ninja", "CMAKE_MAKE_PROGRAM": str(self.build_dir / "missing")}
        self.assertIsNone(direct_build_tool(self.build_dir, missing))
        self.assertIsNone(direct_build_tool(self.build_dir, {"CMAKE_GENERATOR": "Ninja"}))

    def test_jobs_follow_what_cmake_build_would_pass(self) -> None:
        ninja, make = BuildTool(ToolKind.NINJA, "ninja"), BuildTool(ToolKind.MAKE, "make")
        self.assertIsNone(direct_jobs(ninja, {}, 8))
        self.assertEqual(direct_jobs(make, {}, 8), 8)
        self.assertIsNone(direct_jobs(make, {"MAKEFLAGS": "-j3"}, 8))
        self.assertEqual(direct_jobs(ninja, {"CMAKE_BUILD_PARALLEL_LEVEL": "3"}, 8), 3)
        self.assertEqual(direct_jobs(make, {"CMAKE_BUILD_PARALLEL_LEVEL": ""}, 8), 8)

    def test_the_direct_command(self) -> None:
        self.assertEqual(
            build_command(Path("b"), "t", 4, BuildTool(ToolKind.MAKE, "/usr/bin/make")),
            ["/usr/bin/make", "-C", "b", "-j", "4", "t"],
        )
        self.assertEqual(build_command(Path("b"), tool=BuildTool(ToolKind.NINJA, "ninja")), ["ninja", "-C", "b"])


def found(*programs: str):
    return lambda name: f"/usr/bin/{name}" if name in programs else None


class LinkerAdviceTest(unittest.TestCase):
    def test_an_installed_fast_linker_is_recommended(self) -> None:
        advice = linker_advice({}, found("mold", "ld.lld"), "linux")
        assert advice is not None
        self.assertIn("-DCMAKE_LINKER_TYPE=MOLD", advice)
        self.assertIn("-fuse-ld=mold", advice)
        lld = linker_advice({}, found("ld.lld"), "linux")
        assert lld is not None
        self.assertIn("-DCMAKE_LINKER_TYPE=LLD", lld)

    def test_without_a_fast_linker_installing_one_is_recommended(self) -> None:
        advice = linker_advice({}, found(), "linux")
        assert advice is not None
        self.assertIn("install mold or lld", advice)

    def test_a_build_that_already_links_fast_gets_no_advice(self) -> None:
        for cache in (
            {"CMAKE_LINKER_TYPE": "MOLD"},
            {"CMAKE_LINKER_TYPE": "lld"},
            {"CMAKE_EXE_LINKER_FLAGS": "--coverage -fuse-ld=mold"},
            {"CMAKE_BUILD_TYPE": "Debug", "CMAKE_EXE_LINKER_FLAGS_DEBUG": "-fuse-ld=lld"},
            {"CMAKE_CXX_FLAGS": "-fuse-ld=/opt/mold/bin/ld.mold"},
        ):
            with self.subTest(cache=cache):
                self.assertIsNone(linker_advice(cache, found("mold"), "linux"))

    def test_ld_that_is_mold_needs_no_advice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mold = Path(tmp) / "mold"
            mold.touch()
            ld = Path(tmp) / "ld"
            ld.symlink_to(mold)
            self.assertIsNone(linker_advice({"CMAKE_LINKER": str(ld)}, found("mold"), "linux"))

    def test_gold_is_not_fast_enough_to_skip_the_advice(self) -> None:
        self.assertIsNotNone(linker_advice({"CMAKE_EXE_LINKER_FLAGS": "-fuse-ld=gold"}, found("mold"), "linux"))

    def test_platforms_with_fast_default_linkers_get_no_advice(self) -> None:
        self.assertIsNone(linker_advice({}, found("mold"), "darwin"))
        self.assertIsNone(linker_advice({}, found("mold"), "win32"))


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
        self.assertEqual(artifacts[self.build_dir / "sample_tests"].name, "sample_tests")
        self.assertEqual(artifacts[self.build_dir / "other_tests"].name, "other_tests")

    def test_targets_list_their_own_sources(self) -> None:
        artifacts = target_artifacts(self.build_dir)
        self.assertEqual(
            artifacts[self.build_dir / "sample_tests"].sources, {(FIXTURE / "tests" / "sample_tests.cpp").resolve()}
        )

    def test_building_a_target_leaves_other_targets_alone(self) -> None:
        self.assertTrue(build(self.build_dir, "sample_tests"))
        self.assertTrue((self.build_dir / "sample_tests").is_file())
        self.assertFalse((self.build_dir / "other_tests").exists())

    def test_the_generated_build_tool_builds_a_target_directly(self) -> None:
        tool = direct_build_tool(self.build_dir, read_cmake_cache(self.build_dir))
        assert tool is not None
        self.assertTrue(build(self.build_dir, "other_tests", tool=tool))
        self.assertTrue((self.build_dir / "other_tests").is_file())
        self.assertFalse((self.build_dir / "sample_tests").exists())


if __name__ == "__main__":
    unittest.main()
