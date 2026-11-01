"""The `tree-sitter` engine: operators of the binary and assignment expressions in a C++ parse tree."""

import functools
import importlib.util
from collections.abc import Iterator
from typing import Any

from .operator_sites import SOURCE_ENCODING, SOURCE_ERRORS, EngineError, LineIndex, OperatorSite

MODULES = ("tree_sitter", "tree_sitter_cpp")
MISSING = "the tree-sitter engine needs the tree-sitter and tree-sitter-cpp packages: pip install \"strain[tree-sitter]\""
OPERATOR_EXPRESSIONS = frozenset({"binary_expression", "assignment_expression"})
# The condition of `#if X > 1` parses as an expression, but the preprocessor evaluates it, not the compiled code.
PREPROCESSOR_CONDITIONALS = frozenset({"preproc_if", "preproc_elif"})


def is_available() -> bool:
    return all(importlib.util.find_spec(module) is not None for module in MODULES)


@functools.cache
def _parser() -> Any:
    try:
        import tree_sitter
        import tree_sitter_cpp
    except ImportError as error:
        raise EngineError(MISSING) from error
    return tree_sitter.Parser(tree_sitter.Language(tree_sitter_cpp.language()))


def _compiled_children(node: Any) -> Iterator[Any]:
    for index, child in enumerate(node.children):
        if node.type not in PREPROCESSOR_CONDITIONALS or node.field_name_for_child(index) != "condition":
            yield child


def _operator_nodes(root: Any) -> Iterator[Any]:
    pending = [root]
    while pending:
        node = pending.pop()
        if node.type in OPERATOR_EXPRESSIONS:
            operator = node.child_by_field_name("operator")
            if operator is not None:
                yield operator
        pending.extend(_compiled_children(node))


def operator_sites(source: str) -> list[OperatorSite]:
    index = LineIndex(source)
    sites: list[OperatorSite] = []
    for operator in _operator_nodes(_parser().parse(source.encode(SOURCE_ENCODING, SOURCE_ERRORS)).root_node):
        row, byte_column = operator.start_point
        text = operator.text.decode(SOURCE_ENCODING, SOURCE_ERRORS)
        site = index.site_at_byte_column(text, row + 1, byte_column + 1)
        if site is not None:
            sites.append(site)
    return sorted(sites, key=lambda site: site.offset)
