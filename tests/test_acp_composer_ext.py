"""Contract tests for ACP composer wire features (composer_ext.py).

Every new-core API is simulated by injecting fake modules into ``sys.modules``.
An autouse fixture blocks all of them first, so the default state is the old
core regardless of what is installed.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from acp import RequestError
from acp.schema import (
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    ResourceContentBlock,
    TextContentBlock,
    TextResourceContents,
)

from code_puppy_core_plugins.acp import composer_ext as composer

CATALOG = "code_puppy.command_line.command_catalog"
REGISTRY = "code_puppy.command_line.command_registry"
PAUSE = "code_puppy.messaging.pause_controller"
SUBAGENTS = "code_puppy.tools.subagent_invocation"
MEDIA = "code_puppy.model_media"
CONFIG = "code_puppy.config"
ALL_NEW_CORE_MODULES = (CATALOG, REGISTRY, PAUSE, SUBAGENTS, MEDIA, CONFIG)

VISION_TYPES = ["image/png", "image/jpeg"]
VISION_META = {"codePuppy/binaryMediaTypes": VISION_TYPES}
EMPTY_META = {"codePuppy/binaryMediaTypes": []}


@dataclass(frozen=True)
class CommandEntry:
    name: str
    description: str
    hint: str
    aliases: tuple[str, ...]
    category: str
    interactive: bool


class FakeController:
    def __init__(self) -> None:
        self.steers: list[tuple[str, str, bool]] = []

    def request_steer(
        self, text: str, mode: str = "now", *, history_recorded: bool = False
    ) -> None:
        self.steers.append((text, mode, history_recorded))


class FakeAgent:
    def __init__(self, model_name: Any) -> None:
        self._model_name = model_name

    def get_model_name(self) -> Any:
        if isinstance(self._model_name, Exception):
            raise self._model_name
        return self._model_name


class FakeSession:
    def __init__(self, result: Any = None) -> None:
        self.received: list[Any] = []
        self._result = result

    def steer(self, prompt: list[Any]) -> Any:
        self.received.append(prompt)
        return self._result


@pytest.fixture(autouse=True)
def old_core(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ALL_NEW_CORE_MODULES:
        monkeypatch.setitem(sys.modules, name, None)


@pytest.fixture
def core(monkeypatch: pytest.MonkeyPatch):
    def install(module_name: str, **attrs: Any) -> types.ModuleType:
        module = types.ModuleType(module_name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, module_name, module)
        return module

    return install


def _dump(models: list[Any]) -> list[dict[str, Any]]:
    return [m.model_dump(by_alias=True, exclude_none=True) for m in models]


def _command_meta(interactive: bool, aliases: list[str], category: str) -> dict:
    meta = {"interactive": interactive, "aliases": aliases, "category": category}
    return {"codePuppy/command": meta}


# --- (a) available_commands -------------------------------------------------


def test_available_commands_new_core_full_contract(core) -> None:
    entries = [
        CommandEntry(
            name="/help",
            description="Show help",
            hint="",
            aliases=("h", "?"),
            category="general",
            interactive=False,
        ),
        CommandEntry(
            name="model",
            description="Switch model",
            hint="<name>",
            aliases=(),
            category="config",
            interactive=True,
        ),
    ]
    core(CATALOG, list_commands=lambda: entries)

    assert _dump(composer.available_commands()) == [
        {
            "name": "help",
            "description": "Show help",
            "_meta": _command_meta(False, ["h", "?"], "general"),
        },
        {
            "name": "model",
            "description": "Switch model",
            "input": {"hint": "<name>"},
            "_meta": _command_meta(True, [], "config"),
        },
    ]


def test_available_commands_propagates_catalog_errors(core) -> None:
    def broken() -> list[Any]:
        raise RuntimeError("catalog exploded")

    core(CATALOG, list_commands=broken)

    with pytest.raises(RuntimeError, match="catalog exploded"):
        composer.available_commands()


def test_available_commands_old_core_uses_registry_fallback(core) -> None:
    commands = [
        SimpleNamespace(
            name="/quit", description="Exit", aliases=["q"], category="session"
        ),
        SimpleNamespace(name="/plain", description="No extras"),
    ]
    core(REGISTRY, get_unique_commands=lambda: commands)

    assert _dump(composer.available_commands()) == [
        {
            "name": "quit",
            "description": "Exit",
            "_meta": _command_meta(False, ["q"], "session"),
        },
        {
            "name": "plain",
            "description": "No extras",
            "_meta": _command_meta(False, [], "core"),
        },
    ]


def test_available_commands_empty_when_no_catalog_and_no_registry() -> None:
    assert composer.available_commands() == []


# --- (b) extract_steer_text -------------------------------------------------

_LINK = {"type": "resource_link", "uri": "file:///a.py", "name": "a.py"}
_TEXT_RESOURCE = {
    "type": "resource",
    "resource": {"uri": "file:///b.txt", "text": "body", "mimeType": "text/plain"},
}


@pytest.mark.parametrize(
    ("dict_blocks", "model_blocks", "expected"),
    [
        (
            [{"type": "text", "text": "hi"}],
            [TextContentBlock(type="text", text="hi")],
            "hi",
        ),
        (
            [_LINK],
            [
                ResourceContentBlock(
                    type="resource_link", uri="file:///a.py", name="a.py"
                )
            ],
            "[Referenced resource: a.py (file:///a.py)]",
        ),
        (
            [_TEXT_RESOURCE],
            [
                EmbeddedResourceContentBlock(
                    type="resource",
                    resource=TextResourceContents(
                        uri="file:///b.txt", text="body", mime_type="text/plain"
                    ),
                )
            ],
            "<context uri='file:///b.txt'>\nbody\n</context>",
        ),
        (
            [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}],
            [
                TextContentBlock(type="text", text="one"),
                TextContentBlock(type="text", text="two"),
            ],
            "one\n\ntwo",
        ),
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
        [{"type": "image", "data": "AAAA", "mimeType": "image/png"}],
        [{"type": "audio", "data": "AAAA", "mimeType": "audio/wav"}],
        [{"type": "resource", "resource": {"uri": "x", "blob": "AAAA"}}],
        [{"type": "resource", "resource": {"uri": "x", "text": ""}}],
        [{"type": "mystery", "text": "x"}],
        [{"type": "text", "text": "ok"}, {"type": "image", "data": "AAAA"}],
        [{"type": "text", "text": "   \n\t"}],
        [],
        "hello",
        None,
        ["raw string"],
    ],
)
def test_extract_steer_text_rejects_unsupported_content(blocks: Any) -> None:
    assert composer.extract_steer_text(blocks) is None


def test_extract_steer_text_rejects_image_model_instance() -> None:
    image = ImageContentBlock(type="image", data="AAAA", mime_type="image/png")

    assert composer.extract_steer_text([image]) is None


# --- (c) handle_ext_method --------------------------------------------------


def _steer_core(core) -> None:
    core(PAUSE, get_session_pause_controller=lambda session_id: FakeController())


async def test_ext_method_unknown_method_is_method_not_found(core) -> None:
    _steer_core(core)

    with pytest.raises(RequestError) as caught:
        await composer.handle_ext_method("code_puppy/other", {}, {})

    assert (caught.value.code, caught.value.data) == (
        -32601,
        {"method": "_code_puppy/other"},
    )


async def test_ext_method_steer_without_pause_api_is_method_not_found() -> None:
    with pytest.raises(RequestError) as caught:
        await composer.handle_ext_method(
            composer.STEER_METHOD, {"sessionId": "s", "prompt": []}, {}
        )

    assert (caught.value.code, caught.value.data) == (
        -32601,
        {"method": f"_{composer.STEER_METHOD}"},
    )


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"prompt": []},
        {"sessionId": "", "prompt": []},
        {"sessionId": 3, "prompt": []},
        {"sessionId": "s"},
        {"sessionId": "s", "prompt": "hi"},
        {"sessionId": "s", "prompt": {}},
    ],
)
async def test_ext_method_invalid_params_is_invalid_params(core, params) -> None:
    _steer_core(core)

    with pytest.raises(RequestError) as caught:
        await composer.handle_ext_method(composer.STEER_METHOD, params, {})

    assert (caught.value.code, caught.value.data) == (
        -32602,
        {"method": composer.STEER_METHOD},
    )


async def test_ext_method_unknown_session_reports_not_running(core) -> None:
    _steer_core(core)

    result = await composer.handle_ext_method(
        composer.STEER_METHOD, {"sessionId": "gone", "prompt": []}, {}
    )

    assert result == {"accepted": False, "reason": "not_running"}


@pytest.mark.parametrize(
    "session_result",
    [{"accepted": True}, {"accepted": False, "reason": "busy"}],
)
async def test_ext_method_known_session_returns_steer_result(
    core, session_result: dict[str, Any]
) -> None:
    _steer_core(core)
    session = FakeSession(result=session_result)
    prompt = [{"type": "text", "text": "change course"}]

    result = await composer.handle_ext_method(
        composer.STEER_METHOD, {"sessionId": "s1", "prompt": prompt}, {"s1": session}
    )

    assert result == session_result
    assert session.received == [prompt]


# --- (d) steer_session isolation -------------------------------------------


def test_steer_session_routes_each_session_to_its_own_controller(core) -> None:
    controllers = {"A": FakeController(), "B": FakeController()}
    core(PAUSE, get_session_pause_controller=lambda sid: controllers[sid])

    composer.steer_session("A", "one")
    assert controllers["A"].steers == [("one", "now", True)]
    assert controllers["B"].steers == []

    composer.steer_session("B", "two")
    composer.steer_session("B", "three")
    assert controllers["A"].steers == [("one", "now", True)]
    assert controllers["B"].steers == [("two", "now", True), ("three", "now", True)]


# --- (e) interrupted_meta ---------------------------------------------------


@pytest.fixture
def interrupted_store(core) -> tuple[dict[str, list[Any]], list[str]]:
    store: dict[str, list[Any]] = {
        "A": [{"agent_name": "explorer", "session_id": "A-sub-1", "saved_count": 3}],
        "B": [
            "not a dict",
            {"agent_name": 5, "session_id": "B-bad"},
            {"agent_name": "planner", "session_id": "B-sub-1"},
        ],
    }
    clears: list[str] = []

    def peek(key: str | None = None) -> list[Any]:
        if key is None:
            return [record for records in store.values() for record in records]
        return list(store.get(key, []))

    core(
        SUBAGENTS,
        peek_interrupted_subagents=peek,
        clear_interrupted_subagents=clears.append,
    )
    return store, clears


@pytest.mark.parametrize(
    ("session_id", "expected"),
    [
        (
            "A",
            {
                "codePuppy/interrupted": [
                    {
                        "agentName": "explorer",
                        "subSessionId": "A-sub-1",
                        "savedCount": 3,
                    }
                ]
            },
        ),
        (
            "B",
            {
                "codePuppy/interrupted": [
                    {
                        "agentName": "planner",
                        "subSessionId": "B-sub-1",
                        "savedCount": None,
                    }
                ]
            },
        ),
        ("C", None),
    ],
)
def test_interrupted_meta_is_scoped_peeked_and_never_drained(
    interrupted_store: tuple[dict[str, Any], list[str]],
    session_id: str,
    expected: Any,
) -> None:
    store, clears = interrupted_store
    before = {key: list(records) for key, records in store.items()}

    assert composer.interrupted_meta(session_id) == expected
    assert clears == []
    assert store == before


def test_interrupted_meta_swallows_peek_errors(core) -> None:
    def broken(key: str | None = None) -> list[Any]:
        raise RuntimeError("store unavailable")

    core(SUBAGENTS, peek_interrupted_subagents=broken)

    assert composer.interrupted_meta("A") is None


# --- (f) binary media, session model and capabilities -----------------------


@pytest.fixture
def media_core(core):
    def supported(model_name: str) -> list[str]:
        if model_name == "boom":
            raise RuntimeError("media lookup failed")
        return {"vision-model": VISION_TYPES, "text-model": []}.get(model_name, [])

    core(MEDIA, supported_binary_media_types=supported)
    return core


@pytest.mark.parametrize(
    ("model_name", "expected"),
    [
        ("vision-model", VISION_TYPES),
        ("text-model", []),
        ("unknown-model", []),
        ("boom", []),
        (None, []),
        ("", []),
    ],
)
def test_binary_media_types_new_core(media_core, model_name, expected) -> None:
    assert composer.binary_media_types(model_name) == expected


def test_binary_media_meta_new_core(media_core) -> None:
    assert composer.binary_media_meta("vision-model") == VISION_META
    assert composer.binary_media_meta("text-model") == EMPTY_META


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        (FakeAgent("vision-model"), VISION_TYPES),
        (FakeAgent(RuntimeError("no model")), []),
    ],
)
def test_agent_binary_media_types(media_core, agent, expected) -> None:
    assert composer.agent_binary_media_types(agent) == expected


@pytest.mark.parametrize(
    ("session", "global_model", "expected"),
    [
        (SimpleNamespace(agent=FakeAgent("vision-model")), "text-model", VISION_META),
        (None, "vision-model", VISION_META),
        (
            SimpleNamespace(agent=FakeAgent(RuntimeError("x"))),
            "vision-model",
            VISION_META,
        ),
        (None, None, EMPTY_META),
    ],
)
def test_session_meta_prefers_session_model_then_global(
    media_core, session, global_model, expected
) -> None:
    media_core(CONFIG, get_global_model_name=lambda: global_model)

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
    core, has_pause: bool, has_media: bool, expected: Any
) -> None:
    core(CONFIG, get_global_model_name=lambda: "vision-model")
    if has_pause:
        core(PAUSE, get_session_pause_controller=lambda sid: FakeController())
    if has_media:
        core(
            MEDIA,
            supported_binary_media_types=lambda name: (
                VISION_TYPES if name == "vision-model" else []
            ),
        )

    assert composer.capabilities_meta() == expected


def test_capability_helpers_report_nothing_on_old_core() -> None:
    agent = FakeAgent("vision-model")

    assert composer.binary_media_types("vision-model") == []
    assert composer.binary_media_meta("vision-model") is None
    assert composer.agent_binary_media_types(agent) == []
    assert composer.session_meta(SimpleNamespace(agent=agent)) is None
    assert composer.capabilities_meta() is None
    assert composer.interrupted_meta("A") is None


# --- (g) release_session ----------------------------------------------------


def test_release_session_drops_controller_and_clears_subagents(core) -> None:
    dropped: list[str] = []
    cleared: list[str] = []
    core(PAUSE, drop_session_pause_controller=dropped.append)
    core(SUBAGENTS, clear_interrupted_subagents=cleared.append)

    composer.release_session("A")

    assert dropped == ["A"]
    assert cleared == ["A"]


def test_release_session_is_noop_on_old_core() -> None:
    composer.release_session("A")


@pytest.mark.parametrize("failing", ["drop", "clear"])
def test_release_session_one_failure_does_not_block_the_other(core, failing) -> None:
    calls: list[str] = []

    def recorder(name: str):
        def call(session_id: str) -> None:
            calls.append(name)
            if name == failing:
                raise RuntimeError("boom")

        return call

    core(PAUSE, drop_session_pause_controller=recorder("drop"))
    core(SUBAGENTS, clear_interrupted_subagents=recorder("clear"))

    composer.release_session("A")

    assert sorted(calls) == ["clear", "drop"]


# --- (h) prompt_in_flight_error ---------------------------------------------


def test_prompt_in_flight_error_full_contract() -> None:
    error = composer.prompt_in_flight_error("sess-1")

    assert isinstance(error, RequestError)
    assert error.code == -32000
    assert str(error) == "Session already has a running prompt"
    assert error.data == {"sessionId": "sess-1", "reason": "prompt_in_flight"}
