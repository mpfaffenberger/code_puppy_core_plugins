"""Observation state and decisions for the read-before-write plugin.

The design ports DeepSeek Harness's MIT-licensed ``fs-observation-policy``.
See ``LICENSE.deepseek`` for attribution. Files are versioned pragmatically by
``(st_mtime_ns, st_size)``; this catches stale context but leaves a tiny stat ->
mutation race because Code Puppy's file tools do not expose an atomic CAS API.
"""

from __future__ import annotations

import ast
import logging
import os
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import TypeAdapter, ValidationError

from code_puppy.tools.common import resolve_path

Version = tuple[int, int]
ScopeKey = tuple[str, tuple[str, ...]]

EDIT_TOOLS = frozenset({"delete_snippet", "replace_in_file"})
GUARDED_TOOLS = EDIT_TOOLS | {"create_file"}
MUTATION_TOOLS = frozenset({"create_file", "delete_snippet", "replace_in_file"})
OBSERVATION_TOOLS = MUTATION_TOOLS | {"delete_file", "read_file"}

logger = logging.getLogger(__name__)

_PATH_ADAPTER = TypeAdapter(str)
_OVERWRITE_ADAPTER = TypeAdapter(bool)
_HOOK_CONTEXT_PREFIX = "[hook context]\n"


@dataclass(frozen=True, slots=True)
class Observation:
    """An authoritative observation that a canonical path exists or is absent."""

    kind: Literal["present", "absent"]
    version: Version | None = None


@dataclass(frozen=True, slots=True)
class ReadSnapshot:
    """The tool-effective identity/version immediately before a read call."""

    normalized_path: str
    version: Version | None


@dataclass(frozen=True, slots=True)
class MutationSnapshot:
    """The canonical target identity immediately before a mutation call."""

    normalized_path: str


_observations: dict[ScopeKey, dict[str, Observation]] = {}


def _reset_state() -> None:
    """Drop every conversation's observations (tests and defensive re-init)."""
    _observations.clear()


def _normalize_path(file_path: Any) -> str | None:
    """Resolve like the tools, then return one canonical local state key."""
    try:
        raw_path = os.fspath(file_path)
    except TypeError:
        return None
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        return None
    effective_path = resolve_path(raw_path)
    return os.path.realpath(os.path.abspath(effective_path))


def _path_details(tool_args: Any) -> tuple[str, str] | None:
    """Coerce a raw tool path exactly as downstream Pydantic validation does."""
    if not isinstance(tool_args, dict):
        return None
    try:
        file_path = _PATH_ADAPTER.validate_python(tool_args.get("file_path"))
    except ValidationError:
        return None
    normalized = _normalize_path(file_path)
    if normalized is None:
        return None
    return file_path, normalized


def _stat_version(path: str) -> Version:
    stat_result = os.stat(path)
    return stat_result.st_mtime_ns, stat_result.st_size


def _scope_observations(scope: ScopeKey) -> dict[str, Observation]:
    return _observations.setdefault(scope, {})


def _set_observation(
    scope: ScopeKey,
    normalized_path: str,
    observation: Observation,
) -> None:
    _scope_observations(scope)[normalized_path] = observation


def _get_observation(
    scope: ScopeKey,
    normalized_path: str,
) -> Observation | None:
    return _observations.get(scope, {}).get(normalized_path)


def _block(reason: str) -> dict[str, bool | str]:
    return {"blocked": True, "reason": reason}


def _stale_read(display_path: str) -> dict[str, bool | str]:
    return _block(
        f"STALE READ: '{display_path}' changed on disk since you last read it "
        "(external edit?). Call read_file again before editing."
    )


def _overwrite_requested(value: Any) -> bool:
    """Coerce raw overwrite values exactly like the downstream bool field."""
    try:
        return _OVERWRITE_ADAPTER.validate_python(value)
    except ValidationError:
        # Let the actual tool validator explain invalid values to the model.
        return False


def enforce(
    tool_name: str,
    tool_args: dict[str, Any],
    scope: ScopeKey,
) -> dict[str, bool | str] | None:
    """Return a model-actionable block decision, or ``None`` to allow."""
    if tool_name not in GUARDED_TOOLS:
        return None

    details = _path_details(tool_args)
    if details is None:
        return None
    display_path, normalized_path = details
    observation = _get_observation(scope, normalized_path)

    if tool_name in EDIT_TOOLS:
        if observation is None:
            return _block(
                f"READ-BEFORE-WRITE: '{display_path}' has not been read this "
                "session. Call read_file on it first, then retry your edit."
            )
        if observation.kind == "absent":
            return _block(
                f"'{display_path}' does not exist (you observed it missing "
                "earlier). Check the path with list_files or grep."
            )
        try:
            current_version = _stat_version(normalized_path)
        except FileNotFoundError:
            return _stale_read(display_path)
        if current_version != observation.version:
            return _stale_read(display_path)
        return None

    if not _overwrite_requested(tool_args.get("overwrite", False)):
        return None

    try:
        current_version = _stat_version(normalized_path)
    except FileNotFoundError:
        # No target exists to clobber, regardless of an older observation.
        return None

    if observation is None or observation.kind != "present":
        return _block(
            f"'{display_path}' already exists but hasn't been read this session. "
            "Call read_file first (or use replace_in_file for a targeted edit)."
        )
    if current_version != observation.version:
        return _stale_read(display_path)
    return None


