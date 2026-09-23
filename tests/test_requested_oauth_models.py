"""Regression coverage for newly registered OAuth model names."""

from unittest.mock import Mock

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
    assert entry["custom_endpoint"]["headers"]["User-Agent"].startswith(
        "claude-cli/2.1.280 "
    )


@pytest.mark.parametrize("name", ["gpt-6-luna", "gpt-6-sol"])
def test_gpt_6_models_have_fallback_and_reasoning_support(name):
    assert DEFAULT_CODEX_MODELS.count(name) == 1
    assert _supports_xhigh_reasoning(name)
    assert _supports_max_reasoning(name)
    assert _supports_responses_reasoning_controls(name)


def test_successful_codex_discovery_keeps_explicit_options(monkeypatch):
    from code_puppy_core_plugins.chatgpt_oauth import utils

    response = Mock(status_code=200)
    response.json.return_value = {
        "models": [
            {"slug": "gpt-6-astra", "context_window": 272000},
            {"slug": "gpt-6-sol", "context_window": 372000},
        ]
    }
    monkeypatch.setattr(utils.requests, "get", Mock(return_value=response))
    entries = utils.fetch_chatgpt_models("test-token", "test-account")
    assert [entry.name for entry in entries] == [
        "gpt-6-astra",
        "gpt-6-sol",
        "gpt-6-luna",
    ]
    assert entries[1].context_length == 353400
    assert entries[2].context_length is None

    saved = {}
    monkeypatch.setattr(utils, "load_chatgpt_models", lambda: {})
    monkeypatch.setattr(
        utils, "save_chatgpt_models", lambda models: saved.update(models) or True
    )
    assert utils.add_models_to_extra_config(entries)
    assert saved["codex-gpt-6-sol"]["context_length"] == 353400
    assert saved["codex-gpt-6-luna"]["context_length"] == 258400


def test_opus_5_5_is_registered_and_retained_on_load(monkeypatch, tmp_path):
    from code_puppy_core_plugins.claude_code_oauth import utils

    monkeypatch.setattr(
        utils, "get_claude_models_path", lambda: tmp_path / "models.json"
    )
    monkeypatch.setattr(utils, "get_valid_access_token", lambda: "test-token")
    assert utils.add_models_to_extra_config(
        ["claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7"]
    )
    loaded = utils.load_claude_models_filtered()
    assert "claude-code-claude-opus-5-5" in loaded
    assert "claude-code-claude-opus-4-7" not in loaded
