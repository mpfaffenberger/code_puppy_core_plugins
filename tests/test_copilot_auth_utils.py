"""Tests for Copilot model-catalogue parsing and registration.

The Copilot ``/models`` endpoint reports, per model, which HTTP endpoints it
accepts (``supported_endpoints``).  GPT-5.5 / GPT-5.6-* / Codex are
``/responses``-only, so the plugin must remember that at registration time
(``copilot_api``) instead of blindly using Chat Completions.
"""

from unittest.mock import MagicMock, patch

import pytest

from code_puppy_core_plugins.copilot_auth import utils
from code_puppy_core_plugins.copilot_auth.utils import (
    COPILOT_API_CHAT,
    COPILOT_API_RESPONSES,
    _catalogue_entry,
    add_models_to_config,
    fetch_copilot_models,
    preferred_api_for_endpoints,
)

# Trimmed from a real api.enterprise.githubcopilot.com/models response.
CATALOGUE = {
    "data": [
        {
            "id": "gpt-5.6-terra",
            "supported_endpoints": ["/responses", "ws:/responses"],
            "capabilities": {"limits": {"max_context_window_tokens": 400000}},
        },
        {
            "id": "gpt-5.4",
            "supported_endpoints": ["/responses", "/chat/completions", "ws:/responses"],
            "capabilities": {"limits": {"max_context_window_tokens": 400000}},
        },
        {
            "id": "claude-sonnet-5",
            "supported_endpoints": ["/v1/messages", "/chat/completions"],
            "capabilities": {"limits": {"max_context_window_tokens": 264000}},
        },
        {
            "id": "gpt-4o",
            "supported_endpoints": None,
            "capabilities": {"limits": {"max_context_window_tokens": 128000}},
        },
        {"id": "text-embedding-3-small", "capabilities": {"limits": {}}},
        {"name": "legacy-name-only"},
        {"object": "model"},  # no id at all -> dropped
        "bare-string-id",
    ]
}


class TestPreferredApiForEndpoints:
    @pytest.mark.parametrize(
        "endpoints, expected",
        [
            (["/responses", "ws:/responses"], COPILOT_API_RESPONSES),
            (["/responses"], COPILOT_API_RESPONSES),
            (["/responses", "/chat/completions", "ws:/responses"], COPILOT_API_CHAT),
            (["/chat/completions", "/v1/messages"], COPILOT_API_CHAT),
            (["/chat/completions"], COPILOT_API_CHAT),
            (["/v1/messages"], COPILOT_API_CHAT),
            ([], COPILOT_API_CHAT),
            (None, COPILOT_API_CHAT),
            ("/responses", COPILOT_API_CHAT),  # not a list -> default
            ([" /Responses "], COPILOT_API_RESPONSES),  # tolerant of case/space
        ],
    )
    def test_truth_table(self, endpoints, expected):
        assert preferred_api_for_endpoints(endpoints) == expected


class TestCatalogueEntry:
    def test_dict_with_endpoints_and_limits(self):
        assert _catalogue_entry(CATALOGUE["data"][0]) == {
            "id": "gpt-5.6-terra",
            "api": COPILOT_API_RESPONSES,
            "context_length": 400000,
        }

    def test_dict_without_endpoints_defaults_to_chat(self):
        assert _catalogue_entry(CATALOGUE["data"][3]) == {
            "id": "gpt-4o",
            "api": COPILOT_API_CHAT,
            "context_length": 128000,
        }

    def test_missing_limits_gives_none_context(self):
        assert _catalogue_entry(CATALOGUE["data"][4])["context_length"] is None

    def test_name_used_when_id_absent(self):
        assert (
            _catalogue_entry({"name": "legacy-name-only"})["id"] == "legacy-name-only"
        )

    def test_bare_string(self):
        assert _catalogue_entry("gpt-4.1") == {
            "id": "gpt-4.1",
            "api": COPILOT_API_CHAT,
            "context_length": None,
        }

    @pytest.mark.parametrize("raw", [{"object": "model"}, "", None, 42])
    def test_unusable_items_dropped(self, raw):
        assert _catalogue_entry(raw) is None

    @pytest.mark.parametrize("ctx", [0, -1, "400000", None])
    def test_bad_context_values_ignored(self, ctx):
        raw = {
            "id": "x",
            "capabilities": {"limits": {"max_context_window_tokens": ctx}},
        }
        assert _catalogue_entry(raw)["context_length"] is None

    def test_normalised_entry_round_trips_unchanged(self):
        # add_models_to_config feeds fetch_copilot_models' output back in;
        # the api decision must survive instead of defaulting to chat.
        entry = {
            "id": "gpt-5.6-terra",
            "api": COPILOT_API_RESPONSES,
            "context_length": 400000,
        }
        assert _catalogue_entry(entry) == entry
        assert (
            _catalogue_entry(dict(entry, context_length=None))["context_length"] is None
        )

    def test_unknown_api_value_recomputed_from_endpoints(self):
        raw = {"id": "x", "api": "bogus", "supported_endpoints": ["/responses"]}
        assert _catalogue_entry(raw)["api"] == COPILOT_API_RESPONSES


