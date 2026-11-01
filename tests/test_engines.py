import unittest
from unittest import mock

from strain import cppcheck_engine, tree_sitter_engine
from strain.mutators import Engine, apply_mutant, generate_mutants, resolve_engine
from strain.operator_sites import EngineError, OperatorSite


class EngineCases:
    """Behavior every mutation engine shares; each subclass runs it against one engine."""

    engine: Engine

    def summary(self, source: str) -> list[tuple[int, str, str]]:
        return [
            (mutant.line, mutant.original, mutant.replacement)
            for mutant in generate_mutants("f.cpp", source, engine=self.engine)
        ]

    def test_binary_operators_are_mutated(self) -> None:
        source = "bool f(int a, int b, int c) {\n  return a + b < c && a % b == 0;\n}\n"
        self.assertEqual(
            self.summary(source),
            [(2, "+", "-"), (2, "<", "<="), (2, "<", ">="), (2, "&&", "||"), (2, "%", "*"), (2, "==", "!=")],
        )

    def test_compound_assignments_are_mutated(self) -> None:
        self.assertEqual(self.summary("void f(int a) {\n  a += 1;\n  a /= 2;\n}\n"), [(2, "+=", "-="), (3, "/=", "*=")])

    def test_unary_operators_are_not_mutated(self) -> None:
        self.assertEqual(self.summary("int f(int x, int *p) {\n  return -x + *p;\n}\n"), [(2, "+", "-")])

    def test_template_brackets_are_not_mutated(self) -> None:
        source = (
            "#include <vector>\n"
            "template <typename T> T g(T t) { return t; }\n"
            "void f(double x) {\n"
            "  std::vector<std::pair<int, long>> v;\n"
            "  auto n = static_cast<int>(x);\n"
            "}\n"
        )
        self.assertEqual(self.summary(source), [])

    def test_comments_strings_and_directives_are_not_mutated(self) -> None:
        source = (
            "#define ADD(a, b) ((a) + (b))\n"
            "#if VERSION > 1\n"
            "#endif\n"
            "const char *f() {\n"
            "  return \"a < b\"; // x - y\n"
            "}\n"
        )
        self.assertEqual(self.summary(source), [])

    def test_locations_count_characters(self) -> None:
        source = "int f(int a, int b) {\n\tauto s = \"é\"; return a == b;\n}\n"
        mutant = generate_mutants("f.cpp", source, engine=self.engine)[0]
        self.assertEqual((mutant.line, mutant.column, mutant.offset), (2, 25, source.index("==")))
        self.assertEqual(apply_mutant(source, mutant), source.replace("==", "!="))

    def test_line_filter_and_max_per_line(self) -> None:
        source = "int f(int a, int b) {\n  int c = a < b;\n  return a - c;\n}\n"
        mutants = generate_mutants("f.cpp", source, lines={2}, max_per_line=1, engine=self.engine)
        self.assertEqual([(mutant.line, mutant.replacement) for mutant in mutants], [(2, "<=")])


class LexerEngineTest(EngineCases, unittest.TestCase):
    engine = Engine.LEXER


class DeclaratorCases(EngineCases):
    """Engines that parse C++ know that `*` and `&&` in a declaration are not operators."""

    def test_declarators_are_not_mutated(self) -> None:
        source = (
            "using Value = int;\n"
            "struct Foo {};\n"
            "Value *first(Value *values) {\n"
            "  Foo *p = nullptr;\n"
            "  Foo &&r = Foo{};\n"
            "  return values;\n"
            "}\n"
        )
        self.assertEqual(self.summary(source), [])


@unittest.skipUnless(tree_sitter_engine.is_available(), "needs the tree-sitter and tree-sitter-cpp packages")
class TreeSitterEngineTest(DeclaratorCases, unittest.TestCase):
    engine = Engine.TREE_SITTER


@unittest.skipUnless(cppcheck_engine.is_available(), "needs cppcheck on PATH")
class CppcheckEngineTest(DeclaratorCases, unittest.TestCase):
    engine = Engine.CPPCHECK


