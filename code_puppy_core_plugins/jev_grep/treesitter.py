"""tree-sitter :class:`~.structure.Syntax` for JavaScript/TypeScript, Go, Rust
and Java.

Grammars are pinned, offline wheels (one package per language); nothing is
downloaded at runtime. They are optional at runtime: if tree-sitter or a
grammar cannot be imported (e.g. an unsupported platform), the installed
tree-sitter is a known-bad version, or a file does not parse cleanly,
:func:`treesitter_ranges` returns ``None`` and the caller falls back to line
windows.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from importlib.metadata import PackageNotFoundError, version
from functools import cache
from typing import Any

from .structure import Range, structure_ranges


def _javascript():
    import tree_sitter_javascript

    return tree_sitter_javascript.language()


def _typescript():
    import tree_sitter_typescript

    return tree_sitter_typescript.language_typescript()


def _tsx():
    import tree_sitter_typescript

    return tree_sitter_typescript.language_tsx()


def _go():
    import tree_sitter_go

    return tree_sitter_go.language()


def _rust():
    import tree_sitter_rust

    return tree_sitter_rust.language()


def _java():
    import tree_sitter_java

    return tree_sitter_java.language()


# extension -> (parser name, grammar loader)
LANGUAGES: dict[str, tuple[str, Callable[[], Any]]] = {
    **dict.fromkeys((".js", ".jsx", ".mjs", ".cjs"), ("javascript", _javascript)),
    **dict.fromkeys((".ts", ".mts", ".cts"), ("typescript", _typescript)),
    ".tsx": ("tsx", _tsx),
    ".go": ("go", _go),
    ".rs": ("rust", _rust),
    ".java": ("java", _java),
}

# Declarations split into a header plus one range per member. Interfaces and
# structs stay whole: their members are one-line signatures/fields, and a
# range per line would cost a judgment each for no behaviour.
_CONTAINERS = frozenset(
    {
        "class_declaration",
        "abstract_class_declaration",
        "class",
        "enum_declaration",
        "record_declaration",
        "impl_item",
        "trait_item",
        "mod_item",
    }
)
_BLOCKS = frozenset(
    {
        "statement_block",
        "block",
        "class_body",
        "declaration_list",
        "enum_body",
        "switch_body",
    }
)
_WRAPPERS = frozenset({"export_statement"})
# Never named: an imported name would earn BM25's symbol boost for a line
# that is almost never the answer (Python's ast leaves imports unnamed too).
_UNNAMED = frozenset({"import_declaration", "package_declaration", "use_declaration"})
_DECLARATORS = frozenset({"lexical_declaration", "variable_declaration"})
_BODY_SEARCH_DEPTH = 4

# py-tree-sitter 0.26.0 segfaults walking ordinary trees (reproduced on
# CPython 3.13 and 3.14; 0.25.2 is clean on the same input). A native crash
# would kill the whole session, so refuse it even if something pins it.
_BROKEN_TREE_SITTER = ((0, 26),)


def _tree_sitter_is_safe() -> bool:
    try:
        major, minor = (int(p) for p in version("tree-sitter").split(".")[:2])
    except (PackageNotFoundError, ValueError):
        return False
    return (major, minor) not in _BROKEN_TREE_SITTER


def _code_children(node: Any) -> list[Any]:
    """Named children minus comments. A skipped leading comment is absorbed
    into the declaration after it (as Python's ``ast``, which has no comment
    nodes, already behaves)."""
    return [c for c in node.named_children if "comment" not in c.type]


class TreeSitterSyntax:
    """:class:`~.structure.Syntax` over any tree-sitter grammar.

    Node text is sliced from the ``source`` bytes that were parsed, by byte
    offset, so names come from exactly the input we hold.
    """

    def __init__(self, source: bytes) -> None:
        self._source = source

    def _text(self, node: Any) -> str:
        return self._source[node.start_byte : node.end_byte].decode(
            "utf-8", errors="replace"
        )

    @staticmethod
    def _inner(node: Any) -> Any:
        """The declaration an ``export`` wraps (the wrapper keeps the span)."""
        if node.type in _WRAPPERS:
            inner = node.child_by_field_name("declaration")
            if inner is not None:
                return inner
        return node

    def span(self, node: Any) -> tuple[int, int]:
        start = node.start_point.row + 1
        end = node.end_point.row + (1 if node.end_point.column else 0)
        return start, max(start, end)

    def symbol(self, node: Any) -> str | None:
        node = self._inner(node)
        if node.type in _UNNAMED:
            return None
        if node.type == "impl_item":  # `impl Cache` / `impl Trait for Cache`
            target = node.child_by_field_name("type")
            return self._text(target) if target is not None else None
        name = node.child_by_field_name("name")
        if name is None:
            if node.type in _DECLARATORS or node.type.endswith("_declaration"):
                return self._declared_names(node)  # const a, b / Go type / Java field
            return None
        receiver = node.child_by_field_name("receiver")  # Go: (s *Server) Serve
        if receiver is not None:
            owner = self._receiver_type(receiver)
            if owner:
                return f"{owner}.{self._text(name)}"
        return self._text(name)

    def _declared_names(self, node: Any) -> str | None:
        """Names one level down: ``const a, b``, Go ``type_spec``, Java fields."""
        candidates = [node.child_by_field_name("declarator"), *node.named_children]
        names = dict.fromkeys(
            self._text(name)
            for child in candidates
            if child is not None and (name := child.child_by_field_name("name"))
        )
        return ", ".join(names) or None

    def _receiver_type(self, receiver: Any) -> str | None:
        for param in receiver.named_children:
            kind = param.child_by_field_name("type")
            if kind is not None:
                return self._text(kind).lstrip("*&").split("[", 1)[0] or None
        return None

    def members(self, node: Any) -> Sequence[Any] | None:
        node = self._inner(node)
        if node.type not in _CONTAINERS:
            return None
        body = node.child_by_field_name("body")
        members = _code_children(body) if body is not None else []
        return members or None

    def statements(self, node: Any) -> Sequence[Any] | None:
        block = _body_block(self._inner(node))
        if block is None:
            return None
        children = _code_children(block)
        # Go wraps a block's statements in a single statement_list.
        if len(children) == 1 and children[0].type == "statement_list":
            children = _code_children(children[0])
        return children or None


def _body_block(node: Any) -> Any | None:
    """The nearest block under ``node``: its own body, or one inside a wrapper
    (``const f = () => {...}``, ``app.get("/x", (req, res) => {...})``)."""
    if node.type in _BLOCKS:
        return node
    body = node.child_by_field_name("body")
    if body is not None and body.type in _BLOCKS:
        return body
    frontier = list(node.named_children)
    for _ in range(_BODY_SEARCH_DEPTH):
        for child in frontier:
            if child.type in _BLOCKS:
                return child
        frontier = [grand for child in frontier for grand in child.named_children]
    return None


@cache
def _parser(loader: Callable[[], Any]) -> Any | None:
    if not _tree_sitter_is_safe():
        return None
    try:
        from tree_sitter import Language, Parser

        return Parser(Language(loader()))
    except Exception:  # noqa: BLE001 - unavailable grammar => line windows
        return None


def treesitter_ranges(text: str, path: str) -> tuple[list[Range], str] | None:
    """(ranges, parser name), or None when this file should use line windows."""
    entry = LANGUAGES.get(os.path.splitext(path)[1].lower())
    if entry is None:
        return None
    name, loader = entry
    parser = _parser(loader)
    if parser is None:
        return None
    source = text.encode("utf-8")
    root = parser.parse(source).root_node
    if root.has_error:
        return None  # a mis-parsed tree would mis-cut; windows are safe
    ranges = structure_ranges(_code_children(root), TreeSitterSyntax(source))
    return (ranges, name) if ranges else None
