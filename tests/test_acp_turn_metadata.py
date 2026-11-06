"""Unit tests for ACP per-turn metadata: model/correlationId collection and shape."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from code_puppy_core_plugins.acp import turn_metadata
from code_puppy_core_plugins.acp.turn_metadata import (
    collect_turn_metadata,
    register_turn_metadata_provider,
    turn_meta,
    unregister_turn_metadata_provider,
)

SECRET = "SECRET-TOKEN-xyz"


@pytest.fixture(autouse=True)
def clean_registry():
    saved = list(turn_metadata._providers)
    turn_metadata._providers.clear()
    yield
    turn_metadata._providers[:] = saved


def _agent(model_name=None, *, error: Exception | None = None):
    agent = MagicMock()
    if error is not None:
        agent.get_model_name.side_effect = error
    else:
        agent.get_model_name.return_value = model_name
    return agent


def _returning(value):
    def provider(agent, session_id):
        return value

    return provider


def test_model_only_when_no_provider_registered():
    values = collect_turn_metadata(_agent("claude-x"), "sess-1")

    assert values == {"model": "claude-x"}
    assert turn_meta(values) == {"codePuppy": {"turn": {"model": "claude-x"}}}


def test_model_is_stripped():
    assert collect_turn_metadata(_agent("  claude-x \n"), "s") == {"model": "claude-x"}


@pytest.mark.parametrize(
    "agent",
    [
        _agent(error=RuntimeError(SECRET)),
        _agent(""),
        _agent("   "),
        _agent(42),
        _agent(None),
    ],
)
def test_no_model_and_no_provider_yields_empty(agent):
    values = collect_turn_metadata(agent, "s")

    assert values == {}
    assert turn_meta(values) is None


@pytest.mark.parametrize("values", [{}, None, {"other": "x"}])
def test_turn_meta_is_none_without_known_keys(values):
    assert turn_meta(values) is None


@pytest.mark.parametrize("result", [None, "abc", ["correlationId"], {"other": "x"}])
def test_provider_without_usable_dict_adds_nothing(result):
    register_turn_metadata_provider(_returning(result))

    assert collect_turn_metadata(_agent("m"), "s") == {"model": "m"}


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        ("", False),
        ("x" * 129, False),
        ("has space", False),
        ("new\nline", False),
        ("a/b", False),
        (123, False),
        (None, False),
        ("x" * 128, True),
        ("abc-1_2.3:4", True),
        ("a", True),
    ],
)
def test_correlation_id_is_validated(value, accepted):
    register_turn_metadata_provider(_returning({"correlationId": value}))

    values = collect_turn_metadata(_agent("m"), "s")

    if accepted:
        assert values == {"model": "m", "correlationId": value}
    else:
        assert values == {"model": "m"}


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        (_agent("real-model"), {"model": "real-model", "correlationId": "ok"}),
        (_agent(error=RuntimeError("no model")), {"correlationId": "ok"}),
    ],
)
def test_provider_cannot_inject_model_or_other_keys(agent, expected):
    register_turn_metadata_provider(
        _returning({"model": "evil", "secret": "x", "correlationId": "ok"})
    )

    assert collect_turn_metadata(agent, "s") == expected


def test_provider_exception_is_swallowed_and_logged_by_type_only(caplog):
    def boom(agent, session_id):
        raise ValueError(f"token={SECRET}")

    caplog.set_level(logging.DEBUG)
    register_turn_metadata_provider(boom)
    register_turn_metadata_provider(_returning({"correlationId": "after"}))

    values = collect_turn_metadata(_agent("m"), "s")

    assert values == {"model": "m", "correlationId": "after"}
    assert "ValueError" in caplog.text
    assert SECRET not in caplog.text


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ({"correlationId": "first"}, {"correlationId": "second"}, "first"),
        ({"correlationId": "bad id"}, {"correlationId": "second"}, "second"),
    ],
)
def test_first_valid_correlation_id_wins(first, second, expected):
    register_turn_metadata_provider(_returning(first))
    register_turn_metadata_provider(_returning(second))

    values = collect_turn_metadata(_agent("m"), "s")

    assert values["correlationId"] == expected


def test_register_is_idempotent():
    calls = []

    def provider(agent, session_id):
        calls.append(session_id)
        return None

    register_turn_metadata_provider(provider)
    register_turn_metadata_provider(provider)
    collect_turn_metadata(_agent("m"), "s")

    assert calls == ["s"]


def test_unregister_removes_provider_and_ignores_unknown():
    provider = _returning({"correlationId": "gone"})
    register_turn_metadata_provider(provider)

    unregister_turn_metadata_provider(provider)
    unregister_turn_metadata_provider(provider)

    assert collect_turn_metadata(_agent("m"), "s") == {"model": "m"}


@pytest.mark.parametrize(
    ("values", "expected_turn"),
    [
        ({"model": "m"}, {"model": "m"}),
        ({"correlationId": "c"}, {"correlationId": "c"}),
        ({"model": "m", "correlationId": "c"}, {"model": "m", "correlationId": "c"}),
        ({"model": "m", "unrelated": "x"}, {"model": "m"}),
    ],
)
def test_turn_meta_shape_projects_only_known_keys(values, expected_turn):
    assert turn_meta(values) == {"codePuppy": {"turn": expected_turn}}
