"""Plugin: require fresh reads before Code Puppy file-tool edits.

This ports DeepSeek Harness's MIT-licensed ``fs-observation-policy`` into Code
Puppy's callback architecture. A successful read (including a ranged read) or
mutation records the canonical path's ``(st_mtime_ns, st_size)`` version for the
active conversation/subagent scope. Targeted edits require that observation and
must still match it; full-file overwrites may not blindly clobber an unread file.

This is a correctness guard, not a permission prompt, so YOLO mode does not
bypass it. Set ``read_before_write_enabled = 0`` (or ``false``) in ``puppy.cfg``
to disable enforcement; observations continue to be recorded while disabled.
``delete_file`` is deliberately unguarded in v1 and retains its normal
interactive permission flow, matching the source policy's treatment of deletes.
Shell redirection and browser/MCP file tools are out of scope: only Code Puppy's
named file tools pass these hooks. Raw paths are Pydantic-coerced and resolved
through the same session working-directory helper as those tools before
``realpath(abspath(...))`` canonicalization.

Versions use local metadata rather than content hashes. A pre-read snapshot
prevents a changed path/version from being blessed by the post hook, but tiny
read-syscall-to-stat and pre-stat-to-mutation races remain because the tools do
not expose atomic revision/CAS operations. Likewise, the filesystem-backend
protocol exposes no content revision, so host-only unsaved-buffer and virtual
filesystem changes cannot be versioned until core grows that API.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from code_puppy.callbacks import register_callback
from code_puppy.tools.subagent_context import (
    get_conversation_root_id,
    get_subagent_chain,
)

from . import policy

logger = logging.getLogger(__name__)

CONFIG_KEY = "read_before_write_enabled"
ENABLED_CONFIG_KEY = CONFIG_KEY
DEFAULT_ENABLED = True

# Re-export the state primitives from the logic module for focused tests and
# debugging without making callback registration itself chunky.
MutationSnapshot = policy.MutationSnapshot
Observation = policy.Observation
ReadSnapshot = policy.ReadSnapshot
_observations = policy._observations


@dataclass(frozen=True, slots=True)
class _ReadAttempt:
    tool_args: dict
    snapshot: policy.ReadSnapshot | None


@dataclass(frozen=True, slots=True)
class _MutationAttempt:
    tool_name: str
    tool_args: dict
    snapshot: policy.MutationSnapshot | None


_read_attempt: ContextVar[_ReadAttempt | None] = ContextVar(
    "read_before_write_read_attempt", default=None
)
_mutation_attempt: ContextVar[_MutationAttempt | None] = ContextVar(
    "read_before_write_mutation_attempt", default=None
)


def _scope_key() -> policy.ScopeKey:
    """Identify the active conversation and exact subagent ancestry."""
    return (get_conversation_root_id() or "global", get_subagent_chain())


def _is_enabled() -> bool:
    """Read defensive boolean config; config failures disable enforcement."""
    try:
        from code_puppy.config import get_value

        raw = get_value(CONFIG_KEY)
    except Exception:
        logger.warning(
            "Could not read %s; read-before-write enforcement is fail-open",
            CONFIG_KEY,
            exc_info=True,
        )
        return False

    if raw is None:
        return DEFAULT_ENABLED
    try:
        text = str(raw).strip().lower()
    except Exception:
        logger.warning(
            "Invalid %s value; read-before-write enforcement is fail-open",
            CONFIG_KEY,
            exc_info=True,
        )
        return False
    if not text:
        return DEFAULT_ENABLED
    if text in {"0", "false"}:
        return False
    if text in {"1", "true"}:
        return True

    logger.warning(
        "Invalid %s value %r; falling back to enabled",
        CONFIG_KEY,
        raw,
    )
    return DEFAULT_ENABLED


def _on_pre_tool_call(
    tool_name: str,
    tool_args: dict,
    context: Any = None,
) -> dict[str, bool | str] | None:
    """Enforce observation/version rules and otherwise allow the tool call."""
    _ = context
    _read_attempt.set(None)
    _mutation_attempt.set(None)
    if tool_name in policy.MUTATION_TOOLS:
        try:
            snapshot = policy.capture_mutation_snapshot(tool_args)
            _mutation_attempt.set(_MutationAttempt(tool_name, tool_args, snapshot))
        except Exception:
            logger.warning(
                "read-before-write mutation snapshot failed open",
                exc_info=True,
            )
    if tool_name == "read_file":
        try:
            snapshot = policy.capture_read_snapshot(tool_args)
            _read_attempt.set(_ReadAttempt(tool_args, snapshot))
        except Exception:
            logger.warning(
                "read-before-write pre-read snapshot failed open",
                exc_info=True,
            )
        return None
    if tool_name not in policy.GUARDED_TOOLS:
        return None
    try:
        if not _is_enabled():
            return None
        decision = policy.enforce(tool_name, tool_args, _scope_key())
        if isinstance(decision, dict) and decision.get("blocked"):
            _mutation_attempt.set(None)
        return decision
    except Exception:
        logger.warning(
            "read-before-write pre-tool guard failed open for %s",
            tool_name,
            exc_info=True,
        )
        return None


def _on_post_tool_call(
    tool_name: str,
    tool_args: dict,
    result: Any,
    duration_ms: float,
    context: Any = None,
) -> None:
    """Best-effort record reads and successful file mutations."""
    _ = duration_ms, context
    read_attempt = _read_attempt.get()
    mutation_attempt = _mutation_attempt.get()
    _read_attempt.set(None)
    _mutation_attempt.set(None)
    if tool_name not in policy.OBSERVATION_TOOLS:
        return None
    read_snapshot = (
        read_attempt.snapshot
        if tool_name == "read_file"
        and read_attempt is not None
        and read_attempt.tool_args is tool_args
        else None
    )
    mutation_snapshot = (
        mutation_attempt.snapshot
        if tool_name in policy.MUTATION_TOOLS
        and mutation_attempt is not None
        and mutation_attempt.tool_name == tool_name
        and mutation_attempt.tool_args is tool_args
        else None
    )
    try:
        policy.record(
            tool_name,
            tool_args,
            result,
            _scope_key(),
            read_snapshot=read_snapshot,
            mutation_snapshot=mutation_snapshot,
        )
    except Exception:
        logger.warning(
            "read-before-write observation failed for %s",
            tool_name,
            exc_info=True,
        )
    return None


def _reset_state() -> None:
    """Clear every recorded scope (used by tests and defensive re-init)."""
    policy._reset_state()
    _read_attempt.set(None)
    _mutation_attempt.set(None)


register_callback("pre_tool_call", _on_pre_tool_call)
register_callback("post_tool_call", _on_post_tool_call)


__all__ = [
    "CONFIG_KEY",
    "DEFAULT_ENABLED",
    "ENABLED_CONFIG_KEY",
    "MutationSnapshot",
    "Observation",
    "ReadSnapshot",
    "_is_enabled",
    "_observations",
    "_on_post_tool_call",
    "_on_pre_tool_call",
    "_reset_state",
    "_scope_key",
]
