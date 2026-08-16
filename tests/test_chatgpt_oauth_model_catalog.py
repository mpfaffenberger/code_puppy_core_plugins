"""Unit tests for the Codex /models catalog parser."""

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
                        "context_window": 1000,
                        "effective_context_window_percent": 150,
                    },
                    {
                        "slug": "zero-pct",
                        "context_window": 1000,
                        "effective_context_window_percent": 0,
                    },
                ]
            }
        )

        assert catalog[0].context_length == 950
        assert catalog[1].context_length == 950


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
