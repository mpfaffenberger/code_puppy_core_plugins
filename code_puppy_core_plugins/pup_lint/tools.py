"""Pydantic-AI tool registration for Pup Lint."""

from __future__ import annotations

from typing import Any

from pydantic_ai import RunContext

from .runner import lint_paths


def register_pup_lint(agent):
    @agent.tool
    async def pup_lint(
        context: RunContext,
        paths: list[str],
        cwd: str | None = None,
        line_length: int | None = None,
        select: list[str] | None = None,
        ignore: list[str] | None = None,
    ) -> dict[str, Any]:
        """Lint Python files with the zero-Rust Pup Lint analyzer.

        Use after creating or changing Python files and before reporting the
        coding task complete. Paths are interpreted relative to cwd, which
        defaults to the current project. This tool diagnoses only and never
        modifies files. Use Code Puppy's file tools to address diagnostics.
        """
        del context
        return await lint_paths(
            paths,
            cwd=cwd,
            line_length=line_length,
            select=select,
            ignore=ignore,
        )

    return pup_lint
