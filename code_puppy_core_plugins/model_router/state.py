"""Turn the prompt and pydantic-ai message history into a compact judge state.

The judge reads text only and is more accurate with less irrelevant context, so the
state carries the human request, a short system prompt excerpt, the tail of the
previous assistant reply, and the turn count. Tool calls and tool results are
never sent.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

SYSTEM_EXCERPT_CHARS = 800
ASSISTANT_EXCERPT_CHARS = 500


def truncate_middle(text: str, max_chars: int) -> str:
    """Keep the head (70%) and tail (30%) so the ask and trailing instructions survive."""
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.7)
    tail = max_chars - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n...[{omitted} characters omitted]...\n{text[len(text) - tail :]}"


def prompt_text(prompt: Any) -> str:
    """Text of a user prompt: a string, or the string members of a parts list."""
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, (list, tuple)):
        return "\n".join(p for p in prompt if isinstance(p, str))
    return ""


def _part_kind(part: Any) -> Optional[str]:
    return getattr(part, "part_kind", None)


def _part_text(part: Any) -> str:
    content = getattr(part, "content", None)
    return prompt_text(content) if not isinstance(content, str) else content


def _parts(message: Any) -> Iterable[Any]:
    parts = getattr(message, "parts", None)
    return parts if isinstance(parts, (list, tuple)) else ()


def build_state(
    prompt: Any, messages: List[Any], max_request_chars: int
) -> Optional[Dict[str, Any]]:
    """Return the state object for the judge, or None when there is nothing to judge."""
    request = prompt_text(prompt).strip()
    if not request:
        return None

    system_chunks: List[str] = []
    last_assistant = ""
    user_turns = 0
    for message in messages or []:
        for part in _parts(message):
            kind = _part_kind(part)
            if kind == "system-prompt":
                text = _part_text(part).strip()
                if text:
                    system_chunks.append(text)
            elif kind == "user-prompt":
                user_turns += 1
            elif kind == "text":
                text = _part_text(part).strip()
                if text:
                    last_assistant = text

    state: Dict[str, Any] = {
        "request": truncate_middle(request, max_request_chars),
        "conversation_turns": user_turns + 1,
    }
    system_text = "\n".join(system_chunks)
    if system_text:
        state["system_prompt_excerpt"] = system_text[:SYSTEM_EXCERPT_CHARS]
    if last_assistant:
        state["prior_assistant_excerpt"] = last_assistant[-ASSISTANT_EXCERPT_CHARS:]
    return state
