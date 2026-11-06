"""Wire opt-in Logfire instrumentation into core's callback hooks."""

from __future__ import annotations

import logging

from code_puppy.callbacks import register_callback
from code_puppy.command_line.set_menu_schema import Setting, SettingsCategory
from code_puppy_core_plugins.plugin_settings import register_settings

from . import instrumentation, legacy_core

logger = logging.getLogger(__name__)


def _export_token_for_legacy_core() -> None:
    """Legacy cores configure Logfire from the env before ``startup`` fires."""
    try:
        if legacy_core.present() and instrumentation.opted_in():
            legacy_core.export_token(instrumentation.resolve_token(), overwrite=False)
    except Exception:
        logger.debug("Could not expose the Logfire token to core", exc_info=True)


def _on_startup() -> None:
    if legacy_core.present():
        return
    instrumentation.configure()


def _on_agent_run_end(
    _agent_name: str, _model_name: str, session_id: str | None = None, *_args, **_kw
) -> None:
    if session_id:
        instrumentation.clear_context(session_id)


def _register_event_stream_start() -> None:
    try:
        register_callback("agent_event_stream_start", instrumentation.capture_context)
    except ValueError:
        logger.debug("Core predates agent_event_stream_start; it captures context")


_export_token_for_legacy_core()
register_settings(
    SettingsCategory(
        "Features",
        (
            Setting(
                key=instrumentation.ENABLE_KEY,
                display_name="Logfire Instrumentation",
                description=(
                    "Send agent traces to your own Logfire project. Needs a "
                    "write token and the code-puppy-core-plugins[logfire] extra."
                ),
                type_hint="bool",
                effective_getter=instrumentation.opted_in,
                requires_restart=True,
            ),
        ),
    )
)
register_callback("startup", _on_startup)
register_callback("agent_run_cancel", instrumentation.emit_cancellation)
register_callback("agent_run_end", _on_agent_run_end)
_register_event_stream_start()
