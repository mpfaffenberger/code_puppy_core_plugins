"""Contract tests for the pure helpers in composer_ext.py.

Wire-level and session-level behaviour is covered elsewhere; this file pins what
each helper returns or raises. New-core APIs are absent by default (old core);
tests opt in through the ``core`` fixture or the install_* helpers.
"""

from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest
from acp import RequestError
from acp.schema import (
    EmbeddedResourceContentBlock,
    ResourceContentBlock,
    TextContentBlock,
    TextResourceContents,
)

from code_puppy_core_plugins.acp import composer_ext as composer
from tests.acp_session_support import (
    FakeAgent,
    install_binary_media,
    install_pause_store,
    install_subagent_store,
)

CATALOG = "code_puppy.command_line.command_catalog"
REGISTRY = "code_puppy.command_line.command_registry"
PAUSE = "code_puppy.messaging.pause_controller"
SUBAGENTS = "code_puppy.tools.subagent_invocation"
MEDIA = "code_puppy.model_media"
CONFIG = "code_puppy.config"

VISION_TYPES = ["image/png", "image/jpeg"]
MEDIA_SUPPORT = {"vision-model": VISION_TYPES, "text-model": []}
VISION_META = {"codePuppy/binaryMediaTypes": VISION_TYPES}
EMPTY_META = {"codePuppy/binaryMediaTypes": []}
INTERRUPTED = "codePuppy/interrupted"


@pytest.fixture(autouse=True)
def old_core(monkeypatch: pytest.MonkeyPatch) -> None:
    # Real imports first: blocking code_puppy.config would break agent_tools,
    # and importing subagent_invocation first hits a circular import via it.
    importlib.import_module("code_puppy.tools.agent_tools")
    pause = importlib.import_module(PAUSE)
    subagents = importlib.import_module(SUBAGENTS)
    for name in (CATALOG, REGISTRY, MEDIA, CONFIG):
        monkeypatch.setitem(sys.modules, name, None)
    for attr in ("get_session_pause_controller", "drop_session_pause_controller"):
        monkeypatch.delattr(pause, attr, raising=False)
    for attr in ("peek_interrupted_subagents", "clear_interrupted_subagents"):
        monkeypatch.delattr(subagents, attr, raising=False)


@pytest.fixture
def core(monkeypatch: pytest.MonkeyPatch):
    def install(module_name: str, **attrs: Any) -> None:
        module = types.ModuleType(module_name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, module_name, module)

    return install


@pytest.fixture
def media(monkeypatch: pytest.MonkeyPatch) -> None:
    install_binary_media(monkeypatch, MEDIA_SUPPORT)


def _dump(models: list[Any]) -> list[dict[str, Any]]:
    return [m.model_dump(by_alias=True, exclude_none=True) for m in models]


def _meta(interactive: bool, aliases: list[str], category: str) -> dict:
    meta = {"interactive": interactive, "aliases": aliases, "category": category}
    return {"codePuppy/command": meta}


def _entry(name, description, hint="", aliases=(), category="core", interactive=False):
    return SimpleNamespace(
        name=name,
        description=description,
        hint=hint,
        aliases=aliases,
        category=category,
        interactive=interactive,
    )


def _session(model_name: str) -> SimpleNamespace:
    return SimpleNamespace(agent=FakeAgent(model_name))


def _broken_session() -> SimpleNamespace:
    def fail() -> str:
        raise RuntimeError("no model")

    return SimpleNamespace(agent=SimpleNamespace(get_model_name=fail))


# --- available_commands -----------------------------------------------------


def test_available_commands_new_core_full_contract(core) -> None:
    entries = [
        _entry("/help", "Show help", "", ("h", "?"), "general", False),
        _entry("model", "Switch model", "<name>", (), "config", True),
    ]
    core(CATALOG, list_commands=lambda: entries)

    assert _dump(composer.available_commands()) == [
        {
            "name": "help",
            "description": "Show help",
            "_meta": _meta(False, ["h", "?"], "general"),
        },
        {
            "name": "model",
            "description": "Switch model",
            "input": {"hint": "<name>"},
            "_meta": _meta(True, [], "config"),
        },
    ]


def test_available_commands_old_core_uses_registry_fallback(core) -> None:
    commands = [
        _entry("/quit", "Exit", aliases=["q"], category="session"),
        SimpleNamespace(name="/plain", description="No extras"),
    ]
    core(REGISTRY, get_unique_commands=lambda: commands)

    assert _dump(composer.available_commands()) == [
        {
            "name": "quit",
            "description": "Exit",
            "_meta": _meta(False, ["q"], "session"),
        },
        {
            "name": "plain",
            "description": "No extras",
            "_meta": _meta(False, [], "core"),
        },
    ]


@pytest.mark.parametrize("broken_catalog", [False, True])
def test_available_commands_without_sources_is_empty_and_errors_propagate(
    core, broken_catalog: bool
) -> None:
    if not broken_catalog:
        assert composer.available_commands() == []
        return

    def broken() -> list[Any]:
        raise RuntimeError("catalog exploded")

    core(CATALOG, list_commands=broken)
    with pytest.raises(RuntimeError, match="catalog exploded"):
        composer.available_commands()


