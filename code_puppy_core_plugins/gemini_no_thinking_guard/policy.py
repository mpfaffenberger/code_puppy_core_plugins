"""Pure policy for gemini_no_thinking_guard (no Code Puppy imports, unit-testable)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

DEFAULT_STEP_BACK_AFTER = 12
DEFAULT_FORCE_ANSWER_AFTER = 24

# Tools that change files. One of these resets the count, so a turn that is
# making edits is never stopped; only long stretches of reads/searches/queries are.
EDIT_TOOLS = frozenset(
    {"edit_file", "create_file", "replace_in_file", "delete_file", "delete_snippet"}
)

STEP_BACK_TEXT = (
    "[step back] This tool call was not run. You have made {calls} tool calls on this "
    "question without answering. Before any other tool call, write out: "
    "(1) the exact lookup the code performs for this value (file:line, and the "
    "qualifier/namespace, org, key and environment it uses); "
    "(2) for every row or value your answer relies on, its qualifier (or namespace), "
    "org and environment as shown in a query result you actually saw, not as a doc or "
    "note describes it. If you have not seen a query result showing them, make that "
    "query next. A row under a different qualifier, org or environment does not apply "
    "to this code, even if a doc says it does; "
    "(3) the answer that follows. If the exact lookup has no matching row, the answer "
    "is: not set where the code reads it, so the code's fallback applies (say what it "
    "is). If a doc or note claims otherwise, report it as a conflict for the user "
    "instead of investigating it. Then answer the user. Make at most a few more tool "
    "calls, and only for one specific missing fact."
)

FORCE_ANSWER_TEXT = (
    "[answer now] The tool budget for this question is used up; no more tools will "
    "run. Answer the user now from what you already have: the exact lookup the code "
    "makes, what the data store returned for it, the resulting answer (or the code's "
    "fallback), and any conflict with docs. Do not call any tool."
)


@dataclass(frozen=True)
class GuardSettings:
    """``0`` disables a step."""

    step_back_after: int = DEFAULT_STEP_BACK_AFTER
    force_answer_after: int = DEFAULT_FORCE_ANSWER_AFTER


def has_thinking_since_last_prompt(messages: Iterable[Any]) -> bool:
    """True if a model response after the latest user prompt carries thinking.

    Duck-typed over pydantic-ai messages: responses have ``kind == "response"``;
    a thinking part has ``part_kind == "thinking"`` and non-empty content or a
    signature. A request with a ``user-prompt`` part starts a new turn.
    """
    seen = False
    for message in messages:
        kind = getattr(message, "kind", None)
        parts = getattr(message, "parts", None) or []
        if kind == "request":
            if any(getattr(p, "part_kind", None) == "user-prompt" for p in parts):
                seen = False
        elif kind == "response":
            for part in parts:
                if getattr(part, "part_kind", None) == "thinking" and (
                    getattr(part, "content", None) or getattr(part, "signature", None)
                ):
                    seen = True
    return seen


@dataclass
class RunGuard:
    """Per-run state. ``thinking`` is sticky: once seen in the run, the guard stays out."""

    calls: int = 0
    thinking: bool = False
    stepped_back: bool = False

    def before_call(
        self, tool_name: str, thinking_now: bool, settings: GuardSettings
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Return ``(action, pre_tool_call result)``; action is None, "step_back" or "force"."""
        self.thinking = self.thinking or thinking_now
        if tool_name in EDIT_TOOLS:
            self.calls = 0
            self.stepped_back = False
            return None, None
        self.calls += 1
        if self.thinking:
            return None, None
        if settings.force_answer_after and self.calls > settings.force_answer_after:
            return "force", {"blocked": True, "error_message": FORCE_ANSWER_TEXT}
        if (
            settings.step_back_after
            and not self.stepped_back
            and self.calls > settings.step_back_after
        ):
            self.stepped_back = True
            text = STEP_BACK_TEXT.format(calls=self.calls - 1)
            return "step_back", {"blocked": True, "error_message": text}
        return None, None
