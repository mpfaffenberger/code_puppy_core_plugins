"""Unit tests for the Codex /models catalog parser."""

import json
from unittest.mock import patch

from code_puppy_core_plugins.chatgpt_oauth import utils
from code_puppy_core_plugins.chatgpt_oauth.model_catalog import (
    CodexModelInfo,
    fallback_catalog,
    parse_model_catalog,
)


class TestEffectiveContextLength:
    def test_computes_effective_window_from_catalog(self):
        catalog = parse_model_catalog(
            {
                "models": [
                    {
                        "slug": "gpt-5.6-sol",
                        "context_window": 372000,
                        "max_context_window": 372000,
                        "effective_context_window_percent": 95,
                    }
                ]
            }
        )

        assert catalog == [CodexModelInfo("gpt-5.6-sol", 353400)]

    def test_current_rolled_back_window(self):
        catalog = parse_model_catalog(
            {
                "models": [
                    {
                        "slug": "gpt-5.6-sol",
                        "context_window": 272000,
                        "effective_context_window_percent": 95,
                    }
                ]
            }
        )

        assert catalog[0].context_length == 258400

    def test_defaults_percent_to_95_when_missing(self):
        catalog = parse_model_catalog(
            {"models": [{"slug": "m", "context_window": 100000}]}
        )

        assert catalog[0].context_length == 95000

    def test_falls_back_to_max_context_window(self):
        catalog = parse_model_catalog(
            {
                "models": [
                    {
                        "slug": "m",
                        "max_context_window": 200000,
                        "effective_context_window_percent": 50,
                    }
                ]
            }
        )

        assert catalog[0].context_length == 100000

    def test_context_window_wins_over_max_context_window(self):
        """Pin codex-rs precedence (openai_models.rs resolved_context_window):
        context_window is preferred when both fields are present."""
        catalog = parse_model_catalog(
            {
                "models": [
                    {
                        "slug": "gpt-5.6-sol",
                        "context_window": 273000,
                        "max_context_window": 400000,
                        "effective_context_window_percent": 95,
                    }
                ]
            }
        )

        assert catalog[0].context_length == 259350  # 273_000 * 95 // 100

    def test_garbage_small_windows_are_treated_as_missing(self):
        """A hostile catalog must not be able to shrink the budget to ~1 token
        (server-wins would otherwise persist it)."""
        catalog = parse_model_catalog(
            {
                "models": [
                    {"slug": "tiny-raw", "context_window": 2},
                    {"slug": "rounds-to-zero", "context_window": 1},
                    {"slug": "legit", "context_window": 20000},
                ]
            }
        )

        assert catalog[0].context_length is None
        assert catalog[1].context_length is None
        assert catalog[2].context_length == 19000

    def test_ignores_nonpositive_and_junk_windows(self):
        catalog = parse_model_catalog(
            {
                "models": [
                    {"slug": "zero", "context_window": 0},
                    {"slug": "negative", "context_window": -5},
                    {"slug": "bool", "context_window": True},
                    {"slug": "text", "context_window": "lots"},
                    {"slug": "none"},
                ]
            }
        )

        assert [entry.context_length for entry in catalog] == [None] * 5

    def test_clamps_bogus_percent_to_default(self):
        catalog = parse_model_catalog(
            {
                "models": [
                    {
                        "slug": "over",
                        "context_window": 100000,
                        "effective_context_window_percent": 150,
                    },
                    {
                        "slug": "zero-pct",
                        "context_window": 100000,
                        "effective_context_window_percent": 0,
                    },
                ]
            }
        )

        assert catalog[0].context_length == 95000
        assert catalog[1].context_length == 95000


class TestParseModelCatalog:
    def test_extracts_names_in_order_and_dedupes(self):
        catalog = parse_model_catalog(
            {
                "models": [
                    {"slug": "gpt-5.6-sol"},
                    {"id": "by-id"},
                    {"name": "by-name"},
                    {"slug": "gpt-5.6-sol"},  # duplicate
                    None,
                    "not-a-dict",
                    {"slug": "   "},
                    {},
                ]
            }
        )

        assert [entry.name for entry in catalog] == [
            "gpt-5.6-sol",
            "by-id",
            "by-name",
        ]

    def test_strips_whitespace_from_names(self):
        catalog = parse_model_catalog({"models": [{"slug": "  gpt-5.6-sol  "}]})

        assert catalog[0].name == "gpt-5.6-sol"

    def test_returns_empty_for_malformed_payloads(self):
        assert parse_model_catalog(None) == []
        assert parse_model_catalog([]) == []
        assert parse_model_catalog({}) == []
        assert parse_model_catalog({"models": "nope"}) == []
        assert parse_model_catalog({"models": []}) == []


class TestFallbackCatalog:
    def test_builds_metadata_free_entries(self):
        catalog = fallback_catalog(["gpt-5.6-sol", "gpt-5.5"])

        assert catalog == [CodexModelInfo("gpt-5.6-sol"), CodexModelInfo("gpt-5.5")]


