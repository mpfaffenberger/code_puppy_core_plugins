"""Plugin: spill oversized structured tool results to private files.

Ported from DeepSeek Harness's MIT-licensed spill design. See
``LICENSE.deepseek`` in this package for its copyright and license notice.

The post-tool hook receives the same result object that Code Puppy later
serializes for the model. This plugin can therefore replace top-level string
fields in exact built-in dictionaries and an audited allowlist of Code Puppy
scalar output models in place. The allowlist covers practical shell,
file-listing, and agent-invocation results while arbitrary/custom Pydantic
models remain inline. Captured serializer/validator identities and runtime
field types must still match before an allowlisted model is inspected.
Other results (notably strings and ``ToolReturn`` image payloads) are untouched
unless pre-tool hook context has safely converted the combined return into a
plain textual envelope, which is independently eligible for spilling.

When the combined decoded UTF-8 size of top-level string values exceeds the
configured budget, fields are considered largest-first. JSON keys, syntax,
escaping, provider wire bytes, and tokens are intentionally outside this
budget. Full text is saved verbatim, then replaced by a bounded byte-sliced
head/tail preview and a retrieval notice.
``read_file`` is skipped by default to avoid a read -> spill -> read loop.
``activate_skill`` is also skipped because its instructions are intentionally
consumed as one semantic unit. Spill failures leave successful results inline.

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
import concurrent.futures
import logging
import math
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from code_puppy.callbacks import register_callback

try:
    from code_puppy.callbacks import (
        _register_terminal_callback,
        on_final_tool_result as _final_tool_result_api,
    )
except ImportError:  # Compatibility while coordinated Code Puppy releases roll out.
    _HAS_FINAL_TOOL_RESULT = False
    _HAS_TERMINAL_CALLBACKS = False
else:
    _HAS_FINAL_TOOL_RESULT = callable(_final_tool_result_api)
    _HAS_TERMINAL_CALLBACKS = callable(_register_terminal_callback)

from . import store
from .result_shapes import (
    ModelValidationSpec,
    byte_size as _byte_size,
    commit_replacements as _commit_replacements,
    model_facing_mapping as _model_facing_mapping,
    model_validation_spec as _model_validation_spec,
    result_accepts_fields as _result_accepts_fields,
    string_fields as _string_fields,
    validate_model_replacements as _validate_model_replacements,
)

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
MAX_SPILL_FIELDS = 128
OMISSION_MARKER = "\n\n[...]\n\n"
_CLEANUP_EXECUTOR = concurrent.futures.ThreadPoolExecutor(1, "spill-cleanup")


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


@dataclass(frozen=True)
class _SpillSettings:
    cap: int
    preview_bytes: int
    root: str | None
    skip_tools: frozenset[str]


@dataclass(frozen=True)
class _SpillPlan:
    replacements: dict[Any, str]
    expected_strings: dict[Any, str]
    paths: tuple[Path, ...]
    model_validated: bool


@dataclass
class _SpillJob:
    """Coordinate worker-owned files with cancellation on the event loop."""

    _lock: threading.Lock = field(default_factory=threading.Lock)
    _cancelled: bool = False
    _paths: set[Path] = field(default_factory=set)

    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def track(self, path: Path) -> bool:
        with self._lock:
            if not self._cancelled:
                self._paths.add(path)
                return True
        _cleanup_paths([path])
        return False

    def forget(self, path: Path) -> None:
        with self._lock:
            self._paths.discard(path)

    def cancel(self) -> tuple[Path, ...]:
        with self._lock:
            self._cancelled = True
            paths = tuple(self._paths)
            self._paths.clear()
        return paths

    def release(self) -> None:
        with self._lock:
            self._paths.clear()


def _get_settings() -> _SpillSettings:
    preview_bytes = _get_int(PREVIEW_KEY, DEFAULT_PREVIEW_BYTES)
    if preview_bytes < 0:
        logger.warning(
            "Invalid %s value; falling back to %d",
            PREVIEW_KEY,
            DEFAULT_PREVIEW_BYTES,
        )
        preview_bytes = DEFAULT_PREVIEW_BYTES
    return _SpillSettings(
        cap=_get_int(MAX_INLINE_KEY, DEFAULT_MAX_INLINE_BYTES),
        preview_bytes=preview_bytes,
        root=_get_root(),
        skip_tools=_get_skip_tools(),
    )


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


def _minimum_notice_bytes(original_bytes: int) -> int:
    """Lower bound for a zero-preview notice before allocating a spill path."""
    return _byte_size(_notice(original_bytes, Path(".")))


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


def _cleanup_paths(paths: tuple[Path, ...] | list[Path]) -> None:
    for path in set(paths):
        try:
            path.unlink(missing_ok=True)
        except Exception:
            logger.debug("Could not clean up an unused spill file")


def _prepare_spill(
    tool_name: str,
    fields: tuple[tuple[Any, str, int], ...],
    settings: _SpillSettings,
    session_id: str | None,
    job: _SpillJob | None = None,
    model_spec: ModelValidationSpec | None = None,
) -> _SpillPlan | None:
    """Build a globally feasible bounded plan, then persist only needed fields."""
    total = sum(size for _, _, size in fields)
    if total <= settings.cap:
        return None

    ordered = sorted(fields, key=lambda item: item[2], reverse=True)
    candidates = [
        (*item, _minimum_notice_bytes(item[2]))
        for item in ordered
        if _minimum_notice_bytes(item[2]) < item[2]
    ][:MAX_SPILL_FIELDS]
    maximum_reduction = sum(
        original_bytes - minimum_bytes
        for _, _, original_bytes, minimum_bytes in candidates
    )
    if total - maximum_reduction > settings.cap:
        return None

    active_job = job or _SpillJob()
    paths: list[Path] = []
    selected: list[tuple[Any, str, Path, str]] = []
    minimum_total = total
    try:
        for key, original, original_bytes, _ in candidates:
            if minimum_total <= settings.cap:
                break
            if active_job.is_cancelled():
                _cleanup_paths([*paths, *active_job.cancel()])
                return None
            try:
                path = store.save_text(
                    original,
                    tool_name,
                    settings.root,
                    session_id=session_id,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to persist a %s spill field (%s); keeping it inline",
                    tool_name,
                    type(exc).__name__,
                )
                continue

            try:
                if not active_job.track(path):
                    return None
                paths.append(path)
            except BaseException:
                _cleanup_paths([path, *active_job.cancel()])
                raise

            minimum = _build_replacement(original, path, 0, None)
            if minimum is None:
                _cleanup_paths([path])
                active_job.forget(path)
                paths.remove(path)
                continue
            selected.append((key, original, path, minimum))
            minimum_total += _byte_size(minimum) - original_bytes

        if minimum_total > settings.cap or active_job.is_cancelled():
            _cleanup_paths([*paths, *active_job.cancel()])
            return None

        slack = settings.cap - minimum_total
        replacements: dict[Any, str] = {}
        for key, original, path, minimum in selected:
            minimum_bytes = _byte_size(minimum)
            replacement = _build_replacement(
                original,
                path,
                settings.preview_bytes,
                minimum_bytes + slack,
            )
            if replacement is None:
                replacement = minimum
            growth = _byte_size(replacement) - minimum_bytes
            slack -= growth
            replacements[key] = replacement

        if model_spec is not None and not _validate_model_replacements(
            model_spec, replacements, settings.cap
        ):
            _cleanup_paths([*paths, *active_job.cancel()])
            return None
        return _SpillPlan(
            replacements=replacements,
            expected_strings={key: value for key, value, _ in fields},
            paths=tuple(paths),
            model_validated=model_spec is not None,
        )
    except BaseException:
        _cleanup_paths([*paths, *active_job.cancel()])
        raise


def _inspect_and_prepare_spill(
    tool_name: str,
    result: Any,
    settings: _SpillSettings,
    job: _SpillJob,
    session_id: str | None = None,
) -> _SpillPlan | None:
    """Inspect, size, validate, and persist entirely on a worker thread."""
    if job.is_cancelled():
        return None
    mapping = _model_facing_mapping(result)
    if mapping is None or set(mapping) == {"error"}:
        return None
    fields = tuple(_string_fields(mapping))
    if sum(size for _, _, size in fields) <= settings.cap:
        return None
    if not _result_accepts_fields(result, tuple(key for key, _, _ in fields)):
        return None
    return _prepare_spill(
        tool_name,
        fields,
        settings,
        session_id or store.current_session_id(),
        job,
        _model_validation_spec(result),
    )


def _spill_result(tool_name: str, result: Any, session_id: str | None = None) -> None:
    """Synchronous compatibility entry point used by tests and embedders."""
    settings = _get_settings()
    if settings.cap <= 0 or tool_name in settings.skip_tools:
        return
    plan = _inspect_and_prepare_spill(
        tool_name,
        result,
        settings,
        _SpillJob(),
        session_id,
    )
    if plan is None:
        return
    committed = False
    try:
        committed = _commit_replacements(
            result,
            plan.replacements,
            plan.expected_strings,
            model_validated=plan.model_validated,
        )
    except Exception as exc:
        logger.debug("Synchronous spill commit failed (%s)", type(exc).__name__)
    finally:
        if not committed:
            _cleanup_paths(plan.paths)


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
        # Resolve every setting before inspecting result fields or dispatching
        # work. Disabled/skipped tools should pay essentially zero spill cost.
        spill_config = _get_executing_agent_spill_config()
        if not _is_enabled_for_executing_agent(spill_config):
            return
        if tool_name in _get_agent_skip_tools(spill_config):
            return
        settings = _get_settings()
        if settings.cap <= 0 or tool_name in settings.skip_tools:
            return

        job = _SpillJob()
        worker = asyncio.create_task(
            asyncio.to_thread(
                _inspect_and_prepare_spill,
                tool_name,
                result,
                settings,
                job,
            )
        )
        try:
            plan = await worker
        except asyncio.CancelledError:
            paths = job.cancel()
            if paths:
                try:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(
                        _CLEANUP_EXECUTOR,
                        _cleanup_paths,
                        paths,
                    )
                except BaseException as cleanup_error:
                    logger.debug(
                        "Cancellation spill cleanup failed (%s)",
                        type(cleanup_error).__name__,
                    )
            raise
        except Exception:
            await asyncio.to_thread(_cleanup_paths, job.cancel())
            raise
        if plan is None:
            await asyncio.to_thread(_cleanup_paths, job.cancel())
            return

        committed = False
        try:
            committed = _commit_replacements(
                result,
                plan.replacements,
                plan.expected_strings,
                model_validated=plan.model_validated,
            )
        except Exception as exc:
            logger.debug("spill commit failed (%s)", type(exc).__name__)
        finally:
            if committed:
                job.release()
            else:
                await asyncio.to_thread(_cleanup_paths, job.cancel())
    except Exception as exc:
        logger.debug(
            "spill plugin failed (%s); keeping the tool result inline",
            type(exc).__name__,
        )


def _on_startup() -> None:
    """Retain legacy startup ordering for older Code Puppy releases."""
    if _HAS_FINAL_TOOL_RESULT and _HAS_TERMINAL_CALLBACKS:
        return
    from code_puppy import callbacks

    callbacks.unregister_callback("post_tool_call", _on_post_tool_call)
    callbacks.register_callback("post_tool_call", _on_post_tool_call)


def _register_callbacks() -> None:
    if _HAS_FINAL_TOOL_RESULT and _HAS_TERMINAL_CALLBACKS:
        _register_terminal_callback("final_tool_result", _on_post_tool_call)
        return
    register_callback("post_tool_call", _on_post_tool_call)
    register_callback("startup", _on_startup)


def _reset_state() -> None:
    """Reset lazy process storage state for tests and defensive re-init."""
    store._reset_state()


_register_callbacks()


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
    "_build_replacement",
    "_cleanup_paths",
    "_commit_replacements",
    "_get_agent_skip_tools",
    "_get_executing_agent_spill_config",
    "_get_int",
    "_get_settings",
    "_is_agent_spill_enabled",
    "_is_enabled_for_executing_agent",
    "_model_facing_mapping",
    "_on_post_tool_call",
    "_on_startup",
    "_prepare_spill",
    "_reset_state",
    "_spill_result",
]