class TestFetchCopilotModels:
    def _response(self, status=200, payload=None):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = payload if payload is not None else CATALOGUE
        return resp

    def test_parses_live_catalogue(self):
        with patch.object(utils.requests, "get", return_value=self._response()) as get:
            entries = fetch_copilot_models("sess", "github.com")

        assert get.call_args[0][0].endswith("/models")
        assert get.call_args[1]["headers"]["Authorization"] == "Bearer sess"
        by_id = {e["id"]: e for e in entries}
        assert by_id["gpt-5.6-terra"]["api"] == COPILOT_API_RESPONSES
        assert by_id["gpt-5.4"]["api"] == COPILOT_API_CHAT
        assert by_id["claude-sonnet-5"]["api"] == COPILOT_API_CHAT
        assert by_id["gpt-4o"]["context_length"] == 128000
        assert by_id["bare-string-id"]["api"] == COPILOT_API_CHAT
        assert "legacy-name-only" in by_id
        assert len(entries) == 7  # the id-less dict was dropped

    def test_falls_back_to_defaults_on_http_error(self):
        with patch.object(
            utils.requests, "get", return_value=self._response(status=500)
        ):
            entries = fetch_copilot_models("sess")

        assert [e["id"] for e in entries] == list(utils.DEFAULT_COPILOT_MODELS)
        assert all(e["api"] == COPILOT_API_CHAT for e in entries)
        assert all(e["context_length"] is None for e in entries)

    def test_falls_back_to_defaults_on_exception(self):
        with patch.object(utils.requests, "get", side_effect=OSError("boom")):
            entries = fetch_copilot_models("sess")

        assert [e["id"] for e in entries] == list(utils.DEFAULT_COPILOT_MODELS)

    def test_falls_back_when_catalogue_empty(self):
        with patch.object(
            utils.requests, "get", return_value=self._response(payload={"data": []})
        ):
            entries = fetch_copilot_models("sess")

        assert [e["id"] for e in entries] == list(utils.DEFAULT_COPILOT_MODELS)


class TestAddModelsToConfig:
    def _register(self, models, existing=None):
        saved = {}

        def _save(data):
            saved.update(data)
            return True

        with (
            patch.object(
                utils, "load_copilot_models", return_value=dict(existing or {})
            ),
            patch.object(utils, "save_copilot_models", side_effect=_save),
            patch.object(
                utils,
                "get_api_endpoint_for_host",
                return_value="https://api.enterprise.githubcopilot.com",
            ),
        ):
            ok = add_models_to_config(models, "github.com")
        return ok, saved

    def test_persists_copilot_api_and_catalogue_context_length(self):
        ok, saved = self._register(
            [
                {
                    "id": "gpt-5.6-terra",
                    "api": COPILOT_API_RESPONSES,
                    "context_length": 400000,
                },
                {
                    "id": "claude-sonnet-5",
                    "api": COPILOT_API_CHAT,
                    "context_length": 264000,
                },
            ]
        )
        assert ok
        terra = saved["copilot-gpt-5.6-terra"]
        assert terra["type"] == "copilot"
        assert terra["name"] == "gpt-5.6-terra"
        assert terra["copilot_api"] == COPILOT_API_RESPONSES
        assert terra["context_length"] == 400000
        assert (
            terra["custom_endpoint"]["url"]
            == "https://api.enterprise.githubcopilot.com"
        )
        assert terra["copilot_host"] == "github.com"
        assert terra["oauth_source"] == "copilot-auth-plugin"
        assert terra["supported_settings"] == ["temperature"]

        claude = saved["copilot-claude-sonnet-5"]
        assert claude["copilot_api"] == COPILOT_API_CHAT
        assert "extended_thinking" in claude["supported_settings"]

    def test_context_length_falls_back_to_known_table_then_default(self):
        ok, saved = self._register(
            [
                {"id": "gpt-4o", "api": COPILOT_API_CHAT, "context_length": None},
                {
                    "id": "mystery-model",
                    "api": COPILOT_API_CHAT,
                    "context_length": None,
                },
            ]
        )
        assert ok
        assert (
            saved["copilot-gpt-4o"]["context_length"]
            == utils.COPILOT_MODEL_CONTEXT_LENGTHS["gpt-4o"]
        )
        assert (
            saved["copilot-mystery-model"]["context_length"]
            == utils.COPILOT_AUTH_CONFIG["default_context_length"]
        )

    def test_accepts_bare_string_ids(self):
        ok, saved = self._register(["gpt-4.1", "", {"no": "id"}])
        assert ok
        assert list(saved) == ["copilot-gpt-4.1"]
        assert saved["copilot-gpt-4.1"]["copilot_api"] == COPILOT_API_CHAT

    def test_preserves_unrelated_existing_entries(self):
        ok, saved = self._register(
            [{"id": "gpt-5.5", "api": COPILOT_API_RESPONSES, "context_length": None}],
            existing={"copilot-old": {"type": "copilot", "name": "old"}},
        )
        assert ok
        assert set(saved) == {"copilot-old", "copilot-gpt-5.5"}

    def test_returns_false_when_save_fails(self):
        with (
            patch.object(utils, "load_copilot_models", return_value={}),
            patch.object(utils, "save_copilot_models", return_value=False),
            patch.object(utils, "get_api_endpoint_for_host", return_value="https://x"),
        ):
            assert add_models_to_config(["gpt-4o"]) is False