# --- extract_steer_text -----------------------------------------------------

_TEXT = {"type": "text", "text": "hi"}
_TEXT_MODEL = TextContentBlock(type="text", text="hi")
_LINK = {"type": "resource_link", "uri": "file:///a.py", "name": "a.py"}
_LINK_MODEL = ResourceContentBlock(
    type="resource_link", uri="file:///a.py", name="a.py"
)
_LINK_TEXT = "[Referenced resource: a.py (file:///a.py)]"
_RESOURCE = {
    "type": "resource",
    "resource": {"uri": "file:///b.txt", "text": "body", "mimeType": "text/plain"},
}
_RESOURCE_MODEL = EmbeddedResourceContentBlock(
    type="resource",
    resource=TextResourceContents(
        uri="file:///b.txt", text="body", mime_type="text/plain"
    ),
)


@pytest.mark.parametrize(
    ("dict_blocks", "model_blocks", "expected"),
    [
        ([_TEXT], [_TEXT_MODEL], "hi"),
        ([_LINK], [_LINK_MODEL], _LINK_TEXT),
        (
            [_RESOURCE],
            [_RESOURCE_MODEL],
            "<context uri='file:///b.txt'>\nbody\n</context>",
        ),
        ([_TEXT, _LINK], [_TEXT_MODEL, _LINK_MODEL], f"hi\n\n{_LINK_TEXT}"),
    ],
)
def test_extract_steer_text_accepts_dict_and_model_forms(
    dict_blocks: list[Any], model_blocks: list[Any], expected: str
) -> None:
    assert composer.extract_steer_text(dict_blocks) == expected
    assert composer.extract_steer_text(model_blocks) == expected


@pytest.mark.parametrize(
    "blocks",
    [
        "hello",
        [],
        [_TEXT, {"type": "image", "data": "AAAA", "mimeType": "image/png"}],
        [{"type": "resource", "resource": {"uri": "x", "blob": "AAAA"}}],
        [{"type": "resource", "resource": {"uri": "x", "text": ""}}],
        [{"type": "text", "text": "   \n\t"}],
    ],
)
def test_extract_steer_text_rejects_unsupported_content(blocks: Any) -> None:
    assert composer.extract_steer_text(blocks) is None


# --- handle_ext_method and steer_session ------------------------------------


@pytest.mark.parametrize(
    ("method", "pause_api_installed", "reported"),
    [
        ("code_puppy/other", True, "_code_puppy/other"),
        (composer.STEER_METHOD, False, f"_{composer.STEER_METHOD}"),
    ],
)
async def test_ext_method_unknown_or_unsupported_steer_is_method_not_found(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    pause_api_installed: bool,
    reported: str,
) -> None:
    if pause_api_installed:
        install_pause_store(monkeypatch)

    with pytest.raises(RequestError) as caught:
        await composer.handle_ext_method(method, {"sessionId": "s", "prompt": []}, {})

    assert (caught.value.code, caught.value.data) == (-32601, {"method": reported})


@pytest.mark.parametrize(
    "params",
    [
        {"sessionId": "", "prompt": []},
        {"sessionId": 3, "prompt": []},
        {"sessionId": "s"},
        {"sessionId": "s", "prompt": "hi"},
    ],
)
async def test_ext_method_invalid_params_is_invalid_params(
    monkeypatch: pytest.MonkeyPatch, params: dict[str, Any]
) -> None:
    install_pause_store(monkeypatch)

    with pytest.raises(RequestError) as caught:
        await composer.handle_ext_method(composer.STEER_METHOD, params, {})

    assert (caught.value.code, caught.value.data) == (
        -32602,
        {"method": composer.STEER_METHOD},
    )


async def test_ext_method_unknown_session_reports_not_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_pause_store(monkeypatch)

    result = await composer.handle_ext_method(
        composer.STEER_METHOD, {"sessionId": "gone", "prompt": []}, {}
    )

    assert result == {"accepted": False, "reason": "not_running"}


def test_steer_session_queues_immediate_history_recorded_steer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = install_pause_store(monkeypatch)

    composer.steer_session("A", "one")

    assert store.controller_for("A").steers == [("one", "now", True)]


# --- interrupted_meta -------------------------------------------------------

_BAD_RECORD = {"agent_name": 5, "session_id": "A-bad"}
_OK_RECORD = {"agent_name": "planner", "session_id": "A-ok"}


def _interrupted(*entries: dict[str, Any]) -> dict[str, Any]:
    return {INTERRUPTED: list(entries)}


def test_interrupted_meta_scopes_per_session_and_never_drains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = install_subagent_store(monkeypatch)
    store.records = {
        "A": [{"agent_name": "explorer", "session_id": "A-sub-1", "saved_count": 3}],
        "B": [{"agent_name": "planner", "session_id": "B-sub-1"}],
    }

    assert composer.interrupted_meta("A") == _interrupted(
        {"agentName": "explorer", "subSessionId": "A-sub-1", "savedCount": 3}
    )
    assert composer.interrupted_meta("B") == _interrupted(
        {"agentName": "planner", "subSessionId": "B-sub-1", "savedCount": None}
    )
    assert composer.interrupted_meta("C") is None
    assert store.drain_calls == []
    assert store.clear_calls == []


