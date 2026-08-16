"""Plugin: spill oversized dict-shaped tool results to private files.

Ported from DeepSeek Harness's MIT-licensed spill design. See
``LICENSE.deepseek`` in this package for its copyright and license notice.

The post-tool hook receives the same dict reference that Code Puppy later
serializes for the model, so this plugin can replace top-level string fields
in place. Non-dict results (notably plain strings) cannot be replaced through
this hook and are deliberately left untouched.

When the combined UTF-8 size of top-level string values exceeds the configured
cap, fields are considered largest-first. Their full text is saved verbatim,
then replaced by a bounded byte-sliced head/tail preview and a retrieval notice.
``read_file`` is skipped by default to avoid a read -> spill -> read loop.
Every failure is best-effort: the successful tool result stays available inline.

Global config (puppy.cfg, also settable with ``/set``):
    spill_max_inline_bytes = 32768  # 0 or negative disables
    spill_preview_bytes = 4096      # source bytes retained per field
    spill_root =                    # unset uses a private OS temp directory
    spill_skip_tools = read_file    # comma-separated tool names

An individual agent can opt out without changing the global cap:
    "tools_config": {"spill": {"enabled": false}}
"""

from __future__ import annotations

import asyncio
import logging
import math
from pathlib import Path
from typing import Any

from code_puppy.callbacks import register_callback, unregister_callback

from . import store

logger = logging.getLogger(__name__)

MAX_INLINE_KEY = "spill_max_inline_bytes"
PREVIEW_KEY = "spill_preview_bytes"
ROOT_KEY = "spill_root"
SKIP_TOOLS_KEY = "spill_skip_tools"
AGENT_CONFIG_KEY = "spill"
AGENT_ENABLED_KEY = "enabled"
DEFAULT_MAX_INLINE_BYTES = 32768
DEFAULT_PREVIEW_BYTES = 4096
DEFAULT_SKIP_TOOLS = frozenset({"read_file"})
OMISSION_MARKER = "\n\n[...]\n\n"


def _get_value(key: str) -> Any:
    try:
        from code_puppy.config import get_value

        return get_value(key)
    except Exception:
        return None


