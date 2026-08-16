"""Plugin: spill oversized structured tool results to private files.

Ported from DeepSeek Harness's MIT-licensed spill design. See
``LICENSE.deepseek`` in this package for its copyright and license notice.

The post-tool hook receives the same result object that Code Puppy later
serializes for the model. This plugin can therefore replace top-level string
fields in plain dictionaries and mutable Pydantic models in place. Other
results (notably plain strings and ``ToolReturn`` image payloads) cannot be
replaced through this hook and are deliberately left untouched.

When the combined UTF-8 size of top-level string values exceeds the configured
cap, fields are considered largest-first. Their full text is saved verbatim,
then replaced by a bounded byte-sliced head/tail preview and a retrieval notice.
``read_file`` is skipped by default to avoid a read -> spill -> read loop.
``activate_skill`` is also skipped because its instructions are intentionally
consumed as one semantic unit. Every failure is best-effort: the successful
tool result stays available inline.

Global config (puppy.cfg, also settable with ``/set``):
    spill_max_inline_bytes = 32768  # 0 or negative disables
    spill_preview_bytes = 4096      # source bytes retained per field
    spill_root =                    # unset uses a private OS temp directory
    spill_skip_tools = read_file,activate_skill  # comma-separated tool names

An individual agent can opt out or add exact-name tool skips:
    "tools_config": {"spill": {"enabled": false}}
    "tools_config": {"spill": {"skip_tools": ["custom_report"]}}
"""

from __future__ import annotations

import asyncio
import logging
import math
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from code_puppy.callbacks import register_callback, unregister_callback

from . import store

logger = logging.getLogger(__name__)

MAX_INLINE_KEY = "spill_max_inline_bytes"
PREVIEW_KEY = "spill_preview_bytes"
ROOT_KEY = "spill_root"
SKIP_TOOLS_KEY = "spill_skip_tools"
AGENT_CONFIG_KEY = "spill"
AGENT_ENABLED_KEY = "enabled"
AGENT_SKIP_TOOLS_KEY = "skip_tools"
DEFAULT_MAX_INLINE_BYTES = 32768
DEFAULT_PREVIEW_BYTES = 4096
DEFAULT_SKIP_TOOLS = frozenset({"activate_skill", "read_file"})
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


def _get_executing_agent_spill_config() -> dict[str, Any]:
    """Return valid spill config for this run's agent, or an empty config.

    Older Code Puppy versions do not expose the execution-context seam. They
    safely retain the historical globally configured behavior.
    """
    try:
        from code_puppy.agent_execution_context import get_executing_agent
    except ImportError:
        return {}

    agent = get_executing_agent()
    if agent is None:
        return {}

    try:
        tools_config = agent.get_tools_config()
    except Exception:
        logger.debug("Could not read executing agent tools_config", exc_info=True)
        return {}

    if not isinstance(tools_config, dict):
        return {}
    spill_config = tools_config.get(AGENT_CONFIG_KEY)
    return spill_config if isinstance(spill_config, dict) else {}


def _is_agent_spill_enabled(spill_config: dict[str, Any]) -> bool:
    """Return false only for an explicit per-agent boolean opt-out."""
    enabled = spill_config.get(AGENT_ENABLED_KEY, True)
    return enabled if isinstance(enabled, bool) else True


def _get_agent_skip_tools(spill_config: dict[str, Any]) -> frozenset[str]:
    """Return valid exact-name skips contributed by one agent."""
    raw = spill_config.get(AGENT_SKIP_TOOLS_KEY)
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(
        name.strip() for name in raw if isinstance(name, str) and name.strip()
    )


def _is_enabled_for_executing_agent(
    spill_config: dict[str, Any] | None = None,
) -> bool:
    """Return whether spill is enabled for the executing agent."""
    effective_config = (
        _get_executing_agent_spill_config() if spill_config is None else spill_config
    )
    return _is_agent_spill_enabled(effective_config)


def _result_items(result: Any) -> list[tuple[Any, Any]] | None:
    """Return declared top-level result fields for supported mutable shapes."""
    if isinstance(result, dict):
        return list(result.items())
    if isinstance(result, BaseModel):
        try:
            return [
                (field_name, getattr(result, field_name))
                for field_name in type(result).model_fields
            ]
        except Exception:
            logger.debug("Could not inspect Pydantic tool result", exc_info=True)
    return None


def _apply_replacements(result: Any, replacements: dict[Any, str]) -> bool:
    """Commit replacements in place, rolling model fields back on failure."""
    if isinstance(result, dict):
        result.update(replacements)
        return True
    if not isinstance(result, BaseModel):
        return False

    originals = {field_name: getattr(result, field_name) for field_name in replacements}
    applied: list[Any] = []
    try:
        for field_name, replacement in replacements.items():
            setattr(result, field_name, replacement)
            applied.append(field_name)
    except Exception:
        logger.warning(
            "Could not mutate Pydantic tool result; keeping it inline",
            exc_info=True,
        )
        for field_name in reversed(applied):
            try:
                setattr(result, field_name, originals[field_name])
            except Exception:
                logger.error(
                    "Could not roll back Pydantic result field %r",
                    field_name,
                    exc_info=True,
                )
        return False
    return True


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


def _spill_result(tool_name: str, result: Any, session_id: str | None = None) -> None:
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

    result_items = _result_items(result)
    if result_items is None:
        return
    fields = [
        (key, value, _byte_size(value))
        for key, value in result_items
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
        _apply_replacements(result, replacements)


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
        result_items = _result_items(result)
        if result_items is None or {key for key, _ in result_items} == {"error"}:
            return
        spill_config = _get_executing_agent_spill_config()
        if not _is_enabled_for_executing_agent(spill_config):
            return
        if tool_name in _get_agent_skip_tools(spill_config):
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
    "AGENT_SKIP_TOOLS_KEY",
    "DEFAULT_MAX_INLINE_BYTES",
    "DEFAULT_PREVIEW_BYTES",
    "DEFAULT_SKIP_TOOLS",
    "MAX_INLINE_KEY",
    "PREVIEW_KEY",
    "ROOT_KEY",
    "SKIP_TOOLS_KEY",
    "_apply_replacements",
    "_build_replacement",
    "_get_agent_skip_tools",
    "_get_executing_agent_spill_config",
    "_get_int",
    "_is_agent_spill_enabled",
    "_is_enabled_for_executing_agent",
    "_on_post_tool_call",
    "_on_startup",
    "_reset_state",
    "_result_items",
    "_spill_result",
]