def _store_with(records: dict[str, list[dict[str, Any]]]):
    def install(monkeypatch: pytest.MonkeyPatch) -> None:
        install_subagent_store(monkeypatch).records = records

    return install


def _raising_peek(monkeypatch: pytest.MonkeyPatch) -> None:
    def peek(session_id: str) -> list[Any]:
        raise RuntimeError("store unavailable")

    module = importlib.import_module(SUBAGENTS)
    monkeypatch.setattr(module, "peek_interrupted_subagents", peek, raising=False)


@pytest.mark.parametrize(
    ("install", "expected"),
    [
        (
            _store_with({"A": [_BAD_RECORD, _OK_RECORD]}),
            _interrupted(
                {"agentName": "planner", "subSessionId": "A-ok", "savedCount": None}
            ),
        ),
        (_store_with({}), None),
        (lambda monkeypatch: None, None),
        (_raising_peek, None),
    ],
)
def test_interrupted_meta_edge_cases(monkeypatch, install, expected) -> None:
    install(monkeypatch)

    assert composer.interrupted_meta("A") == expected


# --- binary media, session model and capabilities ---------------------------


@pytest.mark.parametrize(
    ("installed", "model", "expected"),
    [
        (True, "vision-model", VISION_TYPES),
        (True, "text-model", []),
        (True, "unknown-model", []),
        (True, None, []),
        (False, "vision-model", []),
    ],
)
def test_binary_media_by_model_on_new_and_old_core(
    request, installed: bool, model: str | None, expected: list[str]
) -> None:
    if installed:
        request.getfixturevalue("media")

    assert composer.binary_media_types(model) == expected
    assert composer.agent_binary_media_types(_session(model).agent) == expected
    meta = {"codePuppy/binaryMediaTypes": expected} if installed else None
    assert composer.binary_media_meta(model) == meta


def test_binary_media_lookup_errors_report_no_types(core) -> None:
    def boom(model_name: str) -> list[str]:
        raise RuntimeError("media lookup failed")

    core(MEDIA, supported_binary_media_types=boom)

    assert composer.binary_media_types("vision-model") == []
    assert composer.binary_media_meta("vision-model") == EMPTY_META


@pytest.mark.parametrize(
    ("installed", "session", "global_model", "expected"),
    [
        (True, _session("vision-model"), "text-model", VISION_META),
        (True, None, "vision-model", VISION_META),
        (True, _broken_session(), "vision-model", VISION_META),
        (True, None, None, EMPTY_META),
        (False, _session("vision-model"), "vision-model", None),
        (False, None, None, None),
    ],
)
def test_session_meta_prefers_session_model_then_global(
    request,
    core,
    installed: bool,
    session: Any,
    global_model: str | None,
    expected: Any,
) -> None:
    if installed:
        core(CONFIG, get_global_model_name=lambda: global_model)
        request.getfixturevalue("media")

    assert composer.session_meta(session) == expected


@pytest.mark.parametrize(
    ("has_pause", "has_media", "expected"),
    [
        (True, True, {"codePuppy/steer": True, **VISION_META}),
        (True, False, {"codePuppy/steer": True}),
        (False, True, VISION_META),
        (False, False, None),
    ],
)
def test_capabilities_meta_reflects_installed_core(
    request, monkeypatch, core, has_pause: bool, has_media: bool, expected: Any
) -> None:
    core(CONFIG, get_global_model_name=lambda: "vision-model")
    if has_pause:
        install_pause_store(monkeypatch)
    if has_media:
        request.getfixturevalue("media")

    assert composer.capabilities_meta() == expected


# --- release_session --------------------------------------------------------


@pytest.mark.parametrize(
    ("installed", "failing"),
    [(True, None), (True, "drop"), (True, "clear"), (False, None)],
)
def test_release_session_calls_both_cores_and_isolates_failures(
    core, installed: bool, failing: str | None
) -> None:
    calls: list[tuple[str, str]] = []

    def recorder(name: str):
        def call(session_id: str) -> None:
            calls.append((name, session_id))
            if name == failing:
                raise RuntimeError("boom")

        return call

    if installed:
        core(PAUSE, drop_session_pause_controller=recorder("drop"))
        core(SUBAGENTS, clear_interrupted_subagents=recorder("clear"))

    composer.release_session("A")

    assert sorted(calls) == ([("clear", "A"), ("drop", "A")] if installed else [])


# --- prompt_in_flight_error -------------------------------------------------


def test_prompt_in_flight_error_full_contract() -> None:
    error = composer.prompt_in_flight_error("sess-1")

    assert isinstance(error, RequestError)
    assert error.code == -32000
    assert str(error) == "Session already has a running prompt"
    assert error.data == {"sessionId": "sess-1", "reason": "prompt_in_flight"}
