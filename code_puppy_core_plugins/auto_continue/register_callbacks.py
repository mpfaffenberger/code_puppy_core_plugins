"""Register bounded automatic continuation after routine permission requests."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from code_puppy.callbacks import register_callback
from code_puppy.command_line.set_menu_schema import Setting, SettingsCategory
from code_puppy.config import get_auto_continue_model_name
from code_puppy_core_plugins.plugin_settings import register_settings

from .classifier import classify

_COMMAND = "auto-continue"
_ENABLED_KEY = "auto_continue_enabled"
_MAX_CONTINUATIONS = 5
_continuation_count: ContextVar[int] = ContextVar(
    "auto_continue_continuation_count", default=0
)


def _is_enabled() -> bool:
    from code_puppy.config import get_truthy_bool_value

    return get_truthy_bool_value(_ENABLED_KEY, True)


def _set_enabled(enabled: bool) -> None:
    from code_puppy.config import set_value

    set_value(_ENABLED_KEY, "true" if enabled else "false")


def _reset_count() -> None:
    _continuation_count.set(0)


def _emit_status() -> None:
    from code_puppy.config import get_auto_continue_model_name
    from code_puppy.messaging import emit_info

    state = "enabled" if _is_enabled() else "disabled"
    model = get_auto_continue_model_name() or "not configured"
    emit_info(
        f"Auto-continue is {state}; classifier model: {model}; "
        f"maximum automatic continuations: {_MAX_CONTINUATIONS}."
    )


def _handle_custom_command(command: str, name: str) -> bool | None:
    if name != _COMMAND:
        return None

    from code_puppy.messaging import emit_error, emit_success

    tokens = command.strip().split()
    action = tokens[1].lower() if len(tokens) > 1 else "status"
    if action == "enable":
        _reset_count()
        _set_enabled(True)
        emit_success("Auto-continue enabled.")
    elif action == "disable":
        _reset_count()
        _set_enabled(False)
        emit_success("Auto-continue disabled.")
    elif action == "status":
        _emit_status()
    else:
        emit_error("Usage: /auto-continue [enable | disable | status]")
    return True


def _on_interactive_turn_cancel(_prompt: str, *, reason: str = "cancelled") -> None:
    """End the current continuation chain after Ctrl+C or task cancellation."""
    _reset_count()


def _custom_help() -> list[tuple[str, str]]:
    return [(_COMMAND, "Enable, disable, or inspect automatic continuation")]


def _response_text(result: Any) -> str | None:
    if result is None:
        return None
    output = getattr(result, "output", result)
    return output if isinstance(output, str) else None


async def _on_interactive_turn_end(
    agent: Any,
    prompt: str,
    result: Any = None,
    *,
    success: bool = True,
    error: BaseException | None = None,
) -> dict[str, str] | None:
    """Continue at most five times; reset the counter whenever a chain stops."""
    response = _response_text(result)
    if not _is_enabled() or not success or error is not None or not response:
        _reset_count()
        return None

    count = _continuation_count.get()
    if count >= _MAX_CONTINUATIONS:
        from code_puppy.messaging import emit_info

        emit_info(
            f"Auto-continue stopped after {_MAX_CONTINUATIONS} automatic continuations."
        )
        _reset_count()
        return None

    approval = await classify(response)
    if approval is None:
        _reset_count()
        return None

    from code_puppy.messaging import emit_info

    _continuation_count.set(count + 1)
    emit_info(f"Auto-continue: submitting '{approval}'.")
    return {"prompt": approval}


register_callback("interactive_turn_end", _on_interactive_turn_end)
register_callback("interactive_turn_cancel", _on_interactive_turn_cancel)
register_callback("custom_command", _handle_custom_command)
register_callback("custom_command_help", _custom_help)
register_settings(
    SettingsCategory(
        "Model",
        (
            Setting(
                key="auto_continue_model",
                display_name="Auto-Continue Model",
                description=(
                    "Model that decides whether a permission request is routine "
                    "enough to auto-continue. Blank follows the active model."
                ),
                type_hint="string",
                effective_getter=get_auto_continue_model_name,
            ),
        ),
    )
)


__all__ = [
    "_custom_help",
    "_handle_custom_command",
    "_is_enabled",
    "_on_interactive_turn_cancel",
    "_on_interactive_turn_end",
]