DUMP = """<?xml version="1.0"?>
<dumps language="cpp">
<dump cfg="">
  <rawtokens>
    <file index="0" name="/tmp/source.cpp"/>
    <file index="1" name="/tmp/header.hpp"/>
  </rawtokens>
  <tokenlist>
    <token id="1" file="/tmp/header.hpp" linenr="1" column="10" str="+" astOperand1="9" astOperand2="9"/>
    <token id="2" file="/tmp/source.cpp" linenr="1" column="11" str="*" astOperand1="9" astOperand2="3"/>
    <token id="3" file="/tmp/source.cpp" linenr="1" column="12" str="p"/>
    <token id="4" file="/tmp/source.cpp" linenr="2" column="11" str="-" astOperand1="9"/>
    <token id="5" file="/tmp/source.cpp" linenr="2" column="14" str="+" macroName="ADD" astOperand1="9" astOperand2="9"/>
    <token id="6" file="/tmp/source.cpp" linenr="2" column="23" str="&lt;" astOperand1="9" astOperand2="9"/>
    <token id="7" file="/tmp/source.cpp" linenr="2" column="10" str="+" astOperand1="9" astOperand2="9"/>
  </tokenlist>
  <scopes>
    <scope id="8">
      <varlist>
        <var id="10"/>
      </varlist>
    </scope>
  </scopes>
  <variables>
    <var id="10" nameToken="3" typeStartToken="9" typeEndToken="2"/>
  </variables>
</dump>
<dump cfg="X">
  <rawtokens>
    <file index="0" name="/tmp/source.cpp"/>
  </rawtokens>
  <tokenlist>
    <token id="11" fileIndex="0" linenr="2" column="23" str="&lt;" astOperand1="9" astOperand2="9"/>
    <token id="12" fileIndex="0" linenr="3" column="3" str="==" astOperand1="9" astOperand2="9"/>
  </tokenlist>
</dump>
</dumps>
"""
DUMP_SOURCE = "int f(Foo *p);\nint x = a+-b + ADD(a) < c;\nb == c;\n"


class CppcheckDumpTest(unittest.TestCase):
    def test_binary_operators_of_the_main_file_without_declarators_or_macros(self) -> None:
        self.assertEqual(
            cppcheck_engine.sites_from_dump(DUMP, DUMP_SOURCE),
            [
                OperatorSite("+", 2, 10, DUMP_SOURCE.index("+")),
                OperatorSite("<", 2, 23, DUMP_SOURCE.index("<")),
                OperatorSite("==", 3, 3, DUMP_SOURCE.index("==")),
            ],
        )

    def test_tokens_that_do_not_match_the_source_are_dropped(self) -> None:
        self.assertEqual(
            cppcheck_engine.sites_from_dump(DUMP, DUMP_SOURCE.replace("==", "!=")),
            [OperatorSite("+", 2, 10, DUMP_SOURCE.index("+")), OperatorSite("<", 2, 23, DUMP_SOURCE.index("<"))],
        )


class ResolveEngineTest(unittest.TestCase):
    def available(self, cppcheck: bool, tree_sitter: bool) -> None:
        for module, available in ((cppcheck_engine, cppcheck), (tree_sitter_engine, tree_sitter)):
            patcher = mock.patch.object(module, "is_available", return_value=available)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_auto_prefers_cppcheck(self) -> None:
        self.available(cppcheck=True, tree_sitter=True)
        self.assertIs(resolve_engine(Engine.AUTO), Engine.CPPCHECK)

    def test_auto_falls_back_to_tree_sitter(self) -> None:
        self.available(cppcheck=False, tree_sitter=True)
        self.assertIs(resolve_engine(Engine.AUTO), Engine.TREE_SITTER)

    def test_auto_falls_back_to_the_lexer(self) -> None:
        self.available(cppcheck=False, tree_sitter=False)
        self.assertIs(resolve_engine(Engine.AUTO), Engine.LEXER)

    def test_requested_engine_must_be_available(self) -> None:
        self.available(cppcheck=False, tree_sitter=False)
        for engine in (Engine.CPPCHECK, Engine.TREE_SITTER):
            with self.subTest(engine=engine), self.assertRaises(EngineError):
                resolve_engine(engine)
        self.assertIs(resolve_engine(Engine.LEXER), Engine.LEXER)


if __name__ == "__main__":
    unittest.main()
