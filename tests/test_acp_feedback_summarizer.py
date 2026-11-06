"""Tests for the thumbs-down background summarizer. No real model or network calls."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai.messages import (
    ImageUrl,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters

from code_puppy_core_plugins.acp import feedback_summarizer
from code_puppy_core_plugins.acp.feedback_summarizer import (
    REASON_MAX_CHARS,
    TAG_VOCABULARY,
    FeedbackSummary,
    extract_turn,
    filter_reason,
    summarize,
)

USER_PROMPT = "please fix the parser"
REPLY = "Here is a patch."
DEFAULT_OUTPUT = '{"tags": ["incomplete"], "reason": "Answer omitted a needed step"}'

SENTINEL_USER = (
    "ZEBRA-SECRET-123 quarterly revenue forecast spreadsheet acme_projection_zxq9000"
)
SENTINEL_REPLY = "Attached the quarterly revenue forecast for ZEBRA-SECRET-123."
SENTINELS = (
    "zebra-secret-123",
    "quarterly revenue forecast",
    "acme_projection_zxq9000",
)


def _exchange(user_text: str, reply_text: str) -> list[ModelMessage]:
    return [
        ModelRequest(parts=[UserPromptPart(content=user_text)]),
        ModelResponse(parts=[TextPart(content=reply_text)]),
    ]


@dataclass
class FakeTriage:
    load_history: MagicMock
    load_config: MagicMock
    get_model: MagicMock
    model_request: AsyncMock
    stream: MagicMock
    sync: MagicMock

    def reply(self, text: str) -> None:
        self.model_request.return_value = ModelResponse(parts=[TextPart(content=text)])


@pytest.fixture
def triage() -> Iterator[FakeTriage]:
    with (
        patch.object(
            feedback_summarizer.persistence,
            "load_history",
            return_value=_exchange(USER_PROMPT, REPLY),
        ) as load_history,
        patch(
            "code_puppy.model_factory.ModelFactory.load_config",
            return_value={"summ-model": {}},
        ) as load_config,
        patch(
            "code_puppy.model_factory.ModelFactory.get_model",
            return_value="fake-model",
        ) as get_model,
        patch(
            "code_puppy.config.get_summarization_model_name",
            return_value="summ-model",
        ),
        patch(
            "pydantic_ai.direct.model_request",
            new_callable=AsyncMock,
            return_value=ModelResponse(parts=[TextPart(content=DEFAULT_OUTPUT)]),
        ) as model_request,
        patch("pydantic_ai.direct.model_request_stream") as stream,
        patch("pydantic_ai.direct.model_request_sync") as sync,
    ):
        yield FakeTriage(
            load_history=load_history,
            load_config=load_config,
            get_model=get_model,
            model_request=model_request,
            stream=stream,
            sync=sync,
        )


def _sent_prompt(triage: FakeTriage) -> str:
    request = triage.model_request.await_args.args[1][0]
    return request.parts[0].content


# --- single non-streaming call with no tools ---------------------------------


async def test_summarize_makes_one_non_streaming_call_without_tools(triage):
    await summarize("session-1", ["wrong"])

    triage.model_request.assert_awaited_once()
    triage.stream.assert_not_called()
    triage.sync.assert_not_called()
    model, messages = triage.model_request.await_args.args[:2]
    assert model == "fake-model"
    triage.get_model.assert_called_once_with(
        "summ-model", triage.load_config.return_value
    )
    assert len(messages) == 1 and isinstance(messages[0], ModelRequest)
    kwargs = triage.model_request.await_args.kwargs
    settings = kwargs["model_settings"]
    assert settings["max_tokens"] == 1024
    assert not any("tool" in key for key in settings)
    params = kwargs["model_request_parameters"]
    assert isinstance(params, ModelRequestParameters)
    assert params.function_tools == []
    assert params.output_tools == []
    assert params.native_tools == []
    assert "toolsets" not in kwargs


async def test_summarize_does_not_pass_temperature(triage):
    await summarize("session-1", ["wrong"])

    settings = triage.model_request.await_args.kwargs["model_settings"]
    assert "temperature" not in settings


# --- prompt construction and privacy -----------------------------------------


async def test_prompt_contains_delimited_turn_text_and_instructions_stay_fixed(triage):
    triage.load_history.return_value = _exchange("UNIQUE-USER-PROMPT", "UNIQUE-REPLY")

    await summarize("session-1", ["wrong", "slow"])

    prompt = _sent_prompt(triage)
    assert (
        "<untrusted_user_prompt>\nUNIQUE-USER-PROMPT\n</untrusted_user_prompt>"
        in prompt
    )
    assert (
        "<untrusted_assistant_reply>\nUNIQUE-REPLY\n</untrusted_assistant_reply>"
        in prompt
    )
    instructions = triage.model_request.await_args.args[1][0].instructions
    assert "UNTRUSTED DATA" in instructions
    assert "UNIQUE-USER-PROMPT" not in instructions


async def test_long_turn_text_is_truncated_to_4000_chars_in_prompt(triage):
    triage.load_history.return_value = _exchange("a" * 5000 + "TAILMARK", "b" * 4500)

    await summarize("session-1")

    prompt = _sent_prompt(triage)
    assert "a" * 4000 in prompt
    assert "a" * 4001 not in prompt
    assert "TAILMARK" not in prompt
    assert "b" * 4000 in prompt
    assert "b" * 4001 not in prompt


@pytest.mark.parametrize(
    ("chips", "expected"),
    [
        (["wrong", "slow", "unsafe", "other"], "wrong, slow, unsafe, other"),
        (["Wrong", "wrong", " slow "], "wrong, slow"),
        (["bogus", "<script>"], "none"),
        ([], "none"),
    ],
    ids=["all-allowed", "normalized-dedupe", "all-rejected", "empty"],
)
async def test_chips_are_filtered_to_allowed_values_before_prompting(
    triage, chips, expected
):
    await summarize("session-1", chips)

    assert f"Chips: {expected}\n" in _sent_prompt(triage)


def test_summarize_has_no_user_comment_parameter():
    assert list(inspect.signature(summarize).parameters) == ["session_id", "chips"]


# --- privacy filter on model output ------------------------------------------


@pytest.mark.parametrize(
    "echoed_reason",
    [
        "Quarterly revenue forecast was off",
        "Quarterly, REVENUE forecast!!",
        "The acme_projection_zxq9000 step was wrong",
        "ZEBRA-SECRET-123 answer was wrong",
    ],
    ids=["verbatim-trigram", "case-punct-trigram", "long-identifier", "sentinel-id"],
)
async def test_reason_echoing_turn_text_is_dropped(triage, echoed_reason):
    triage.load_history.return_value = _exchange(SENTINEL_USER, SENTINEL_REPLY)
    triage.reply(f'{{"tags": ["incorrect"], "reason": "{echoed_reason}"}}')

    result = await summarize("session-1")

    assert result == FeedbackSummary(tags=["incorrect"], reason=None)
    rendered = repr(result).casefold()
    assert not any(sentinel in rendered for sentinel in SENTINELS)


async def test_tags_echoing_turn_text_are_dropped(triage):
    triage.load_history.return_value = _exchange(SENTINEL_USER, SENTINEL_REPLY)
    triage.reply(
        '{"tags": ["ZEBRA-SECRET-123", "quarterly revenue forecast", "incomplete"],'
        ' "reason": "Answer was incomplete"}'
    )

    result = await summarize("session-1")

    assert result == FeedbackSummary(
        tags=["incomplete"], reason="Answer was incomplete"
    )


@pytest.mark.parametrize(
    ("reason", "turn_text", "expected"),
    [
        (
            "The answer seemed incomplete and slow",
            "fix the parser",
            "The answer seemed incomplete and slow",
        ),
        (
            "Quarterly, REVENUE; spreadsheet issue",
            "the Quarterly-Revenue spreadsheet.",
            None,
        ),
        (
            "quarterly revenue looked off",
            "quarterly revenue spreadsheet",
            "quarterly revenue looked off",
        ),
        ("The `parser` was incomplete", "x", None),
        ('The "answer" was incomplete', "x", None),
        ("The \u201canswer\u201d was incomplete", "x", None),
        ("The src/app.py edit was incomplete", "x", None),
        ("The C\\temp path was wrong", "x", None),
        ("Ran under ~ and failed", "x", None),
        ("See https://example.com for details", "x", None),
        ("The config.yaml edit was incomplete", "x", None),
        ("   ", "x", None),
        ("", "x", None),
        ("The acme_projection_zxq9000 step", "ACME_PROJECTION_ZXQ9000 happened", None),
        (
            "the spreadsheet looked wrong",
            "quarterly revenue spreadsheet",
            "the spreadsheet looked wrong",
        ),
    ],
    ids=[
        "generic-kept",
        "three-word-overlap-case-punct",
        "two-word-overlap-kept",
        "backtick",
        "ascii-quote",
        "curly-quote",
        "path-with-filename",
        "backslash",
        "tilde",
        "url",
        "bare-filename",
        "whitespace-only",
        "empty",
        "long-token-case-insensitive",
        "eleven-char-token-kept",
    ],
)
def test_filter_reason_matrix(reason, turn_text, expected):
    assert filter_reason(reason, turn_text) == expected


@pytest.mark.parametrize(
    "reason", [None, 42, ["incomplete"], {"a": 1}], ids=lambda r: type(r).__name__
)
def test_filter_reason_rejects_non_string(reason):
    assert filter_reason(reason, "x") is None


def test_filter_reason_truncates_to_max_chars_then_validates():
    long_reason = "The answer was incomplete " + "clear " * 40

    result = filter_reason(long_reason, "fix the parser")

    assert result is not None
    assert len(result) <= REASON_MAX_CHARS


@pytest.mark.parametrize(
    ("reason", "kept"),
    [
        ("The answer was incomplete " + "x" * 100 + " /tmp/out", True),
        ("The answer was incomplete /tmp/out " + "x" * 100, False),
    ],
    ids=["slash-after-cut-kept", "slash-before-cut-dropped"],
)
def test_filter_reason_validates_only_the_truncated_text(reason, kept):
    assert (filter_reason(reason, "fix the parser") is not None) is kept


# --- output parser ------------------------------------------------------------


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            '```json\n{"tags": ["incomplete"], "reason": "Answer omitted a step"}\n```',
            FeedbackSummary(tags=["incomplete"], reason="Answer omitted a step"),
        ),
        (
            'Result: {"tags": ["too_slow"], "reason": "Response took long"} thanks {note}',
            FeedbackSummary(tags=["too_slow"], reason="Response took long"),
        ),
        (
            '{"tags": [" Incomplete ", "INCOMPLETE", "bogus", 7, "too_slow", "Too_Slow", "other"]}',
            FeedbackSummary(tags=["incomplete", "too_slow", "other"], reason=None),
        ),
        (
            '{"tags": ["other", "incorrect"], "reason": "Unclear step"}',
            FeedbackSummary(tags=["other", "incorrect"], reason="Unclear step"),
        ),
        (
            '{"tags": "incomplete", "reason": "Unclear step"}',
            FeedbackSummary(tags=[], reason="Unclear step"),
        ),
        (
            '{"tags": ["unclear"], "reason": 5}',
            FeedbackSummary(tags=["unclear"], reason=None),
        ),
    ],
    ids=[
        "code-fenced",
        "prose-around",
        "mixed-valid-invalid",
        "order-preserved",
        "tags-not-list",
        "reason-not-string",
    ],
)
async def test_parser_matrix(triage, output, expected):
    triage.reply(output)

    assert await summarize("session-1") == expected


def test_vocabulary_is_the_documented_nine_tags():
    assert len(TAG_VOCABULARY) == 9
    assert "unsafe_action" in TAG_VOCABULARY


# --- turn extraction ----------------------------------------------------------


def test_extract_turn_uses_only_the_last_exchange():
    history = [
        *_exchange("first prompt", "first reply"),
        ModelRequest(parts=[UserPromptPart(content="second prompt")]),
        ModelResponse(parts=[ToolCallPart(tool_name="t", args={}, tool_call_id="1")]),
        ModelRequest(
            parts=[
                ToolReturnPart(tool_name="t", content="TOOLSECRET", tool_call_id="1")
            ]
        ),
        ModelResponse(parts=[TextPart(content="second reply A")]),
        ModelResponse(parts=[TextPart(content="second reply B")]),
    ]

    assert extract_turn(history) == ("second prompt", "second reply A\nsecond reply B")


def test_extract_turn_joins_only_string_parts_of_list_content():
    image = ImageUrl(url="https://example.com/diagram.png")
    history = [ModelRequest(parts=[UserPromptPart(content=["alpha", image, "beta"])])]

    assert extract_turn(history) == ("alpha\nbeta", "")


@pytest.mark.parametrize(
    "history",
    [
        [],
        [ModelResponse(parts=[TextPart(content="orphan reply")])],
        [
            ModelRequest(
                parts=[ToolReturnPart(tool_name="t", content="x", tool_call_id="1")]
            )
        ],
        [ModelRequest(parts=[UserPromptPart(content="")])],
    ],
    ids=["empty", "no-user-prompt", "tool-return-only", "blank-turn"],
)
def test_extract_turn_returns_none_without_turn_text(history):
    assert extract_turn(history) is None


# --- failure paths return None and never log turn text -----------------------


def _no_history(t: FakeTriage) -> None:
    t.load_history.return_value = None


def _empty_history(t: FakeTriage) -> None:
    t.load_history.return_value = []


def _no_user_prompt(t: FakeTriage) -> None:
    t.load_history.return_value = [ModelResponse(parts=[TextPart(content="hi")])]


def _history_raises(t: FakeTriage) -> None:
    t.load_history.configure_mock(side_effect=OSError("disk gone"))


def _lookup_raises(t: FakeTriage) -> None:
    t.get_model.configure_mock(side_effect=ValueError(f"no model {SENTINELS[0]}"))


def _config_raises(t: FakeTriage) -> None:
    t.load_config.configure_mock(side_effect=RuntimeError("bad config"))


def _call_raises(t: FakeTriage) -> None:
    t.model_request.configure_mock(
        side_effect=RuntimeError(f"provider said {SENTINELS[0]}")
    )


def _no_text_part(t: FakeTriage) -> None:
    t.model_request.return_value = ModelResponse(parts=[])


def _unparsable_output(t: FakeTriage) -> None:
    t.reply(f"{SENTINELS[0]} not json at all")


def _json_list_output(t: FakeTriage) -> None:
    t.reply('["incomplete"]')


@pytest.mark.parametrize(
    "configure",
    [
        _no_history,
        _empty_history,
        _no_user_prompt,
        _history_raises,
        _lookup_raises,
        _config_raises,
        _call_raises,
        _no_text_part,
        _unparsable_output,
        _json_list_output,
    ],
    ids=[
        "history-none",
        "history-empty",
        "no-user-prompt",
        "history-raises",
        "model-lookup-raises",
        "config-raises",
        "model-call-raises",
        "no-text-part",
        "unparsable-output",
        "json-list-output",
    ],
)
async def test_failures_return_none_and_log_only_exception_type(
    triage, caplog, configure
):
    triage.load_history.return_value = _exchange(SENTINEL_USER, SENTINEL_REPLY)
    configure(triage)
    caplog.set_level(logging.DEBUG, logger=feedback_summarizer.__name__)

    assert await summarize("session-1") is None

    assert not any(sentinel in caplog.text.casefold() for sentinel in SENTINELS)
