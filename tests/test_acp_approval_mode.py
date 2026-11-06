"""The ACP ``approval_mode`` config option is read-only and reflects yolo mode.

Clients may see the approval mode but must never change it: writing it would
turn a client request into a CLI configuration change.
"""

from __future__ import annotations

from typing import Any

import pytest

from code_puppy_core_plugins.acp import session_config

_OPTION_ID = "approval_mode"


@pytest.fixture
def writes(monkeypatch):
    """Record every config/model write; none should happen for approval_mode."""
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        "code_puppy.config.set_config_value",
        lambda key, value: calls.append(("set_config_value", key, value)),
    )
    monkeypatch.setattr(
        "code_puppy.config.set_model_name",
        lambda name: calls.append(("set_model_name", name)),
    )
    return calls


@pytest.fixture(autouse=True)
def _stub_model_list(monkeypatch):
    # Keeps config_options() independent of the real models file.
    monkeypatch.setattr(
        "code_puppy.command_line.model_picker_completion.load_model_names",
        lambda: ["stub-model"],
    )
    monkeypatch.setattr("code_puppy.config.get_global_model_name", lambda: "stub-model")


def _set_yolo(monkeypatch, yolo: bool) -> None:
    monkeypatch.setattr("code_puppy.config.get_yolo_mode", lambda: yolo)


def _approval_option(options: list[Any]) -> Any:
    matches = [opt for opt in options if opt.id == _OPTION_ID]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.parametrize(
    ("yolo", "expected"),
    [(False, "ask"), (True, "auto")],
)
def test_approval_option_reflects_yolo_mode(monkeypatch, yolo, expected):
    _set_yolo(monkeypatch, yolo)

    option = _approval_option(session_config.config_options())

    assert option.current_value == expected
    assert option.description == "Read-only: set by the CLI configuration."


@pytest.mark.parametrize(
    ("yolo", "expected", "description"),
    [
        (False, "ask", "Ask before commands and edits"),
        (True, "auto", "Run without asking"),
    ],
)
def test_approval_option_lists_only_current_value(
    monkeypatch, yolo, expected, description
):
    _set_yolo(monkeypatch, yolo)

    option = _approval_option(session_config.config_options())

    assert [(c.value, c.description) for c in option.options] == [
        (expected, description)
    ]


@pytest.mark.parametrize("value", ["auto", "ask", "true", True])
def test_applying_approval_mode_is_a_no_op(monkeypatch, writes, value):
    _set_yolo(monkeypatch, False)

    options = session_config.apply_config_option(_OPTION_ID, value)

    assert writes == []
    assert _approval_option(options).current_value == "ask"
