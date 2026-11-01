"""Composer wire features for the ACP agent.

Covers the steer ext-method, advertised slash-command inputs, binary-media
capabilities, and interrupted-subagent metadata. Every new-core API is imported
lazily through ``_core_attr`` so an older installed core keeps working and only
advertises less; callers never see the difference beyond missing keys.

Called by: agent.py :: CodePuppyAgent :: ext_method(), _announce_commands(),
    new_session(), load_session(), resume_session(), set_config_option(),
    close_session(); capabilities.py :: agent_capabilities();
    session.py :: ACPSession :: steer(), _run_turn(), prompt()
Depends on: session.py :: ACPSession.steer() (sync, returns {"accepted", "reason"?});
    content.py :: parse_prompt()
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Mapping, Optional

from acp import RequestError
from acp.schema import (
    AvailableCommand,
    EmbeddedResourceContentBlock,
    ResourceContentBlock,
    TextContentBlock,
    TextResourceContents,
    UnstructuredCommandInput,
)
from pydantic import TypeAdapter, ValidationError

from code_puppy_core_plugins.acp import content

logger = logging.getLogger(__name__)

STEER_METHOD = "code_puppy/session/steer"
STEER_META_KEY = "codePuppy/steer"
BINARY_MEDIA_META_KEY = "codePuppy/binaryMediaTypes"
COMMAND_META_KEY = "codePuppy/command"
INTERRUPTED_META_KEY = "codePuppy/interrupted"
PROMPT_IN_FLIGHT_CODE = -32000

_CATALOG = "code_puppy.command_line.command_catalog"
_REGISTRY = "code_puppy.command_line.command_registry"
_PAUSE = "code_puppy.messaging.pause_controller"
_SUBAGENTS = "code_puppy.tools.subagent_invocation"
_MEDIA = "code_puppy.model_media"
_CONFIG = "code_puppy.config"

_BLOCK_ADAPTER: TypeAdapter[Any] = TypeAdapter(
    TextContentBlock | ResourceContentBlock | EmbeddedResourceContentBlock
)


def _core_attr(module_name: str, attr: str) -> Any:
    """Return ``module_name.attr`` or None when the installed core predates it."""
    try:
        return getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError):
        return None


def available_commands() -> list[AvailableCommand]:
    """Advertise slash commands; falls back to the legacy registry on old cores."""
    list_commands = _core_attr(_CATALOG, "list_commands")
    if list_commands is not None:
        return [
            _command(
                entry.name,
                entry.description,
                hint=entry.hint,
                interactive=entry.interactive,
                aliases=list(entry.aliases),
                category=entry.category,
            )
            for entry in list_commands()
        ]
    get_unique_commands = _core_attr(_REGISTRY, "get_unique_commands")
    if get_unique_commands is None:
        return []
    return [
        _command(
            cmd.name,
            cmd.description,
            hint="",
            interactive=False,
            aliases=list(getattr(cmd, "aliases", None) or []),
            category=getattr(cmd, "category", None) or "core",
        )
        for cmd in get_unique_commands()
    ]


def _command(
    name: str,
    description: str,
    *,
    hint: str,
    interactive: bool,
    aliases: list[str],
    category: str,
) -> AvailableCommand:
    return AvailableCommand(
        name=name.removeprefix("/"),
        description=description,
        input=UnstructuredCommandInput(hint=hint) if hint else None,
        field_meta={
            COMMAND_META_KEY: {
                "interactive": interactive,
                "aliases": aliases,
                "category": category,
            }
        },
    )


def steer_supported() -> bool:
    return _core_attr(_PAUSE, "get_session_pause_controller") is not None


def extract_steer_text(blocks: Any) -> Optional[str]:
    """Return steer text from raw prompt blocks, or None if unsupported.

    Only text, resource_link and text-resource blocks are steerable; anything
    else (image, audio, blob resource, unknown) makes the whole steer unsupported.
    """
    if not isinstance(blocks, list) or not blocks:
        return None
    try:
        models = [_BLOCK_ADAPTER.validate_python(block) for block in blocks]
    except ValidationError:
        return None
    if not all(_is_steerable(model) for model in models):
        return None
    text = content.parse_prompt(models).text
    return text if text.strip() else None


def _is_steerable(block: Any) -> bool:
    if isinstance(block, EmbeddedResourceContentBlock):
        resource = block.resource
        return isinstance(resource, TextResourceContents) and bool(resource.text)
    return True


# Editor-owned input must not land in the user's terminal command history.
def steer_session(session_id: str, text: str) -> None:
    """Queue ``text`` as an immediate steer; caller must check steer_supported()."""
    get_controller = _core_attr(_PAUSE, "get_session_pause_controller")
    get_controller(session_id).request_steer(text, mode="now", history_recorded=True)


async def handle_ext_method(
    method: str, params: dict[str, Any], sessions: Mapping[str, Any]
) -> dict[str, Any]:
    if method != STEER_METHOD or not steer_supported():
        raise RequestError.method_not_found(f"_{method}")
    session_id = params.get("sessionId")
    prompt = params.get("prompt")
    if (
        not isinstance(session_id, str)
        or not session_id
        or not isinstance(prompt, list)
    ):
        raise RequestError.invalid_params({"method": method})
    session = sessions.get(session_id)
    if session is None:
        return {"accepted": False, "reason": "not_running"}
    return session.steer(prompt)


def _supported_binary_media() -> Any:
    return _core_attr(_MEDIA, "supported_binary_media_types")


def binary_media_types(model_name: str | None) -> list[str]:
    supported = _supported_binary_media()
    if supported is None or not model_name:
        return []
    try:
        return list(supported(model_name))
    except Exception as exc:  # noqa: BLE001
        logger.debug("ACP: binary media lookup failed (%s)", type(exc).__name__)
        return []


def agent_binary_media_types(agent: Any) -> list[str]:
    return binary_media_types(_agent_model_name(agent))


def binary_media_meta(model_name: str | None) -> dict[str, list[str]] | None:
    if _supported_binary_media() is None:
        return None
    return {BINARY_MEDIA_META_KEY: binary_media_types(model_name)}


def session_meta(session: Any | None) -> dict[str, list[str]] | None:
    model_name = None
    if session is not None:
        model_name = _agent_model_name(getattr(session, "agent", None))
    return binary_media_meta(model_name or _global_model_name())


def capabilities_meta() -> dict[str, Any] | None:
    meta: dict[str, Any] = {}
    if steer_supported():
        meta[STEER_META_KEY] = True
    binary = binary_media_meta(_global_model_name())
    if binary is not None:
        meta.update(binary)
    return meta or None


def interrupted_meta(session_id: str) -> dict[str, list[dict[str, Any]]] | None:
    """Report interrupted subagents without draining them."""
    peek = _core_attr(_SUBAGENTS, "peek_interrupted_subagents")
    if peek is None:
        return None
    try:
        records = peek(session_id) or []
    except Exception as exc:  # noqa: BLE001
        logger.debug("ACP: interrupted subagent peek failed (%s)", type(exc).__name__)
        return None
    entries = [
        entry
        for entry in (_interrupted_entry(record) for record in records)
        if entry is not None
    ]
    return {INTERRUPTED_META_KEY: entries} if entries else None


def _interrupted_entry(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    agent_name = record.get("agent_name")
    sub_session_id = record.get("session_id")
    if not isinstance(agent_name, str) or not isinstance(sub_session_id, str):
        return None
    return {
        "agentName": agent_name,
        "subSessionId": sub_session_id,
        "savedCount": record.get("saved_count"),
    }


def release_session(session_id: str) -> None:
    _call_guarded(_PAUSE, "drop_session_pause_controller", session_id)
    _call_guarded(_SUBAGENTS, "clear_interrupted_subagents", session_id)


def prompt_in_flight_error(session_id: str) -> RequestError:
    return RequestError(
        PROMPT_IN_FLIGHT_CODE,
        "Session already has a running prompt",
        {"sessionId": session_id, "reason": "prompt_in_flight"},
    )


def _call_guarded(module_name: str, attr: str, *args: Any) -> None:
    func = _core_attr(module_name, attr)
    if func is None:
        return
    try:
        func(*args)
    except Exception as exc:  # noqa: BLE001
        logger.debug("ACP: %s failed (%s)", attr, type(exc).__name__)


def _agent_model_name(agent: Any) -> str | None:
    try:
        return agent.get_model_name()
    except Exception as exc:  # noqa: BLE001
        logger.debug("ACP: agent model name unavailable (%s)", type(exc).__name__)
        return None


def _global_model_name() -> str | None:
    get_global = _core_attr(_CONFIG, "get_global_model_name")
    if get_global is None:
        return None
    try:
        return get_global()
    except Exception as exc:  # noqa: BLE001
        logger.debug("ACP: global model name unavailable (%s)", type(exc).__name__)
        return None
