"""The ``smart_grep`` agent tool."""

from __future__ import annotations

from pathlib import Path

from pydantic_ai import RunContext

from code_puppy.i18n import add_catalog_dir, t
from code_puppy.messaging import emit_info
from code_puppy.tools.common import resolve_path

from .config import (
    API_KEY_NAME,
    API_KEY_NAMES,
    ENABLED_KEY,
    get_jev_model_name,
    get_threshold,
    get_typesafe_api_key,
    is_enabled,
)
from .judge import build_jev_model
from .search import DEFAULT_CANDIDATES, SemanticGrepOutput, semantic_search

add_catalog_dir(Path(__file__).parent / "locales")

TOOL_NAME = "smart_grep"


def _first_error(exc: BaseException) -> BaseException:
    """TaskGroup wraps failures in ExceptionGroups; surface the real one."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


async def run_smart_grep(
    query: str,
    directory: str = ".",
    glob: str | None = None,
    limit: int = 5,
    candidates: int = DEFAULT_CANDIDATES,
) -> SemanticGrepOutput:
    # Re-checked per call: an agent built before `/set smart_grep off` must
    # not keep sending source out.
    if not is_enabled():
        return SemanticGrepOutput(
            error=f"smart_grep is off. Enable it with `/set {ENABLED_KEY} on`."
        )
    api_key = get_typesafe_api_key()
    if not api_key:
        return SemanticGrepOutput(
            error=(
                f"No Jev API key: set {' or '.join(API_KEY_NAMES)} "
                f"(`/set {API_KEY_NAME} <key>` or export it)."
            )
        )
    directory = resolve_path(directory)
    try:
        result = await semantic_search(
            build_jev_model(get_jev_model_name(), api_key),
            query,
            directory,
            glob=glob,
            limit=max(1, min(limit, 100)),
            candidates=candidates,
            threshold=get_threshold(),
        )
    except Exception as exc:  # noqa: BLE001 - report to the model, never crash the turn
        err = _first_error(exc)
        return SemanticGrepOutput(
            error=f"smart_grep failed: {type(err).__name__}: {err}"
        )
    emit_info(
        t(
            "smart_grep.summary",
            query=query,
            directory=directory,
            matches=len(result.matches),
            evaluated=result.coverage.evaluated,
            snippets=result.coverage.snippets,
        )
    )
    return result


def register_smart_grep(agent):
    """Register the smart_grep tool on an agent."""

    # Read-only discovery: eligible for speculative early launch. Safe because
    # run_smart_grep re-checks the opt-in flag on every call.
    @agent.tool(metadata={"speculatable": True})
    async def smart_grep(
        context: RunContext,
        query: str,
        directory: str = ".",
        glob: str | None = None,
        limit: int = 5,
        candidates: int = DEFAULT_CANDIDATES,
    ) -> SemanticGrepOutput:
        """Find code by what it DOES, described in plain English (semantic search).

        Use for behaviour-based discovery when you don't know the symbol names,
        e.g. "where do we reject expired sessions?" or "retry a failed network
        request". Keep using `grep` for exact symbols, regexes and exhaustive
        reference lists -- this tool ranks, it does not enumerate.

        A local lexical shortlist of `candidates` snippets is judged by a
        relevance model; matches come back ranked with file/line ranges and a
        short excerpt. An empty result does not prove absence. Tests are
        labelled kind="test", not demoted.

        Args:
            query: Plain-English description of the behaviour to find.
            directory: Root to search (respects .gitignore; skips hidden files).
            glob: Optional ripgrep glob to restrict files, e.g. "*.py".
            limit: Max matches to return (1-100).
            candidates: Snippets to judge after the lexical shortlist (1-256).
                Defaults to 128. Higher = better recall, slower and costlier.
        """
        return await run_smart_grep(query, directory, glob, limit, candidates)
