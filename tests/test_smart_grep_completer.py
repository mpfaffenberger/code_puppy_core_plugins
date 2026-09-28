"""`/set` autocompletes smart_grep's keys (jev_grep plugin)."""

from __future__ import annotations

import pytest
from termflow.tui.completion import Document

from code_puppy import callbacks
from code_puppy.command_line import completers as core_completers
from code_puppy.config import set_config_value
from code_puppy.messaging.editor_completion import build_completer
from code_puppy_core_plugins.jev_grep.completer import SmartGrepSetCompleter


def _texts(completer, text: str) -> list[str]:
    document = Document(text=text, cursor_position=len(text))
    return [c.text for c in completer.get_completions(document, None)]


@pytest.mark.parametrize(
    "typed, expected",
    [
        ("/set ", ["smart_grep = ", "smart_grep_model = ", "smart_grep_threshold = "]),
        (
            "/set smart",
            ["smart_grep = ", "smart_grep_model = ", "smart_grep_threshold = "],
        ),
        ("  /set smart_grep_t", ["smart_grep_threshold = "]),
        ("/set yolo", []),
        ("/set smart_grep = o", []),  # past the key: nothing left to complete
        ("/set", []),  # core suggests the trailing space
        ("/sett smart", []),
        ("smart", []),
    ],
)
def test_completes_smart_grep_keys(typed, expected):
    assert _texts(SmartGrepSetCompleter(), typed) == expected


def test_completion_replaces_only_the_typed_key():
    document = Document(text="/set smart_g", cursor_position=len("/set smart_g"))
    first = next(iter(SmartGrepSetCompleter().get_completions(document, None)))
    assert first.start_position == -len("smart_g")


def test_steps_aside_once_saved_so_core_owns_it():
    set_config_value("smart_grep", "on")
    assert _texts(SmartGrepSetCompleter(), "/set smart_grep") == [
        "smart_grep_model = ",
        "smart_grep_threshold = ",
    ]


def test_registered_as_completion_provider():
    from code_puppy_core_plugins.jev_grep import register_callbacks  # noqa: F401

    registered = callbacks._callbacks["register_completion_provider"]
    assert SmartGrepSetCompleter in registered


@pytest.mark.parametrize("saved", [False, True])
def test_real_stack_lists_smart_grep_exactly_once(monkeypatch, saved):
    """End to end through core's completer stack: no gap, no duplicate."""
    monkeypatch.setitem(
        callbacks._callbacks, "register_completion_provider", [SmartGrepSetCompleter]
    )
    core_completers._config_keys_cache.clear()
    if saved:
        set_config_value("smart_grep", "on")
    texts = _texts(build_completer(), "/set smart_grep")
    core_completers._config_keys_cache.clear()
    hits = [t for t in texts if t.startswith("smart_grep =")]
    assert hits == (["smart_grep = on"] if saved else ["smart_grep = "])
