"""Wire emoji_filter into the runtime.

Three points of contact, all gated by ``config.is_enabled()``:

1. ``pre_tool_call`` callback — mutates the *args dict in-place* for file-write
   and shell tools before the tool actually runs. We only touch text destined
   for disk (new_str, content, payload) and the shell command string. Search
   strings (old_str / snippet) are left alone so matching doesn't silently
   break.

2. ``stream_event`` callback — strips emojis from the raw ``TextPart`` /
   ``TextPartDelta`` objects that core exposes at the stream seam.
   ``ThinkingPart`` / ``ThinkingPartDelta`` are deliberately untouched.

3. ``custom_command`` / ``custom_command_help`` — a tiny ``/emoji-filter``
   slash command so the user can flip the switch without editing puppy.cfg.

Failures here must never crash the app: every patch site is wrapped.
"""

from __future__ import annotations

import logging
from typing import Any

from code_puppy.callbacks import register_callback

from .config import is_enabled, set_enabled
from .stripper import strip_emojis

logger = logging.getLogger(__name__)

# Tool name → handler. Keeps the dispatch table flat and inspectable (no
# nested if/elif soup).  All handlers mutate ``args`` in place and return None.
_FILE_WRITE_TOOLS = {"create_file", "edit_file", "replace_in_file"}
_SHELL_TOOLS = {"agent_run_shell_command"}


def _strip_field(container: dict, key: str) -> bool:
    """Strip emojis from ``container[key]`` in place. Return True if it changed.

    One helper, one job. Used by every other strip-site so the
    "did anything actually change?" signal is honest and DRY.
    """
    val = container.get(key)
    if not isinstance(val, str) or not val:
        return False
    stripped = strip_emojis(val)
    if stripped == val:
        return False
    container[key] = stripped
    return True


def _filter_replacements(replacements: Any) -> int:
    """Strip emojis from each ``new_str`` in a replacements list (in place).

    Returns the number of items whose ``new_str`` was modified.
    """
    if not isinstance(replacements, list):
        return 0
    count = 0
    for item in replacements:
        if isinstance(item, dict) and _strip_field(item, "new_str"):
            count += 1
    return count


def _filter_edit_payload(payload: Any) -> list[str]:
    """Mutate an edit_file payload dict in place.

    edit_file accepts three payload shapes:
      * ContentPayload      → strip ``content``
      * ReplacementsPayload → strip each ``new_str``
      * DeleteSnippetPayload → leave alone (it's a search string)

    Returns a list of human-readable labels describing what was stripped
    (empty list = nothing changed).
    """
    if not isinstance(payload, dict):
        return []
    stripped: list[str] = []
    if _strip_field(payload, "content"):
        stripped.append("payload.content")
    if "replacements" in payload:
        n = _filter_replacements(payload["replacements"])
        if n:
            stripped.append(f"payload.replacements ({n} item{'s' if n != 1 else ''})")
    return stripped


# Notify the model when we tamper with its tool call so it stops emitting emojis.
# The framework (pydantic_patches._patched_call_tool) prepends the ``context_message``
# to the tool result as ``[hook context]\n{msg}``, which the model then reads.
_CONTEXT_MESSAGE_TEMPLATE = (
    "emoji_filter is ENABLED. Emojis were detected and stripped from "
    "`{tool_name}` arg(s): {fields}. This project does not allow emojis "
    "in file writes, shell commands, or assistant output \u2014 please "
    "omit them in future tool calls and responses."
)


def _on_pre_tool_call(
    tool_name: str, tool_args: dict, context: Any = None
) -> dict | None:
    """Strip emojis from file-write and shell tool args, and notify the model.

    Returns ``{"context_message": ...}`` when any emojis were detected so the
    framework can surface that fact to the model via the tool result. Returns
    ``None`` otherwise (no-op for the framework).
    """
    if not is_enabled() or not isinstance(tool_args, dict):
        return None

    stripped_fields: list[str] = []
    try:
        if tool_name == "create_file":
            if _strip_field(tool_args, "content"):
                stripped_fields.append("content")

        elif tool_name == "replace_in_file":
            n = _filter_replacements(tool_args.get("replacements"))
            if n:
                stripped_fields.append(
                    f"replacements ({n} item{'s' if n != 1 else ''})"
                )

        elif tool_name == "edit_file":
            stripped_fields.extend(_filter_edit_payload(tool_args.get("payload")))

        elif tool_name in _SHELL_TOOLS:
            if _strip_field(tool_args, "command"):
                stripped_fields.append("command")
    except Exception as exc:  # never block tool execution
        logger.debug("emoji_filter pre_tool_call failed: %s", exc)
        return None

    if not stripped_fields:
        return None

    return {
        "context_message": _CONTEXT_MESSAGE_TEMPLATE.format(
            tool_name=tool_name, fields=", ".join(stripped_fields)
        )
    }


