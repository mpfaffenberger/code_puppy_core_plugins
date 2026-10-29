"""Judge whether a tool result told the model anything it had not seen yet.

A call is *productive* when its result contains at least one piece of
evidence (a match, a file, a chunk of output) not seen earlier in the run.
Raw call counts are deliberately ignored: a 30-file refactor is a long run of
productive calls, while three re-worded greps over the same files are not.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from enum import Enum
from typing import Any

from pydantic_ai.messages import ToolReturn

# Core prefixes every pre_tool_call ``context_message`` like this and then
# stringifies the result, so the structured tool output is no longer available.
HOOK_CONTEXT_PREFIX = "[hook context]"

# Large outputs are replaced by a preview that names a fresh storage handle on
# every call. The handle is not evidence; the preview is.
SPILL_PREFIX = "[Tool output too large"
_SPILL_HANDLE = re.compile(r"handle=?\s*'[^']*'")

# Values that differ between identical calls, or carry no evidence themselves.
_IGNORED_KEYS = frozenset(
    {
        "error",
        "execution_time",
        "duration_ms",
        "timestamp",
        "pid",
        "log_file",
        "exit_code",
        "num_tokens",
    }
)


class Outcome(str, Enum):
    PRODUCTIVE = "productive"
    EMPTY = "empty"
    ERROR = "error"
    REPEAT = "repeat"
    UNKNOWN = "unknown"


OUTCOME_LABELS = {
    Outcome.EMPTY: "no results",
    Outcome.ERROR: "an error",
    Outcome.REPEAT: "only results already seen",
}


def classify(
    result: Any,
    seen: set[str],
    call_key: str = "",
    echoed: frozenset[str] = frozenset(),
) -> Outcome:
    """Classify ``result`` and add its evidence to ``seen``.

    ``call_key`` identifies the call (tool + args). List items such as grep
    matches are evidence on their own, so a re-worded search that finds the
    same lines is a repeat. Everything else (file contents, shell output, edit
    confirmations) is evidence only together with the call that produced it,
    so two different edits whose confirmations read alike both count.
    ``echoed`` holds the call's own argument values: a result field that just
    repeats one (a shell result's ``command``) tells the model nothing.
    """
    if isinstance(result, str) and result.startswith(HOOK_CONTEXT_PREFIX):
        return Outcome.UNKNOWN
    data = _plain(result)
    evidence = _evidence(data, call_key, echoed)
    new_evidence = evidence - seen
    seen.update(evidence)
    if new_evidence:
        return Outcome.PRODUCTIVE
    if _is_error(data):
        return Outcome.ERROR
    return Outcome.REPEAT if evidence else Outcome.EMPTY


def _plain(result: Any) -> Any:
    """Turn pydantic models, dataclasses and ToolReturns into plain data."""
    if isinstance(result, ToolReturn):
        return _plain(result.return_value)
    model_dump = getattr(result, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(mode="json")
        except Exception:
            return str(result)
    if dataclasses.is_dataclass(result) and not isinstance(result, type):
        return dataclasses.asdict(result)
    return result


def _is_error(data: Any) -> bool:
    if isinstance(data, dict):
        return bool(data.get("error"))
    return isinstance(data, str) and data.lstrip().upper().startswith("ERROR")


def _evidence(data: Any, call_key: str, echoed: frozenset[str]) -> set[str]:
    """Fingerprints of the facts a result carries; empty means no results."""
    if data is None:
        return set()
    if isinstance(data, list):
        return {_fingerprint(item) for item in data}
    if isinstance(data, dict):
        evidence: set[str] = set()
        scalars: dict[str, Any] = {}
        for key, value in data.items():
            if key in _IGNORED_KEYS or value is None or isinstance(value, bool):
                continue
            if isinstance(value, list):
                evidence.update(_fingerprint(item) for item in value)
            elif value not in ("", {}) and str(value) not in echoed:
                scalars[key] = value
        if scalars:
            evidence.add(_fingerprint([call_key, scalars]))
        return evidence
    text = str(data).strip()
    if text.startswith(SPILL_PREFIX):
        # A preview of a large output: the same output is a repeat whichever
        # call produced it, as with list items.
        return {_fingerprint(_SPILL_HANDLE.sub("handle", text))}
    return {_fingerprint([call_key, text])} if text else set()


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()
