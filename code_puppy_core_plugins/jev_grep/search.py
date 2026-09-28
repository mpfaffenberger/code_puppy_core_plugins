"""Semantic search pipeline: discover -> shortlist -> judge -> rank -> excerpt."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel
from pydantic_ai.models import Model

from .chunks import Chunk, discover
from .judge import judge
from .retrieve import rank, terms

MAX_QUERY_CHARS = 2000
DEFAULT_CANDIDATES = 128
MAX_CANDIDATES = 256
EXCERPT_LINES = 12
EXCERPT_CHARS = 1200
_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|[._-](test|spec)\.[^/]+$|_test\.py$"
)


class SemanticMatch(BaseModel):
    file_path: str
    start_line: int
    end_line: int
    symbol: str | None = None
    kind: Literal["test"] | None = None
    score: float
    text: str
    snippet_start_line: int
    snippet_end_line: int


class Coverage(BaseModel):
    files: int = 0
    snippets: int = 0
    evaluated: int = 0
    skipped_files: int = 0
    selection_complete: bool = False


class SemanticGrepOutput(BaseModel):
    matches: list[SemanticMatch] = []
    coverage: Coverage = Coverage()
    omitted_matches: int = 0
    warnings: list[str] = []
    error: str | None = None


@dataclass(frozen=True)
class _Scored:
    chunk: Chunk
    score: float


def _overlaps(a: Chunk, b: Chunk) -> bool:
    return a.path == b.path and a.line <= b.end_line and b.line <= a.end_line


def _dedupe(passing: list[_Scored]) -> list[_Scored]:
    """Best-scoring window wins; overlapping lower-ranked windows collapse into it."""
    kept: list[_Scored] = []
    for item in passing:
        if not any(_overlaps(k.chunk, item.chunk) for k in kept):
            kept.append(item)
    return kept


def _evidence(match: Chunk, passing: list[_Scored]) -> Chunk | None:
    """A narrower judged block inside ``match`` that also passed, if any."""
    span = match.end_line - match.line
    inside = [
        p
        for p in passing
        if p.chunk.path == match.path
        and p.chunk.line >= match.line
        and p.chunk.end_line <= match.end_line
        and p.chunk.end_line - p.chunk.line < span
    ]
    if not inside:
        return None
    best = min(inside, key=lambda p: (-p.score, p.chunk.end_line - p.chunk.line))
    return best.chunk


def _excerpt(chunk: Chunk, focus: Chunk | None, query: str) -> tuple[int, str]:
    """(first_line, text) of at most EXCERPT_LINES lines / EXCERPT_CHARS chars."""
    lines = chunk.text.splitlines()
    if focus is not None:
        offset = focus.line - chunk.line
        lines, first = (
            lines[offset : offset + focus.end_line - focus.line + 1],
            focus.line,
        )
    else:
        first = chunk.line
    if len(lines) > EXCERPT_LINES:
        wanted = set(terms(query))
        hits = [sum(t in wanted for t in terms(line)) for line in lines]
        best = max(
            range(len(lines) - EXCERPT_LINES + 1),
            key=lambda i: (sum(hits[i : i + EXCERPT_LINES]), -i),
        )
        lines, first = lines[best : best + EXCERPT_LINES], first + best
    while len(lines) > 1 and len("\n".join(lines)) > EXCERPT_CHARS:
        lines = lines[:-1]
    return first, "\n".join(lines)[:EXCERPT_CHARS]


def _to_match(item: _Scored, passing: list[_Scored], query: str) -> SemanticMatch:
    chunk = item.chunk
    first, text = _excerpt(chunk, _evidence(chunk, passing), query)
    return SemanticMatch(
        file_path=chunk.path,
        start_line=first,
        end_line=first + max(text.count("\n"), 0),
        symbol=chunk.symbol,
        kind="test" if _TEST_PATH.search(chunk.path.replace("\\", "/")) else None,
        score=round(item.score, 3),
        text=text,
        snippet_start_line=chunk.line,
        snippet_end_line=chunk.end_line,
    )


async def semantic_search(
    model: Model,
    query: str,
    directory: str,
    *,
    glob: str | None = None,
    limit: int = 5,
    candidates: int = DEFAULT_CANDIDATES,
    threshold: float = 0.5,
) -> SemanticGrepOutput:
    query = query.strip()
    if not query or len(query) > MAX_QUERY_CHARS:
        return SemanticGrepOutput(
            error=f"Query must contain 1-{MAX_QUERY_CHARS} characters."
        )
    candidates = max(1, min(candidates, MAX_CANDIDATES))

    found = await asyncio.to_thread(discover, directory, glob)
    selected = rank(query, found.chunks)[:candidates]
    coverage = Coverage(
        files=found.files,
        snippets=len(found.chunks),
        evaluated=len(selected),
        skipped_files=len(found.skipped),
        selection_complete=len(selected) == len(found.chunks),
    )

    scores = await judge(model, query, selected) if selected else []
    passing = sorted(
        (_Scored(c, s) for c, s in zip(selected, scores) if s >= threshold),
        key=lambda p: (-p.score, p.chunk.path, p.chunk.line),
    )
    kept = _dedupe(passing)

    warnings: list[str] = []
    if selected and not kept:
        warnings.append(
            f"No snippet reached threshold {threshold}; this does not prove absence. "
            "Try rephrasing, a narrower directory, or regular grep."
        )
    if not coverage.selection_complete:
        warnings.append(
            f"Judged {len(selected)} of {len(found.chunks)} snippets picked by a lexical "
            "shortlist; code with unrelated wording may be missed. Raise `candidates` "
            "or narrow the directory/glob."
        )
    if found.skipped:
        warnings.append(
            f"{len(found.skipped)} files skipped (binary, non-UTF-8, >1 MiB or minified)."
        )

    return SemanticGrepOutput(
        matches=[_to_match(item, passing, query) for item in kept[:limit]],
        coverage=coverage,
        omitted_matches=max(len(kept) - limit, 0),
        warnings=warnings,
    )