# Streaming filter: mutate only the raw text parts core exposes through its
# public callback seam. This deliberately avoids patching pydantic-ai classes:
# message-part constructors and storage details are provider-facing internals,
# while ``stream_event`` is the core-owned presentation boundary.

_RENDER_WRAPPER_FLAG = "_emoji_filter_renderer_wrapped"


class _FilteringWriter:
    """Proxy a terminal writer while removing emoji from rendered text."""

    def __init__(self, target: Any) -> None:
        self._target = target

    def write(self, text: Any) -> Any:
        if is_enabled() and isinstance(text, str):
            text = strip_emojis(text)
        return self._target.write(text)

    def flush(self) -> Any:
        return self._target.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)


def _install_render_wrapper() -> None:
    """Wrap termflow's renderer output without changing model message classes.

    Core currently schedules ``stream_event`` callbacks asynchronously, while
    its text renderer consumes a delta synchronously. The callback remains
    useful for raw message/history mutation, but this writer proxy is the
    deterministic last mile for terminal output.
    """
    try:
        import termflow
    except Exception as exc:
        logger.debug("emoji_filter: termflow unavailable: %s", exc)
        return

    renderer = getattr(termflow, "Renderer", None)
    if renderer is None or getattr(renderer, _RENDER_WRAPPER_FLAG, False):
        return

    class EmojiFilteringRenderer(renderer):
        _emoji_filter_renderer_wrapped = True

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            if args and args[0] is not None:
                args = (_FilteringWriter(args[0]), *args[1:])
            elif kwargs.get("output") is not None:
                kwargs["output"] = _FilteringWriter(kwargs["output"])
            super().__init__(*args, **kwargs)

    termflow.Renderer = EmojiFilteringRenderer


def _strip_object_field(container: Any, key: str) -> bool:
    """Strip a string attribute in place, returning whether it changed."""
    current = getattr(container, key, None)
    if not isinstance(current, str) or not current:
        return False
    stripped = strip_emojis(current)
    if stripped == current:
        return False
    setattr(container, key, stripped)
    return True


def _strip_event_field(event_data: dict[str, Any], key: str) -> bool:
    """Strip a string event field used by core's sub-agent stream path."""
    current = event_data.get(key)
    if not isinstance(current, str) or not current:
        return False
    stripped = strip_emojis(current)
    if stripped == current:
        return False
    event_data[key] = stripped
    return True


def _on_stream_event(
    event_type: str,
    event_data: Any,
    agent_session_id: Any = None,
) -> None:
    """Strip emojis from streamed text parts without touching thinking parts.

    Core's main event handler includes the raw part/delta under ``part`` or
    ``delta``. Its sub-agent handler exposes the same text as string fields;
    support both shapes so this callback remains a presentation-only seam.
    """
    del agent_session_id
    if not is_enabled() or not isinstance(event_data, dict):
        return None

    try:
        from pydantic_ai.messages import TextPart, TextPartDelta

        if event_type == "part_start":
            part = event_data.get("part")
            if isinstance(part, TextPart):
                _strip_object_field(part, "content")
            elif event_data.get("part_type") == "TextPart":
                _strip_event_field(event_data, "content")
        elif event_type == "part_delta":
            delta = event_data.get("delta")
            if isinstance(delta, TextPartDelta):
                _strip_object_field(delta, "content_delta")
            elif event_data.get("delta_type") == "TextPartDelta":
                _strip_event_field(event_data, "content_delta")
    except Exception as exc:  # never break the stream
        logger.debug("emoji_filter stream callback failed: %s", exc)
    return None


# --- /emoji-filter slash command --------------------------------------------

_COMMAND_NAMES = {"emoji-filter", "emojifilter"}


def _custom_help():
    return [
        ("emoji-filter", "Show / toggle the emoji filter (on|off|status)"),
    ]


def _handle_command(command: str, name: str):
    if name not in _COMMAND_NAMES:
        return None

    from code_puppy.messaging import emit_info

    parts = command.split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) == 2 else "status"

    if arg in ("on", "enable", "enabled", "true", "1"):
        set_enabled(True)
        emit_info(
            "emoji_filter: ON (emojis will be stripped from outputs/file writes/shell)"
        )
        return True
    if arg in ("off", "disable", "disabled", "false", "0"):
        set_enabled(False)
        emit_info("emoji_filter: OFF (emojis pass through untouched)")
        return True
    if arg in ("status", ""):
        state = "ON" if is_enabled() else "OFF"
        emit_info(f"emoji_filter: {state}")
        return True

    emit_info("Usage: /emoji-filter [on|off|status]")
    return True


# --- Registration ------------------------------------------------------------

register_callback("startup", _install_render_wrapper)
register_callback("stream_event", _on_stream_event)
register_callback("pre_tool_call", _on_pre_tool_call)
register_callback("custom_command_help", _custom_help)
register_callback("custom_command", _handle_command)