def capture_mutation_snapshot(
    tool_args: dict[str, Any],
) -> MutationSnapshot | None:
    """Freeze a mutation target so post-call symlink drift cannot bless another."""
    details = _path_details(tool_args)
    if details is None:
        return None
    _, normalized_path = details
    return MutationSnapshot(normalized_path)


def capture_read_snapshot(tool_args: dict[str, Any]) -> ReadSnapshot | None:
    """Capture the tool-effective identity/version immediately before a read."""
    details = _path_details(tool_args)
    if details is None:
        return None
    _, normalized_path = details
    try:
        version = _stat_version(normalized_path)
    except FileNotFoundError:
        version = None
    return ReadSnapshot(normalized_path, version)


def _context_wrapped_result(result: str) -> dict[Any, Any] | None:
    """Recover core's structured result after hook-context string decoration."""
    if not result.startswith(_HOOK_CONTEXT_PREFIX):
        return None
    _, separator, payload = result.rpartition("\n\n")
    if not separator:
        return None

    try:
        parsed = ast.literal_eval(payload)
    except (SyntaxError, ValueError):
        # ReadFileOutput's string form ends in its final ``error=...`` field.
        if not payload.startswith("content=") or " num_tokens=" not in payload:
            return None
        _, error_separator, error_literal = payload.rpartition(" error=")
        if not error_separator:
            return None
        try:
            error = ast.literal_eval(error_literal)
        except (SyntaxError, ValueError):
            return None
        return {"error": error}
    return parsed if isinstance(parsed, dict) else None


def _result_dict(result: Any) -> dict[Any, Any] | None:
    """Return dict/Pydantic results, including core context-wrapped variants."""
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        return _context_wrapped_result(result)

    model_dump = getattr(result, "model_dump", None)
    if not callable(model_dump):
        return None
    try:
        dumped = model_dump(exclude_none=True)
    except TypeError:
        dumped = model_dump()
    return dumped if isinstance(dumped, dict) else None


def _result_indicates_not_found(result: dict[Any, Any], display_path: str) -> bool:
    """Match only the target-specific not-found forms emitted by read_file."""
    error = result.get("error")
    if not isinstance(error, str):
        return False
    stripped = error.strip()
    if stripped == "FILE NOT FOUND":
        return True
    return stripped == f"File {resolve_path(display_path)} does not exist"


def _record_present(
    scope: ScopeKey,
    normalized_path: str,
    read_snapshot: ReadSnapshot | None = None,
) -> None:
    """Best-effort stat and record a version the model could have observed."""
    try:
        version = _stat_version(normalized_path)
    except OSError:
        logger.warning(
            "Could not stat %s while recording a file observation",
            normalized_path,
            exc_info=True,
        )
        return

    if read_snapshot is not None and (
        read_snapshot.normalized_path != normalized_path
        or (read_snapshot.version is not None and read_snapshot.version != version)
    ):
        logger.warning(
            "File identity/version changed while read_file was running; "
            "observation skipped for %s",
            normalized_path,
        )
        return
    _set_observation(scope, normalized_path, Observation("present", version))


def record(
    tool_name: str,
    tool_args: dict[str, Any],
    result: Any,
    scope: ScopeKey,
    read_snapshot: ReadSnapshot | None = None,
    mutation_snapshot: MutationSnapshot | None = None,
) -> None:
    """Record authoritative reads and successful mutations for one scope."""
    if tool_name not in OBSERVATION_TOOLS:
        return

    details = _path_details(tool_args)
    if details is None:
        return
    display_path, normalized_path = details
    result_dict = _result_dict(result)
    if result_dict is None:
        return

    if tool_name == "read_file":
        if _result_indicates_not_found(result_dict, display_path):
            if (
                read_snapshot is not None
                and read_snapshot.normalized_path != normalized_path
            ):
                logger.warning(
                    "File identity changed while missing read_file was running; "
                    "absent observation skipped for %s",
                    normalized_path,
                )
                return
            _set_observation(scope, normalized_path, Observation("absent"))
            return
        if "error" not in result_dict or result_dict.get("error") is None:
            # Ranged reads count: even partial content authoritatively observed
            # this path and therefore supplies a fresh stat version.
            _record_present(scope, normalized_path, read_snapshot)
        return

    if not result_dict.get("success"):
        return
    if tool_name == "delete_file":
        _set_observation(scope, normalized_path, Observation("absent"))
        return

    mutation_path = (
        mutation_snapshot.normalized_path
        if mutation_snapshot is not None
        else normalized_path
    )
    if mutation_path != normalized_path:
        logger.warning(
            "Mutation target identity changed before observation; recording the "
            "pre-call target %s instead of %s",
            mutation_path,
            normalized_path,
        )
    _record_present(scope, mutation_path)


__all__ = [
    "EDIT_TOOLS",
    "GUARDED_TOOLS",
    "MUTATION_TOOLS",
    "OBSERVATION_TOOLS",
    "MutationSnapshot",
    "Observation",
    "ReadSnapshot",
    "ScopeKey",
    "Version",
    "_normalize_path",
    "_observations",
    "_reset_state",
    "capture_mutation_snapshot",
    "capture_read_snapshot",
    "enforce",
    "record",
]