class TestRegistrationContextResolution:
    """context_length precedence at registration: server > table > default."""

    def test_server_context_wins_over_fallback_table(self, tmp_path):
        with patch.object(
            utils, "get_chatgpt_models_path", return_value=tmp_path / "models.json"
        ):
            assert utils.add_models_to_extra_config(
                [CodexModelInfo(name="gpt-5.6-sol", context_length=353400)]
            )

            assert (
                utils.load_chatgpt_models()["codex-gpt-5.6-sol"]["context_length"]
                == 353400
            )

    def test_fallback_never_claims_api_spec_context(self, tmp_path):
        """Without catalog metadata, GPT-5.6 must fall back to the effective
        window of the rolled-back catalog (258,400) — never the 1.05M raw
        API model spec."""
        with patch.object(
            utils, "get_chatgpt_models_path", return_value=tmp_path / "models.json"
        ):
            assert utils.add_models_to_extra_config(
                [
                    CodexModelInfo(name="gpt-5.6-sol"),
                    CodexModelInfo(name="gpt-5.6-luna"),
                ]
            )

            loaded = utils.load_chatgpt_models()
            assert loaded["codex-gpt-5.6-sol"]["context_length"] == 258400
            assert loaded["codex-gpt-5.6-luna"]["context_length"] == 258400

    def test_per_model_overrides_still_apply(self, tmp_path):
        with patch.object(
            utils, "get_chatgpt_models_path", return_value=tmp_path / "models.json"
        ):
            assert utils.add_models_to_extra_config(["gpt-5.3-codex-spark"])

            assert (
                utils.load_chatgpt_models()["codex-gpt-5.3-codex-spark"][
                    "context_length"
                ]
                == 131000
            )


class TestLegacyContextLengthSelfHeal:
    """Pre-fix installs persisted 1.05M; loading must not keep serving it."""

    def _write_models(self, tmp_path, payload):
        import json as jsonlib

        models_path = tmp_path / "models.json"
        models_path.write_text(jsonlib.dumps(payload))
        return models_path

    def test_legacy_1050000_swapped_in_memory_only(self, tmp_path):
        models_path = self._write_models(
            tmp_path,
            {
                "codex-gpt-5.6-sol": {
                    "type": "chatgpt_oauth",
                    "name": "gpt-5.6-sol",
                    "oauth_source": "chatgpt-oauth-plugin",
                    "context_length": 1050000,
                }
            },
        )

        with patch.object(utils, "get_chatgpt_models_path", return_value=models_path):
            loaded = utils.load_chatgpt_models()

        assert loaded["codex-gpt-5.6-sol"]["context_length"] == 258400
        # Disk is untouched — the file is rewritten with live catalog data on
        # the next /chatgpt-auth, never silently mutated by a read.
        assert (
            json.loads(models_path.read_text())["codex-gpt-5.6-sol"]["context_length"]
            == 1050000
        )

    def test_live_catalog_values_and_foreign_entries_untouched(self, tmp_path):
        models_path = self._write_models(
            tmp_path,
            {
                "codex-gpt-5.6-sol": {
                    "name": "gpt-5.6-sol",
                    "oauth_source": "chatgpt-oauth-plugin",
                    "context_length": 353400,
                },
                "user-custom": {
                    "name": "whatever",
                    "context_length": 1050000,
                },
            },
        )

        with patch.object(utils, "get_chatgpt_models_path", return_value=models_path):
            loaded = utils.load_chatgpt_models()

        assert loaded["codex-gpt-5.6-sol"]["context_length"] == 353400
        assert loaded["user-custom"]["context_length"] == 1050000


class TestModelsConfigBridge:
    """The load_models_config hook must carry healed values past core's raw
    file read (ModelFactory merges hook results after the JSON file sources)."""

    def test_bridge_serves_healed_values_to_model_factory(self, tmp_path):
        from code_puppy_core_plugins.chatgpt_oauth import register_callbacks

        models_path = tmp_path / "models.json"
        models_path.write_text(
            json.dumps(
                {
                    "codex-gpt-5.6-sol": {
                        "name": "gpt-5.6-sol",
                        "oauth_source": "chatgpt-oauth-plugin",
                        "context_length": 1050000,
                    }
                }
            )
        )

        with patch.object(utils, "get_chatgpt_models_path", return_value=models_path):
            bridged = register_callbacks._load_models_config()

        assert bridged["codex-gpt-5.6-sol"]["context_length"] == 258400

    def test_malformed_entries_do_not_crash_add_or_remove(self, tmp_path):
        models_path = tmp_path / "models.json"
        models_path.write_text(
            json.dumps(
                {
                    "codex-garbage": "oops",
                    "codex-gpt-5.6-sol": {
                        "name": "gpt-5.6-sol",
                        "oauth_source": "chatgpt-oauth-plugin",
                        "context_length": 353400,
                    },
                }
            )
        )

        with patch.object(utils, "get_chatgpt_models_path", return_value=models_path):
            assert utils.add_models_to_extra_config(["gpt-5.6-sol"])
            loaded = utils.load_chatgpt_models()

        # Garbage survives untouched; the real model is rewritten cleanly.
        assert loaded["codex-garbage"] == "oops"
        assert loaded["codex-gpt-5.6-sol"]["context_length"] == 258400

        with patch.object(utils, "get_chatgpt_models_path", return_value=models_path):
            removed = utils.remove_chatgpt_models()

        assert removed == 1
