"""Tests for the Meta Muse OAuth key mint and plan reporting."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from code_puppy_core_plugins.meta_oauth import plan, register_callbacks, utils
from code_puppy_core_plugins.meta_oauth.config import (
    DEFAULT_REASONING_EFFORT,
    reasoning_effort_choices,
)


def _response(payload: dict[str, object], status: int = 200) -> MagicMock:
    response = MagicMock()
    response.ok = 200 <= status < 300
    response.status_code = status
    response.json.return_value = payload
    return response


def _fake_t(key: str, **kwargs: object) -> str:
    args = ",".join(f"{name}={value}" for name, value in sorted(kwargs.items()))
    return f"{key}[{args}]" if args else key


class TestMintApiCredentials:
    def test_sends_json_body(self) -> None:
        """Regression: Meta rejects a body-less mint POST ("Request body is required")."""
        minted = {"api_key": "sk-muse", "base_url": "https://api.meta.ai/v1"}
        with patch.object(
            utils.requests, "post", return_value=_response(minted)
        ) as post:
            credentials = utils.mint_api_credentials("access-token")

        assert post.call_args.kwargs["json"] == {}
        assert (
            post.call_args.kwargs["headers"]["Authorization"] == "Bearer access-token"
        )
        assert credentials["api_key"] == "sk-muse"
        assert credentials["api_base_url"] == "https://api.meta.ai/v1"
        assert "base_url" not in credentials

    def test_payment_required_surfaces_detail_and_action_url(self) -> None:
        refusal = {
            "require_payment": True,
            "detail": "Muse Code requires a subscription",
            "action_url": "https://accountscenter.meta.com/muse_code/",
        }
        with patch.object(utils.requests, "post", return_value=_response(refusal)):
            with pytest.raises(RuntimeError) as excinfo:
                utils.mint_api_credentials("access-token")

        message = str(excinfo.value)
        assert "Muse Code requires a subscription" in message
        assert "https://accountscenter.meta.com/muse_code/" in message

    def test_http_error_uses_problem_detail(self) -> None:
        problem = {"title": "Bad Request", "detail": "nope"}
        with patch.object(utils.requests, "post", return_value=_response(problem, 400)):
            with pytest.raises(RuntimeError, match="nope"):
                utils.mint_api_credentials("access-token")


class TestReasoningConfig:
    """Meta Muse effort scale: docs say "none" 400s on Wonder/Spark and
    "max" is unavailable on muse-spark-1.3-contributor."""

    @pytest.mark.parametrize("model_id", ["muse-wonder-1.0", "muse-spark-1.3"])
    def test_full_scale_includes_max_without_none(self, model_id: str) -> None:
        choices = reasoning_effort_choices(model_id)
        assert "max" in choices and "minimal" in choices
        assert "none" not in choices

    def test_contributor_omits_max(self) -> None:
        choices = reasoning_effort_choices("muse-spark-1.3-contributor")
        assert "max" not in choices and "none" not in choices
        assert "xhigh" in choices

    def test_load_meta_models_advertises_choices_and_default(self) -> None:
        credentials = {
            "models": {
                "muse-spark-1.3-contributor": {
                    "context_length": 1_007_997,
                    "supported_settings": ["reasoning_effort"],
                }
            }
        }
        with patch.object(
            register_callbacks, "load_credentials", return_value=credentials
        ):
            models = register_callbacks._load_meta_models()

        entry = models["meta-muse-spark-1.3-contributor"]
        assert "none" not in entry["setting_choices"]["reasoning_effort"]
        assert "max" not in entry["setting_choices"]["reasoning_effort"]
        assert entry["setting_defaults"]["reasoning_effort"] == (
            DEFAULT_REASONING_EFFORT
        )

    def test_created_model_requests_encrypted_reasoning(self) -> None:
        """Profile must opt into include=["reasoning.encrypted_content"],
        or pydantic-ai drops thinking blocks on replay."""
        credentials = {
            "api_key": "sk-test",
            "api_base_url": "https://api.meta.ai/v1",
        }
        with patch.object(
            register_callbacks, "load_credentials", return_value=credentials
        ):
            model = register_callbacks._create_meta_oauth_model(
                "meta-muse-spark-1.3",
                {"type": "meta_oauth", "name": "muse-spark-1.3"},
                {},
            )

        profile = model.profile
        assert profile["openai_supports_encrypted_reasoning_content"]
        assert profile["openai_supports_reasoning"]
        assert profile["openai_supports_minimal_reasoning_effort"]
        assert not profile["openai_supports_reasoning_effort_none"]
        # Regression: dropping phase on replay makes Muse end runs on preambles.
        assert profile["openai_supports_phase"]
        # Without a requested summary Meta streams no reasoning until it ends.
        assert model.settings == {"openai_reasoning_summary": "auto"}


class TestDescribePlan:
    def test_full_plan(self) -> None:
        credentials = {
            "subs_tier_name": "Pro",
            "is_subs_active": True,
            "is_subs_upgrade_available": True,
            "subs_usage": {
                "window": {
                    "used_percent": 12.5,
                    "window_duration_mins": 300,
                    "resets_at": "2026-10-12T20:00:00Z",
                },
                "weekly": {"used_percent": 40},
            },
        }
        with patch.object(plan, "t", _fake_t):
            lines = plan.describe_plan(credentials)

        assert lines == [
            "plugin.meta-oauth.plan.tier[tier=Pro]",
            "plugin.meta-oauth.plan.active",
            "plugin.meta-oauth.plan.usage_reset[reset=2026-10-12T20:00:00Z,used=12.5,"
            "window=plugin.meta-oauth.plan.window_hours[hours=5]]",
            "plugin.meta-oauth.plan.usage[used=40,window=plugin.meta-oauth.plan.weekly]",
            "plugin.meta-oauth.plan.upgrade",
        ]

    def test_muse_cli_subs_active_key(self) -> None:
        with patch.object(plan, "t", _fake_t):
            assert plan.describe_plan({"subs_active": False}) == [
                "plugin.meta-oauth.plan.inactive"
            ]

    def test_no_plan_data(self) -> None:
        with patch.object(plan, "t", _fake_t):
            assert plan.describe_plan({"api_key": "sk"}) == []

    def test_ignores_malformed_usage(self) -> None:
        credentials = {
            "subs_usage": {"window": "junk", "weekly": {"used_percent": "x"}}
        }
        with patch.object(plan, "t", _fake_t):
            assert plan.describe_plan(credentials) == []


class TestSaveCredentials:
    """Regression: os.fchmod is POSIX-only and its failure leaked the temp fd,
    so the cleanup unlink then crashed on Windows with WinError 32."""

    def test_writes_payload_without_fchmod(self, tmp_path, monkeypatch) -> None:
        destination = tmp_path / "meta_oauth.json"
        monkeypatch.setattr(utils, "get_token_storage_path", lambda: destination)
        monkeypatch.delattr(utils.os, "fchmod", raising=False)

        assert utils.save_credentials({"api_key": "sk-muse", "_source": "x"})

        assert json.loads(destination.read_text(encoding="utf-8")) == {
            "api_key": "sk-muse"
        }
        assert [p.name for p in tmp_path.iterdir()] == ["meta_oauth.json"]

    def test_failure_returns_false_and_cleans_up(self, tmp_path, monkeypatch) -> None:
        destination = tmp_path / "meta_oauth.json"
        monkeypatch.setattr(utils, "get_token_storage_path", lambda: destination)

        def boom(*_args: object) -> None:
            raise OSError("disk on fire")

        monkeypatch.setattr(utils.os, "replace", boom)

        assert utils.save_credentials({"api_key": "sk-muse"}) is False
        assert list(tmp_path.iterdir()) == []
