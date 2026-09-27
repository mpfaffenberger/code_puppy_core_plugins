"""Language-agnostic structural chunking: declarations -> line ranges.

One algorithm for every parser. A language plugs in by implementing
:class:`Syntax` (Python via stdlib ``ast`` here, others via tree-sitter in
``treesitter.py``):

* containers (classes, impls, modules) are split into a header plus one
  range per member, named ``Container.member``;
* declarations of ``SPLIT_LINES`` or more are split into the blocks inside
  them, so each range is one behaviour. Splits cover every line of the
  original range, so coverage is unchanged;
* runs of short neighbours (imports, one-liners) merge into one range,
  since each range costs a relevance judgment;
* a node the parser could not read cleanly is not split or trusted: it
  becomes a ``window`` range, cut into overlapping line windows like an
  unparsed file. One bad macro costs its own region, not the whole file.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

SPLIT_LINES = 24
_MAX_DEPTH = 3


@dataclass
class Range:
    start: int
    end: int
    symbol: str | None
    window: bool = False  # parse error inside: use line windows, not one blob


class Syntax(Protocol):
    """What the chunker needs to know about one language's tree."""

    def span(self, node: Any) -> tuple[int, int]:
        """1-based first and last line, including decorators/wrappers."""

    def symbol(self, node: Any) -> str | None:
        """The declared name, if any."""

    def members(self, node: Any) -> Sequence[Any] | None:
        """Member declarations when ``node`` is a container, else None."""

    def statements(self, node: Any) -> Sequence[Any] | None:
        """The statements of ``node``'s body, if it has one."""

    def broken(self, node: Any) -> bool:
        """True when the parser could not read ``node`` cleanly."""


def partition(node: Any, start: int, end: int, symbol, syntax: Syntax, depth=0):
    """Split a long body into its blocks, covering every line of [start, end]."""
    body = syntax.statements(node)
    if not body or end - start < SPLIT_LINES:
        return None
    if len(body) == 1:
        if depth >= _MAX_DEPTH:
            return None
        return partition(body[0], start, end, symbol, syntax, depth + 1)

    out: list[Range] = []
    cursor = start
    group: list[Any] = []

    def flush(last: bool) -> None:
        nonlocal cursor
        if not group:
            return
        stop = end if last else min(end, syntax.span(group[-1])[1])
        if stop >= cursor:
            inner = None
            if len(group) == 1 and depth < _MAX_DEPTH:
                inner = partition(group[0], cursor, stop, symbol, syntax, depth + 1)
            out.extend(inner or [Range(cursor, stop, symbol)])
            cursor = stop + 1
        group.clear()

    for index, stmt in enumerate(body):
        first, last = syntax.span(stmt)
        span = last - first + 1
        if group and (span >= 6 or last - cursor + 1 > 20):
            flush(False)
        group.append(stmt)
        if span >= 6 and index != len(body) - 1:
            flush(False)
    flush(True)
    return out if len(out) > 1 else None


def structure_ranges(nodes: Sequence[Any], syntax: Syntax) -> list[Range]:
    """Ranges for a file's top-level nodes."""
    ranges: list[Range] = []

    def walk(node: Any, prefix: str | None) -> None:
        start, end = syntax.span(node)
        name = syntax.symbol(node)
        symbol = f"{prefix}.{name or 'body'}" if prefix else name
        members = syntax.members(node)
        if members:
            header_end = syntax.span(members[0])[0] - 1
            if header_end >= start:
                ranges.append(Range(start, header_end, symbol))
            for member in members:
                walk(member, symbol or "body")
            # The container's tail (a closing brace) joins its last member
            # rather than becoming a judgment-costing snippet of its own.
            if ranges and ranges[-1].end < end:
                ranges[-1].end = end
            return
        if syntax.broken(node):
            ranges.append(Range(start, end, symbol, window=True))
            return
        ranges.extend(
            partition(node, start, end, symbol, syntax) or [Range(start, end, symbol)]
        )

    for node in nodes:
        walk(node, None)
    return merge_short_neighbours(ranges)


def merge_short_neighbours(ranges: list[Range]) -> list[Range]:
    """Fold runs of imports/one-liners into one target (each costs a judgment).

    Members (dotted symbols) never merge, so a method is never folded into its
    container's header or a neighbour.
    """

    def mergeable(r: Range) -> bool:
        return r.end - r.start <= 1 and "." not in (r.symbol or "")

    merged: list[Range] = []
    for r in ranges:
        last = merged[-1] if merged else None
        if (
            last
            and mergeable(last)
            and mergeable(r)
            and r.start <= last.end + 1
            and r.end - last.start < 12
            and len(f"{last.symbol} {r.symbol}") < 60
        ):
            names = dict.fromkeys(s for s in (last.symbol, r.symbol) if s)
            last.end, last.symbol = r.end, ", ".join(names) or None
        else:
            merged.append(Range(r.start, r.end, r.symbol, r.window))
    return merged


class PythonSyntax:
    """:class:`Syntax` over the standard library ``ast``."""

    def span(self, node: ast.AST) -> tuple[int, int]:
        decorators = getattr(node, "decorator_list", [])
        return min([node.lineno, *(d.lineno for d in decorators)]), node.end_lineno

    def symbol(self, node: ast.AST) -> str | None:
        return getattr(node, "name", None)

    def members(self, node: ast.AST) -> Sequence[ast.AST] | None:
        return node.body if isinstance(node, ast.ClassDef) else None

    def statements(self, node: ast.AST) -> Sequence[ast.AST] | None:
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            return None
        return [s for s in body if hasattr(s, "end_lineno")]

    def broken(self, node: ast.AST) -> bool:
        return False  # ast.parse either succeeds for the whole file or raises


def python_ranges(text: str) -> list[Range]:
    """Raises SyntaxError/ValueError/RecursionError on unparseable source."""
    return structure_ranges(ast.parse(text).body, PythonSyntax())