def _get_int(key: str, default: int) -> int:
    raw = _get_value(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("Invalid %s value; falling back to %d", key, default)
        return default


def _get_root() -> str | None:
    raw = _get_value(ROOT_KEY)
    if raw is None or not str(raw).strip():
        return None
    return str(raw).strip()


def _get_skip_tools() -> frozenset[str]:
    raw = _get_value(SKIP_TOOLS_KEY)
    if raw is None or not str(raw).strip():
        return DEFAULT_SKIP_TOOLS
    return frozenset(name.strip() for name in str(raw).split(",") if name.strip())


def _is_enabled_for_executing_agent() -> bool:
    """Return false only for an explicit per-agent boolean opt-out.

    Older Code Puppy versions do not expose the execution-context seam. They
    safely retain the historical globally configured behavior.
    """
    try:
        from code_puppy.agent_execution_context import get_executing_agent
    except ImportError:
        return True

    agent = get_executing_agent()
    if agent is None:
        return True

    try:
        tools_config = agent.get_tools_config()
    except Exception:
        logger.debug("Could not read executing agent tools_config", exc_info=True)
        return True

    if not isinstance(tools_config, dict):
        return True
    spill_config = tools_config.get(AGENT_CONFIG_KEY)
    if not isinstance(spill_config, dict):
        return True
    enabled = spill_config.get(AGENT_ENABLED_KEY, True)
    return enabled if isinstance(enabled, bool) else True


def _byte_size(text: str) -> int:
    return len(text.encode("utf-8"))


def _preview(text: str, budget: int) -> tuple[str, int]:
    """Return a byte-sliced preview and the count of omitted source bytes."""
    encoded = text.encode("utf-8")
    kept_bytes = min(max(0, budget), len(encoded))
    if kept_bytes == 0:
        return "", len(encoded)
    head_bytes = math.ceil(kept_bytes / 2)
    tail_bytes = math.floor(kept_bytes / 2)
    head = encoded[:head_bytes].decode("utf-8", errors="replace")
    tail = encoded[-tail_bytes:].decode("utf-8", errors="replace") if tail_bytes else ""
    return f"{head}{OMISSION_MARKER}{tail}", len(encoded) - kept_bytes


def _notice(omitted: int, path: Path) -> str:
    return (
        f"(~{omitted} bytes omitted. Full output stored at: {path}. "
        "Retrieve it with read_file using start_line/num_lines, or grep this path.)"
    )


def _build_replacement(
    text: str,
    path: Path,
    preview_bytes: int,
    max_bytes: int | None,
) -> str | None:
    """Build a strictly smaller replacement, optionally fitting ``max_bytes``."""
    original_bytes = _byte_size(text)
    budget = min(max(0, preview_bytes), original_bytes)
    limit = original_bytes - 1
    if max_bytes is not None:
        limit = min(limit, max_bytes)
    if limit < 0:
        return None

    while True:
        preview, omitted = _preview(text, budget)
        notice = _notice(omitted, path)
        replacement = f"{preview}\n\n{notice}" if preview else notice
        replacement_bytes = _byte_size(replacement)
        if replacement_bytes <= limit:
            return replacement
        if budget == 0:
            return None
        budget = max(0, budget - max(1, replacement_bytes - limit))


def _spill_result(
    tool_name: str, result: dict[Any, Any], session_id: str | None = None
) -> None:
    cap = _get_int(MAX_INLINE_KEY, DEFAULT_MAX_INLINE_BYTES)
    if cap <= 0 or tool_name in _get_skip_tools():
        return
    preview_bytes = _get_int(PREVIEW_KEY, DEFAULT_PREVIEW_BYTES)
    if preview_bytes < 0:
        logger.warning(
            "Invalid %s value; falling back to %d",
            PREVIEW_KEY,
            DEFAULT_PREVIEW_BYTES,
        )
        preview_bytes = DEFAULT_PREVIEW_BYTES

    fields = [
        (key, value, _byte_size(value))
        for key, value in result.items()
        if isinstance(value, str)
    ]
    total = sum(size for _, _, size in fields)
    if total <= cap:
        return

    replacements: dict[Any, str] = {}
    for key, original, original_bytes in sorted(
        fields, key=lambda item: item[2], reverse=True
    ):
        if total <= cap:
            break
        try:
            path = store.save_text(
                original, tool_name, _get_root(), session_id=session_id
            )
        except Exception:
            logger.warning(
                "Failed to spill %s result field %r; keeping it inline",
                tool_name,
                key,
                exc_info=True,
            )
            continue

        other_bytes = total - original_bytes
        field_limit = cap - other_bytes if other_bytes < cap else None
        replacement = _build_replacement(
            original,
            path,
            preview_bytes,
            field_limit,
        )
        if replacement is None:
            logger.warning(
                "Spill notice for %s result field %r cannot fit; keeping it inline",
                tool_name,
                key,
            )
            continue
        replacements[key] = replacement
        total = other_bytes + _byte_size(replacement)

    # Commit atomically. A partial preview set that still exceeds the cap is
    # neither bounded nor graceful; keep every original inline in that case.
    if total <= cap:
        result.update(replacements)


async def _on_post_tool_call(
    tool_name: str,
    tool_args: dict,
    result: Any,
    duration_ms: float,
    context: Any = None,
) -> None:
    """Spill oversized string fields off the event loop without breaking calls."""
    _ = tool_args, duration_ms, context
    try:
        if not isinstance(result, dict) or set(result) == {"error"}:
            return
        if not _is_enabled_for_executing_agent():
            return
        # Capture session attribution before entering the worker. The result
        # reference remains valid and the callback dispatcher awaits us before
        # the model serializes it.
        session_id = store.current_session_id()
        await asyncio.to_thread(_spill_result, tool_name, result, session_id)
    except Exception:
        logger.debug(
            "spill plugin failed; keeping the tool result inline", exc_info=True
        )


def _on_startup() -> None:
    """Move spill behind result-mutating plugins so the final result is capped."""
    if unregister_callback("post_tool_call", _on_post_tool_call):
        # unregister_callback intentionally preserves callback ownership, so
        # disabled-plugin filtering still recognizes this as the spill hook.
        register_callback("post_tool_call", _on_post_tool_call)


register_callback("startup", _on_startup)


def _reset_state() -> None:
    """Reset lazy process storage state for tests and defensive re-init."""
    store._reset_state()


register_callback("post_tool_call", _on_post_tool_call)


__all__ = [
    "AGENT_CONFIG_KEY",
    "AGENT_ENABLED_KEY",
    "DEFAULT_MAX_INLINE_BYTES",
    "DEFAULT_PREVIEW_BYTES",
    "DEFAULT_SKIP_TOOLS",
    "MAX_INLINE_KEY",
    "PREVIEW_KEY",
    "ROOT_KEY",
    "SKIP_TOOLS_KEY",
    "_build_replacement",
    "_get_int",
    "_is_enabled_for_executing_agent",
    "_on_post_tool_call",
    "_on_startup",
    "_reset_state",
    "_spill_result",
]
