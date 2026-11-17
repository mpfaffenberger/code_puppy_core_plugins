"""Tests for the gemini_no_thinking_guard plugin."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field

import pytest

from code_puppy.config import set_config_value


def _module(name: str):
    return importlib.import_module(
        f"code_puppy_core_plugins.gemini_no_thinking_guard.{name}"
    )


policy = _module("policy")
config = _module("config")
callbacks = _module("register_callbacks")


@dataclass
class Part:
    part_kind: str
    content: str = ""
    signature: str | None = None


@dataclass
class Msg:
    kind: str
    parts: list = field(default_factory=list)


def prompt() -> Msg:
    return Msg("request", [Part("user-prompt", "where does X come from?")])


def response(*parts: Part) -> Msg:
    return Msg("response", list(parts))


SMALL = policy.GuardSettings(step_back_after=3, force_answer_after=5)


def actions(guard, tools, thinking=False, settings=SMALL):
    return [guard.before_call(t, thinking, settings)[0] for t in tools]


# ---------------------------------------------------------------------------
# thinking detection
# ---------------------------------------------------------------------------


class TestThinkingDetection:
    def test_thinking_in_current_turn_counts(self):
        msgs = [prompt(), response(Part("thinking", "plan"), Part("tool-call"))]
        assert policy.has_thinking_since_last_prompt(msgs)

    def test_thinking_from_an_earlier_turn_does_not_count(self):
        msgs = [
            prompt(),
            response(Part("thinking", "plan")),
            prompt(),
            response(Part("tool-call")),
        ]
        assert not policy.has_thinking_since_last_prompt(msgs)

    def test_empty_unsigned_thinking_part_is_not_thinking(self):
        msgs = [prompt(), response(Part("thinking", ""), Part("tool-call"))]
        assert not policy.has_thinking_since_last_prompt(msgs)

    def test_signed_thinking_part_without_text_counts(self):
        msgs = [prompt(), response(Part("thinking", "", signature="sig"))]
        assert policy.has_thinking_since_last_prompt(msgs)


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------


class TestRunGuard:
    def test_no_thinking_steps_back_once_then_forces_answer(self):
        got = actions(policy.RunGuard(), ["grep"] * 8)
        assert got == [None, None, None, "step_back", None, "force", "force", "force"]

    def test_step_back_blocks_and_reports_the_calls_made(self):
        guard = policy.RunGuard()
        actions(guard, ["grep"] * 3)
        action, result = guard.before_call("grep", False, SMALL)
        assert action == "step_back"
        assert result["blocked"] is True
        assert "made 3 tool calls" in result["error_message"]
        assert "qualifier" in result["error_message"]

    def test_force_blocks_with_answer_now(self):
        guard = policy.RunGuard()
        actions(guard, ["grep"] * 5)
        assert guard.before_call("grep", False, SMALL) == (
            "force",
            {"blocked": True, "error_message": policy.FORCE_ANSWER_TEXT},
        )

    def test_stays_out_once_thinking_was_seen_this_run(self):
        guard = policy.RunGuard()
        guard.before_call("grep", True, SMALL)
        assert actions(guard, ["grep"] * 10) == [None] * 10

    def test_edits_reset_the_count_so_long_edit_turns_are_never_stopped(self):
        tools = ["read_file", "read_file", "edit_file"] * 10
        assert set(actions(policy.RunGuard(), tools)) == {None}

    def test_step_back_can_fire_again_after_an_edit(self):
        guard = policy.RunGuard()
        first = actions(guard, ["grep"] * 4)
        second = actions(guard, ["create_file"] + ["grep"] * 4)
        assert first[-1] == "step_back" and second[-1] == "step_back"

    def test_zero_disables_each_step(self):
        off = policy.GuardSettings(step_back_after=0, force_answer_after=0)
        assert actions(policy.RunGuard(), ["grep"] * 40, settings=off) == [None] * 40


# ---------------------------------------------------------------------------
# config + hook wiring
# ---------------------------------------------------------------------------


class FakeAgent:
    def __init__(self, model: str, history: list):
        self._model = model
        self._history = history

    def get_model_name(self):
        return self._model

    def get_message_history(self):
        return self._history


@pytest.fixture
def run_as(monkeypatch):
    callbacks._guards.clear()

    def _set(agent):
        monkeypatch.setattr(callbacks, "get_executing_agent", lambda: agent)
        return agent

    yield _set
    callbacks._guards.clear()


def call_n(n: int) -> list:
    return [callbacks._on_pre_tool_call("grep", {}) for _ in range(n)]


class TestHook:
    def test_flash_without_thinking_is_guarded(self, run_as):
        run_as(FakeAgent("gemini-3-flash", [prompt()]))
        results = call_n(13)
        assert results[:12] == [None] * 12
        assert results[12]["blocked"] and "[step back]" in results[12]["error_message"]

    def test_flash_with_thinking_is_left_alone(self, run_as):
        run_as(
            FakeAgent("gemini-3-flash", [prompt(), response(Part("thinking", "plan"))])
        )
        assert call_n(30) == [None] * 30

    @pytest.mark.parametrize("model", ["gemini-3-pro", "claude-sonnet-4-5", "gpt-5"])
    def test_other_models_are_untouched(self, run_as, model):
        run_as(FakeAgent(model, [prompt()]))
        assert call_n(30) == [None] * 30

    def test_disabled_by_setting(self, run_as):
        set_config_value(config.ENABLED_KEY, "off")
        try:
            run_as(FakeAgent("gemini-3-flash", [prompt()]))
            assert call_n(30) == [None] * 30
        finally:
            set_config_value(config.ENABLED_KEY, "")

    def test_thresholds_come_from_settings(self, run_as):
        set_config_value(config.STEP_BACK_AFTER_KEY, "2")
        try:
            run_as(FakeAgent("gemini-3-flash", [prompt()]))
            results = call_n(3)
            assert results[:2] == [None, None] and results[2]["blocked"]
        finally:
            set_config_value(config.STEP_BACK_AFTER_KEY, "")

    def test_new_run_starts_a_fresh_count(self, run_as):
        run_as(FakeAgent("gemini-3-flash", [prompt()]))
        call_n(10)
        callbacks._on_agent_run_start("code-puppy", "gemini-3-flash")
        assert call_n(12) == [None] * 12
