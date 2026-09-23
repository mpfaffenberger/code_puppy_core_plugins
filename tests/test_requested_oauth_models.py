"""Regression coverage for newly registered OAuth model names."""

import pytest

from code_puppy_core_plugins.chatgpt_oauth.utils import (
    DEFAULT_CODEX_MODELS,
    _supports_max_reasoning,
    _supports_responses_reasoning_controls,
    _supports_xhigh_reasoning,
)
from code_puppy_core_plugins.claude_code_oauth.config import CLAUDE_CODE_OAUTH_CONFIG
from code_puppy_core_plugins.claude_code_oauth.model_filter import (
    filter_latest_claude_models,
)
from code_puppy_core_plugins.claude_code_oauth.utils import _build_model_entry


def test_opus_5_5_survives_family_filter():
    models = [
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-opus-5",
        "claude-opus-5-5",
    ]
    assert filter_latest_claude_models(
        models, max_per_family=CLAUDE_CODE_OAUTH_CONFIG["model_family_limits"]
    ) == ["claude-opus-5-5", "claude-opus-5", "claude-opus-4-8"]
    entry = _build_model_entry("claude-opus-5-5", "test-token", 200000)
    assert entry["name"] == "claude-opus-5-5"
    assert "effort" in entry["supported_settings"]


@pytest.mark.parametrize("name", ["gpt-6-luna", "gpt-6-sol"])
def test_gpt_6_models_have_fallback_and_reasoning_support(name):
    assert DEFAULT_CODEX_MODELS.count(name) == 1
    assert _supports_xhigh_reasoning(name)
    assert _supports_max_reasoning(name)
    assert _supports_responses_reasoning_controls(name)
