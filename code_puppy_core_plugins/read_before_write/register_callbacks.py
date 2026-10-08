"""Plugin: require fresh reads before Code Puppy file-tool edits.

This ports DeepSeek Harness's MIT-licensed ``fs-observation-policy`` into Code
Puppy's callback architecture. A successful read (including a ranged read) or
mutation records the canonical path's ``(st_mtime_ns, st_size)`` version for the
active conversation/subagent scope. Targeted edits require that observation and
must still match it; full-file overwrites may not blindly clobber an unread file.

The guard is opt-in and OFF by default. Set ``read_before_write_enabled = 1``
(or ``true``) in ``puppy.cfg`` to enable enforcement. Observations are recorded
either way, so turning it on mid-session already knows about earlier reads. A
missing, empty, unparseable or unreadable setting all mean "off".

Once enabled this is a correctness guard, not a permission prompt, so YOLO mode
does not bypass it.
``delete_file`` is deliberately unguarded in v1 and retains its normal
interactive permission flow, matching the source policy's treatment of deletes.
Shell redirection and browser/MCP file tools are out of scope: only Code Puppy's
named file tools pass these hooks. Raw paths are Pydantic-coerced and resolved
through the same session working-directory helper as those tools before
``realpath`` canonicalization.

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
DEFAULT_ENABLED = False

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


# The most recent config problem that was logged, as (kind, detail). The config
# is re-read on every guarded call (so enabling at runtime works) but an
# unchanged problem is logged only once; a clean read clears it.
_last_diagnostic: tuple[str, str] | None = None


def _warn_once(kind: str, detail: str, message: str, *args: Any, **kwargs: Any) -> None:
    global _last_diagnostic
    if _last_diagnostic != (kind, detail):
        _last_diagnostic = (kind, detail)
        logger.warning(message, *args, **kwargs)


def _is_enabled() -> bool:
    """Return whether enforcement is on (opt-in, off by default).

    Every failure mode leans the same way: a config read error, a missing or
    empty value, or an unparseable value all leave enforcement OFF. Only an
    explicit truthy value (``1`` / ``true``) turns it on. The config is read on
    every call; a problem is logged once until it changes or the config reads
    cleanly again.
    """
    global _last_diagnostic
    try:
        from code_puppy.config import get_value

        raw = get_value(CONFIG_KEY)
        text = "" if raw is None else str(raw).strip().lower()
    except Exception as exc:
        _warn_once(
            "unreadable",
            f"{type(exc).__name__}: {exc}",
            "Could not read %s; read-before-write enforcement stays off",
            CONFIG_KEY,
            exc_info=True,
        )
        return False

    if text in {"", "0", "false", "1", "true"}:
        _last_diagnostic = None
        if not text:
            return DEFAULT_ENABLED
        return text in {"1", "true"}

    _warn_once(
        "invalid",
        repr(raw),
        "Invalid %s value %r; read-before-write enforcement stays off",
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
    global _last_diagnostic
    _last_diagnostic = None
    policy._reset_state()
    _read_attempt.set(None)
    _mutation_attempt.set(None)


register_callback("pre_tool_call", _on_pre_tool_call)
register_callback("post_tool_call", _on_post_tool_call)


__all__ = [
    "CONFIG_KEY",
    "DEFAULT_ENABLED",
    "MutationSnapshot",
    "Observation",
    "ReadSnapshot",
]
