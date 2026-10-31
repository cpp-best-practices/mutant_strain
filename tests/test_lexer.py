import unittest

from strain.lexer import TokenKind, tokenize


def code_texts(source: str) -> list[str]:
    return [token.text for token in tokenize(source) if token.kind is not TokenKind.COMMENT]


class TokenizeTest(unittest.TestCase):
    def test_operators_use_maximal_munch(self) -> None:
        self.assertEqual(code_texts("a<<=b<=c<d"), ["a", "<<=", "b", "<=", "c", "<", "d"])

    def test_positions_are_one_based(self) -> None:
        tokens = tokenize("int x;\n  y == z;")
        equals = next(token for token in tokens if token.text == "==")
        self.assertEqual((equals.line, equals.column, equals.offset), (2, 5, 11))

    def test_comments_are_comment_tokens(self) -> None:
        tokens = tokenize("a // x < y\n/* p && q */ b")
        self.assertEqual(
            [(token.kind, token.text) for token in tokens],
            [
                (TokenKind.IDENTIFIER, "a"),
                (TokenKind.COMMENT, "// x < y"),
                (TokenKind.COMMENT, "/* p && q */"),
                (TokenKind.IDENTIFIER, "b"),
            ],
        )

    def test_string_and_char_literals_hide_operators(self) -> None:
        source = 'f("a < \\"b\\"", \'<\')'
        self.assertEqual(code_texts(source), ["f", "(", '"a < \\"b\\""', ",", "'<'", ")"])

    def test_raw_string_literal(self) -> None:
        self.assertEqual(code_texts('x = R"tag(a < ")" > b)tag";'), ["x", "=", 'R"tag(a < ")" > b)tag"', ";"])

    def test_prefixed_literals(self) -> None:
        self.assertEqual(code_texts("u8\"a+b\" L'+'"), ['u8"a+b"', "L'+'"])

    def test_preprocessor_lines_are_skipped(self) -> None:
        self.assertEqual(code_texts("#include <vector>\n#define F(a) \\\n  a + 1\nx + y"), ["x", "+", "y"])

    def test_numbers_include_exponent_signs_and_separators(self) -> None:
        self.assertEqual(code_texts("1e-5 + 0x1p+3 - 1'000"), ["1e-5", "+", "0x1p+3", "-", "1'000"])

    def test_token_kinds(self) -> None:
        kinds = [token.kind for token in tokenize("a 1 \"s\" 'c' +")]
        self.assertEqual(
            kinds,
            [TokenKind.IDENTIFIER, TokenKind.NUMBER, TokenKind.STRING, TokenKind.CHAR, TokenKind.OPERATOR],
        )


if __name__ == "__main__":
    unittest.main()
