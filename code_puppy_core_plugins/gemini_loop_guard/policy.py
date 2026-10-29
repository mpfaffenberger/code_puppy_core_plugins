"""Per-run streak of unproductive tool calls and what to do about it.

Levels, checked on every tool call (see the plugin README):

- L3 backstop: block once the streak reaches ``block_after`` (0 = off).
- L2 dedupe: skip an exact repeat of a call already in the current streak --
  nothing productive happened since, so it would return the same thing.
- L1 reflect: once per streak, show the model the facts of its streak and
  permission to stop. The tool still runs.
- Variation reflect: once per run of near-identical search/shell calls,
  whatever they returned (see ``variation``). Reflects only, never blocks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .outcomes import OUTCOME_LABELS, Outcome, classify
from .variation import VariationDetector

_ARGS_PREVIEW_CHARS = 80
_MAX_LISTED_CALLS = 5


@dataclass(frozen=True)
class GuardSettings:
    reflect_after: int = 3
    dedupe: bool = True
    block_after: int = 0
    variation_after: int = 3


@dataclass(frozen=True)
class CallRecord:
    tool_name: str
    args_key: str
    outcome: Outcome

    def describe(self) -> str:
        preview = self.args_key
        if len(preview) > _ARGS_PREVIEW_CHARS:
            preview = preview[: _ARGS_PREVIEW_CHARS - 3] + "..."
        return f"{self.tool_name} {preview} -> {OUTCOME_LABELS[self.outcome]}"


@dataclass
class RunTracker:
    """Everything the guard remembers about one agent run."""

    seen_evidence: set[str] = field(default_factory=set)
    streak: list[CallRecord] = field(default_factory=list)
    reflected: bool = False
    # Streak calls before this index may not be deduped: a call with an unknown
    # outcome (e.g. a file write another hook annotated) may have changed state.
    dedupe_from: int = 0
    variation: VariationDetector = field(default_factory=VariationDetector)

    def record_result(self, tool_name: str, tool_args: Any, result: Any) -> Outcome:
        key = args_key(tool_args)
        outcome = classify(
            result, self.seen_evidence, f"{tool_name}:{key}", _arg_values(tool_args)
        )
        if outcome is Outcome.PRODUCTIVE:
            self.streak.clear()
            self.reflected = False
            self.dedupe_from = 0
        elif outcome is Outcome.UNKNOWN:
            self.dedupe_from = len(self.streak)
        else:
            self.streak.append(CallRecord(tool_name, key, outcome))
        return outcome

    def before_call(
        self, tool_name: str, tool_args: Any, settings: GuardSettings
    ) -> dict[str, Any] | None:
        """Return a ``pre_tool_call`` hook result, or ``None`` to stay quiet."""
        if settings.block_after and len(self.streak) >= settings.block_after:
            return {"blocked": True, "error_message": _block_message(self.streak)}

        key = args_key(tool_args)
        if settings.dedupe:
            earlier = self._find_in_streak(tool_name, key)
            if earlier is not None:
                self.streak.append(CallRecord(tool_name, key, Outcome.REPEAT))
                return {"blocked": True, "tool_result": _dedupe_message(earlier)}

        look_alikes = self.variation.observe(
            tool_name, tool_args, settings.variation_after
        )
        streak_reflect = (
            settings.reflect_after
            and not self.reflected
            and len(self.streak) >= settings.reflect_after
        )
        if streak_reflect:
            self.reflected = True
        messages = []
        if streak_reflect:
            messages.append(_streak_text(self.streak))
        if look_alikes:
            messages.append(look_alikes)
        if messages:
            return {"context_message": "[loop check] " + "\n\n".join(messages)}
        return None

    def _find_in_streak(self, tool_name: str, key: str) -> CallRecord | None:
        return next(
            (
                record
                for record in self.streak[self.dedupe_from :]
                if record.tool_name == tool_name and record.args_key == key
            ),
            None,
        )


def args_key(tool_args: Any) -> str:
    """Stable, compact text form of a call's arguments."""
    try:
        return json.dumps(tool_args, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(tool_args)


def _arg_values(tool_args: Any) -> frozenset[str]:
    """The call's own argument values, which results often echo back."""
    if not isinstance(tool_args, dict):
        return frozenset()
    return frozenset(str(value) for value in tool_args.values() if value is not None)


def _listed_calls(streak: list[CallRecord]) -> str:
    recent = streak[-_MAX_LISTED_CALLS:]
    first_number = len(streak) - len(recent) + 1
    return "\n".join(
        f"  {number}. {record.describe()}"
        for number, record in enumerate(recent, start=first_number)
    )


def _streak_text(streak: list[CallRecord]) -> str:
    return (
        f"Your last {len(streak)} tool calls found nothing new:\n"
        f"{_listed_calls(streak)}\n"
        "More variations of the same search are unlikely to help. If what you "
        "are looking for may not exist, it is fine to say so and summarize "
        "what you checked."
    )


def _dedupe_message(earlier: CallRecord) -> str:
    return (
        f"[loop check] Not re-run: this exact {earlier.tool_name} call already "
        f"returned {OUTCOME_LABELS[earlier.outcome]} earlier in this task, and "
        "nothing has changed since. Running it again would give the same "
        "result. Try a different approach, or answer with what you have."
    )


def _block_message(streak: list[CallRecord]) -> str:
    return (
        f"{len(streak)} tool calls in a row found nothing new:\n"
        f"{_listed_calls(streak)}\n"
        "Stop calling tools and answer the user with what you found so far."
    )
