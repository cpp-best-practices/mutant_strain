import unittest
from pathlib import Path

from strain.build import CompileCommand, CompilationDatabase

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


if __name__ == "__main__":
    unittest.main()
