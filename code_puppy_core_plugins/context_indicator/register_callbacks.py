"""Expose ``/context`` for a detailed token-usage breakdown.

The plugin leaves the bottom-bar token summary and startup output untouched.
"""

from __future__ import annotations

from typing import List, Tuple

from code_puppy.callbacks import register_callback
from code_puppy.token_usage import (
    ContextUsage,
    get_current_usage,
)

_COMMAND_NAME = "context"


# ---------------------------------------------------------------------------
# Messaging helpers (lazy-imported to dodge circular imports at boot)
# ---------------------------------------------------------------------------
def _emit_info(message: str) -> None:
    from code_puppy.messaging import emit_info

    emit_info(message)


# ---------------------------------------------------------------------------
# /context slash command
# ---------------------------------------------------------------------------
def _custom_help() -> List[Tuple[str, str]]:
    return [
        (
            _COMMAND_NAME,
            "Show context-window usage (tokens used vs. model capacity)",
        )
    ]


# Bar glyphs — kept as module constants so the legend stays DRY.
_BAR_GLYPH_OVERHEAD = "▒"  # system prompt + tool schema baseline
_BAR_GLYPH_MESSAGES = "█"  # conversation tokens
_BAR_GLYPH_EMPTY = "░"  # unused capacity
_BAR_GLYPH_THRESHOLD = "┃"  # vertical marker for compaction trigger
_BAR_WIDTH = 30


def _render_usage_bar(usage: ContextUsage, threshold: float) -> str:
    """Render the ASCII usage bar with three zones + a compaction marker.

    Zones: overhead | messages | empty. The compaction-threshold marker
    overwrites whichever cell it lands on (cosmetic; the underlying token
    counts are unchanged).
    """
    capacity = max(1, usage.capacity)
    overhead_cells = min(
        _BAR_WIDTH, int(round(usage.overhead_tokens / capacity * _BAR_WIDTH))
    )
    total_cells = min(
        _BAR_WIDTH, int(round(usage.total_tokens / capacity * _BAR_WIDTH))
    )
    # Guarantee messages cells start strictly after overhead cells.
    message_cells = max(0, total_cells - overhead_cells)
    empty_cells = _BAR_WIDTH - overhead_cells - message_cells

    cells = (
        [_BAR_GLYPH_OVERHEAD] * overhead_cells
        + [_BAR_GLYPH_MESSAGES] * message_cells
        + [_BAR_GLYPH_EMPTY] * empty_cells
    )

    threshold_clamped = max(0.0, min(1.0, threshold))
    marker_idx = min(_BAR_WIDTH - 1, int(round(threshold_clamped * _BAR_WIDTH)))
    cells[marker_idx] = _BAR_GLYPH_THRESHOLD
    return "".join(cells)


def _get_compaction_threshold() -> float:
    """Fetch compaction threshold defensively; fall back to 0.85 on any error."""
    try:
        from code_puppy.config import get_compaction_threshold

        return float(get_compaction_threshold())
    except Exception:
        return 0.85


def _format_overhead_breakdown(usage: ContextUsage) -> str:
    """Render the per-bucket overhead breakdown.

    Each non-zero bucket gets its own indented line. Zero-valued buckets are
    hidden so users with no MCP servers / no AGENTS.md aren't staring at
    noise.  We *always* show the aggregate ``Overhead`` line so the report
    structure stays consistent.
    """
    # (label, token_count) — order matters for readability.
    rows = (
        ("System prompt", usage.system_prompt_tokens),
        ("AGENTS.md     ", usage.agents_md_tokens),
        ("Kennel memory", usage.kennel_memory_tokens),
        ("Pydantic tools", usage.pydantic_tools_tokens),
        ("MCP toolsets  ", usage.mcp_tokens),
    )
    lines = [
        f"    └─ {label}: {tokens:,} tokens" for label, tokens in rows if tokens > 0
    ]
    return "\n".join(lines)


def _format_usage_report(usage: ContextUsage) -> str:
    threshold = _get_compaction_threshold()
    bar = _render_usage_bar(usage, threshold)
    legend = (
        f"  Legend   : {_BAR_GLYPH_OVERHEAD} overhead  "
        f"{_BAR_GLYPH_MESSAGES} messages  "
        f"{_BAR_GLYPH_EMPTY} free  "
        f"{_BAR_GLYPH_THRESHOLD} compaction @ {threshold:.0%}"
    )
    breakdown = _format_overhead_breakdown(usage)
    breakdown_block = f"\n{breakdown}" if breakdown else ""
    return (
        f"Context usage: {usage.percent:.1f}%\n"
        f"  [{bar}]\n"
        f"{legend}\n"
        f"  Messages : {usage.used_tokens:,} tokens\n"
        f"  Overhead : {usage.overhead_tokens:,} tokens (system prompt + AGENTS.md + kennel memory + tools + MCP)"
        f"{breakdown_block}\n"
        f"  Total    : {usage.total_tokens:,} / {usage.capacity:,} tokens"
    )


def _handle_context_command(command: str) -> bool:
    usage = get_current_usage()
    if usage is None:
        _emit_info("🐶 No context info yet — load an agent and send a message first.")
        return True
    _emit_info(_format_usage_report(usage))
    return True


def _handle_custom_command(command: str, name: str):
    if name != _COMMAND_NAME:
        return None
    return _handle_context_command(command)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
register_callback("custom_command", _handle_custom_command)
register_callback("custom_command_help", _custom_help)


__all__ = [
    "_custom_help",
    "_format_overhead_breakdown",
    "_format_usage_report",
    "_handle_context_command",
    "_handle_custom_command",
]
