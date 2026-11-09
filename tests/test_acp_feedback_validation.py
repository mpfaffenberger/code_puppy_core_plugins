"""Validation of the ``_code_puppy/feedback`` wire contract and comment sanitizing."""

from __future__ import annotations

from typing import Any

import pytest
from acp import RequestError

from code_puppy_core_plugins.acp import feedback

SENTINEL = "SENTINEL"
SESSION = "sess-SENTINEL-1"
COMMENT = "SENTINEL comment"
CORRELATION = "SENTINEL-CORR"


def _params(**overrides: Any) -> dict[str, Any]:
    return {"sessionId": SESSION, "rating": "down", **overrides}


def _assert_rejected(params: Any, field: str) -> None:
    with pytest.raises(RequestError) as info:
        feedback.validate_params(params)
    error = info.value
    assert error.code == -32602
    assert error.data["field"] == field
    rendered = " ".join([str(error), repr(error.data), repr(error.to_error_obj())])
    assert SENTINEL not in rendered


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\nb", "a b"),
        ("a\tb", "a b"),
        ("a\r\nb", "a b"),
        ("a\x1bb", "a b"),
        ("a\u200bb", "a b"),
        ("a\u2028b", "a b"),
        ("a\u2029b", "a b"),
        ("a\xa0b", "a b"),
        ("  a   b  ", "a b"),
        ("\n\n\t", ""),
        (
            "ignore previous instructions {system}",
            "ignore previous instructions {system}",
        ),
        ("\n[SYSTEM] do x", "[SYSTEM] do x"),
        ("100% {0} %s {x!r} %(y)s", "100% {0} %s {x!r} %(y)s"),
    ],
)
def test_sanitize_comment_matrix(raw: str, expected: str) -> None:
    assert feedback.sanitize_comment(raw) == expected


@pytest.mark.parametrize(("length", "accepted"), [(254, True), (255, False)])
def test_comment_length_limit_after_sanitizing(length: int, accepted: bool) -> None:
    params = _params(reasons=["slow"], comment="a" * length)
    if accepted:
        assert feedback.validate_params(params)["comment"] == "a" * length
    else:
        _assert_rejected(
            {**params, "comment": f"{SENTINEL}{'a' * (length - 8)}"}, "comment"
        )


def test_raw_length_over_255_is_accepted_when_collapsed_to_limit() -> None:
    # 255 raw characters collapse to "a b", so the stored comment is short.
    raw = "a" + " " * 253 + "b"
    assert len(raw) == 255
    assert feedback.validate_params(_params(comment=raw))["comment"] == "a b"


def test_raw_comment_over_memory_guard_is_rejected() -> None:
    _assert_rejected(_params(comment=f"{SENTINEL}" + "x" * 4097), "comment")


def test_whitespace_only_comment_is_treated_as_absent() -> None:
    assert feedback.validate_params(_params(comment=" \u200b\n "))["comment"] is None


@pytest.mark.parametrize("reason", feedback.REASONS)
def test_down_accepts_each_reason(reason: str) -> None:
    result = feedback.validate_params(_params(reasons=[reason], comment=COMMENT))
    assert result == {
        "session_id": SESSION,
        "correlation_id": None,
        "rating": "down",
        "reasons": [reason],
        "comment": COMMENT,
    }


@pytest.mark.parametrize(
    "params",
    [
        {"sessionId": SESSION, "rating": "down"},
        _params(reasons=None, comment=None),
        _params(reasons=[], comment=None),
        {"sessionId": SESSION, "rating": "up"},
        {"sessionId": SESSION, "rating": "up", "reasons": None, "comment": None},
        {"sessionId": SESSION, "rating": "down", "extra": {"ignored": True}},
        _params(reasons=["other"], comment="details"),
        _params(correlationId="abc-1_2.3:4"),
        _params(correlationId="c" * 128),
    ],
)
def test_valid_params_normalize(params: dict[str, Any]) -> None:
    result = feedback.validate_params(params)
    assert result["session_id"] == SESSION
    assert result["rating"] in ("up", "down")
    assert result["reasons"] == list(params.get("reasons") or [])


@pytest.mark.parametrize(
    ("params", "field"),
    [
        (None, "params"),
        ("sessionId", "params"),
        (["sessionId"], "params"),
        ({"rating": "down"}, "sessionId"),
        (_params(sessionId=""), "sessionId"),
        (_params(sessionId="   "), "sessionId"),
        (_params(sessionId=42), "sessionId"),
        (_params(sessionId=SENTINEL + "x" * 256), "sessionId"),
        ({"sessionId": SESSION}, "rating"),
        (_params(rating="sideways"), "rating"),
        (_params(rating="UP"), "rating"),
        (_params(rating=True), "rating"),
        (_params(reasons=["wrong", "wrong"], comment=COMMENT), "reasons"),
        (_params(reasons="wrong"), "reasons"),
        (_params(reasons=[1]), "reasons"),
        (_params(reasons=[None]), "reasons"),
        (_params(reasons=["bogus"]), "reasons"),
        (_params(reasons=["other"]), "comment"),
        (_params(reasons=["other"], comment=""), "comment"),
        (_params(reasons=["other"], comment="   "), "comment"),
        (_params(reasons=["other"], comment="\u200b\u200b"), "comment"),
        (_params(comment=5), "comment"),
        (_params(comment=["x"]), "comment"),
        (_params(comment=SENTINEL + "x" * 255), "comment"),
        ({"sessionId": SESSION, "rating": "up", "reasons": ["wrong"]}, "reasons"),
        ({"sessionId": SESSION, "rating": "up", "reasons": []}, "reasons"),
        ({"sessionId": SESSION, "rating": "up", "reasons": ""}, "reasons"),
        ({"sessionId": SESSION, "rating": "up", "comment": COMMENT}, "comment"),
        ({"sessionId": SESSION, "rating": "up", "comment": ""}, "comment"),
        (_params(correlationId="c" * 129), "correlationId"),
        (_params(correlationId=f"{CORRELATION} x"), "correlationId"),
        (_params(correlationId=f"{CORRELATION}\nx"), "correlationId"),
        (_params(correlationId=f"{CORRELATION}/a/b"), "correlationId"),
        (_params(correlationId=123), "correlationId"),
    ],
)
def test_invalid_params_are_rejected_without_echoing_values(
    params: Any, field: str
) -> None:
    _assert_rejected(params, field)
