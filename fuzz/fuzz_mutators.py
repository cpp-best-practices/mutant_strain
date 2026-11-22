"""atheris fuzz target: the lexer engine reads any source, and every mutant it finds applies where it says it does.

Run it as `python fuzz/fuzz_mutators.py -max_total_time=60`; any libFuzzer option works.
"""

import sys

import atheris

with atheris.instrument_imports():
    from strain.lexer import tokenize
    from strain.mutators import Engine, apply_mutant, generate_mutants, suppressed_lines


def check_tokens_lie_in_the_source(source: str) -> None:
    for token in tokenize(source):
        if source[token.offset : token.offset + len(token.text)] != token.text:
            raise AssertionError(f"token {token!r} is not at its offset")


def check_mutants_replace_only_their_operator(source: str) -> None:
    for mutant in generate_mutants("fuzz.cpp", source, engine=Engine.LEXER):
        mutated = apply_mutant(source, mutant)
        end = mutant.offset + len(mutant.replacement)
        original_end = mutant.offset + len(mutant.original)
        if mutated[: mutant.offset] != source[: mutant.offset] or mutated[end:] != source[original_end:]:
            raise AssertionError(f"mutant {mutant!r} changed more than its operator")
        if mutated[mutant.offset : end] != mutant.replacement:
            raise AssertionError(f"mutant {mutant!r} did not write its replacement")


def test_one_input(data: bytes) -> None:
    source = atheris.FuzzedDataProvider(data).ConsumeUnicodeNoSurrogates(len(data))
    check_tokens_lie_in_the_source(source)
    check_mutants_replace_only_their_operator(source)
    suppressed_lines(source, ["std::*"])


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
