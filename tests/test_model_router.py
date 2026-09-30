"""Tests for the model_router plugin."""

from __future__ import annotations

import asyncio
import json
import os
import time
from unittest.mock import patch

import httpx
import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from code_puppy_core_plugins.model_router import register_callbacks as rc
from code_puppy_core_plugins.model_router.config import (
    RouterConfigError,
    api_key_for,
    config_path,
    load_config,
    parse_config,
    save_config,
    template_config,
)
from code_puppy_core_plugins.model_router.judge_client import (
    JudgeAnswers,
    JudgeError,
    JudgeResult,
    ask_judge,
)
from code_puppy_core_plugins.model_router.policy import (
    REASON_JUDGED,
    REASON_JUDGE_ERROR,
    REASON_LOW_CONFIDENCE,
    REASON_NO_API_KEY,
    REASON_ROUNDED_UP,
    REASON_UNKNOWN_MODEL,
    apply_policy,
)
from code_puppy_core_plugins.model_router.router import Router
from code_puppy_core_plugins.model_router.state import build_state, truncate_middle

RAW = {
    "fallback_model": "opus",
    "tiers": ["mini", "sonnet", "opus"],
    "complexity_to_tier": ["mini", "mini", "sonnet", "opus"],
    "risk_floor": {"threshold": 0.7, "min_tier": "sonnet"},
    "confidence_threshold": 0.6,
}
KNOWN = {"mini", "sonnet", "opus", "auto"}


def answers(score=0.4, confidence=0.9, risky=0.1) -> JudgeAnswers:
    return JudgeAnswers(
        complexity_score=score,
        complexity_confidence=confidence,
        complexity_probabilities={"0": 0.6, "1": 0.4},
        risky=risky,
    )


def fake_ask(score=0.4, confidence=0.9, risky=0.1, error: str | None = None):
    calls = []

    def _ask(state, **kwargs):
        calls.append((state, kwargs))
        if error:
            raise JudgeError(error)
        return JudgeResult(
            answers=answers(score, confidence, risky),
            model="jev-latest",
            latency_ms=12,
            usage={"input_tokens": 300},
        )

    _ask.calls = calls
    return _ask


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    from code_puppy import config

    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    return tmp_path / "state"


@pytest.fixture
def router(monkeypatch, state_dir):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-key")
    r = Router(ask=fake_ask())
    monkeypatch.setattr(r, "known_models", lambda: set(KNOWN))
    save_config(parse_config(RAW))
    return r


# -- config ---------------------------------------------------------------------


def test_parse_config_defaults_and_validation():
    cfg = parse_config(RAW)
    assert cfg.virtual_model == "auto"
    assert cfg.judge.base_url == "https://api.typesafe.ai"
    assert cfg.judge.timeout_s == 4.0
    assert cfg.tier_index("sonnet") == 1
    assert cfg.referenced_models() == ["mini", "sonnet", "opus"]

    with pytest.raises(RouterConfigError, match="exactly 4"):
        parse_config({**RAW, "complexity_to_tier": ["mini"]})
    with pytest.raises(RouterConfigError, match="not in 'tiers'"):
        parse_config({**RAW, "complexity_to_tier": ["mini", "mini", "haiku", "opus"]})
    with pytest.raises(RouterConfigError, match="min_tier"):
        parse_config({**RAW, "risk_floor": {"threshold": 0.5, "min_tier": "nope"}})
    with pytest.raises(RouterConfigError, match="virtual model"):
        parse_config({**RAW, "virtual_model": "opus"})
    with pytest.raises(RouterConfigError, match="fallback_model"):
        parse_config({k: v for k, v in RAW.items() if k != "fallback_model"})


def test_config_round_trip_and_missing_file(tmp_path):
    cfg = parse_config(
        {
            **RAW,
            "context_length": 200000,
            "judge": {"base_url": "http://localhost:8080/", "timeout_s": 2},
        }
    )
    path = save_config(cfg, tmp_path / "model_router.json")
    back = load_config(path)
    assert back is not None
    assert back.to_dict() == cfg.to_dict()
    assert back.judge.base_url == "http://localhost:8080"
    assert back.context_length == 200000
    assert load_config(tmp_path / "missing.json") is None
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    with pytest.raises(RouterConfigError, match="invalid JSON"):
        load_config(tmp_path / "bad.json")


def test_template_config_fills_tiers_with_flagship():
    only = template_config("opus")
    assert only.tiers == ["opus"]
    assert only.complexity_to_tier == ["opus"] * 4
    assert only.risk_floor is None
    full = template_config("opus", cheap="mini", mid="sonnet")
    assert full.tiers == ["mini", "sonnet", "opus"]
    assert full.complexity_to_tier == ["mini", "mini", "sonnet", "opus"]
    assert full.risk_floor and full.risk_floor.min_tier == "sonnet"


def test_api_key_from_environment(monkeypatch):
    cfg = parse_config(RAW)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert api_key_for(cfg) is None
    monkeypatch.setenv("TYPESAFE_API_KEY", "abc")
    assert api_key_for(cfg) == "abc"


# -- state ----------------------------------------------------------------------


def test_build_state_from_pydantic_history():
    history = [
        ModelRequest(
            parts=[
                SystemPromptPart(content="You are a puppy."),
                UserPromptPart(content="add a flag"),
            ]
        ),
        ModelResponse(
            parts=[
                TextPart(content="Sure."),
                ToolCallPart(tool_name="edit_file", args={}),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(tool_name="edit_file", content="ok", tool_call_id="1")
            ]
        ),
        ModelResponse(parts=[TextPart(content="Done, the flag is added.")]),
        ModelRequest(parts=[UserPromptPart(content="now tests")]),
        ModelResponse(parts=[TextPart(content="Tests written.")]),
    ]
    state = build_state("and docs please", history, 12000)
    assert state == {
        "request": "and docs please",
        "conversation_turns": 3,
        "system_prompt_excerpt": "You are a puppy.",
        "prior_assistant_excerpt": "Tests written.",
    }


def test_build_state_handles_empty_and_list_prompts():
    assert build_state("   ", [], 1000) is None
    assert build_state(None, [], 1000) is None
    state = build_state(["look at this", object()], [], 1000)
    assert (
        state
        and state["request"] == "look at this"
        and state["conversation_turns"] == 1
    )


def test_truncate_middle_keeps_head_and_tail():
    text = "a" * 700 + "b" * 300
    out = truncate_middle(text, 200)
    assert out.startswith("a" * 140) and out.endswith("b" * 60)
    assert "characters omitted" in out
    long_state = build_state("x" * 500, [], 300)
    assert long_state and "omitted" in long_state["request"]


# -- judge client -----------------------------------------------------------------


def test_ask_judge_sends_questions_and_parses_answers():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "kev-1",
                "answers": {
                    "complexity": {
                        "score": 2.3,
                        "confidence": 0.81,
                        "probabilities": {"2": 0.7, "3": 0.3},
                    },
                    "risky": {"noul": 0.2},
                },
                "usage": {"input_tokens": 500},
            },
        )

    result = ask_judge(
        {"request": "hi", "conversation_turns": 1},
        api_key="k",
        base_url="https://judge.example",
        model="jev-latest",
        timeout_s=2,
        transport=httpx.MockTransport(handler),
    )
    assert seen["url"] == "https://judge.example/v1/systemone"
    assert seen["auth"] == "Bearer k"
    assert seen["body"]["model"] == "jev-latest"
    assert seen["body"]["state"] == {"request": "hi", "conversation_turns": 1}
    assert seen["body"]["questions"]["complexity"]["type"] == "score"
    assert len(seen["body"]["questions"]["complexity"]["criteria"]) == 4
    assert seen["body"]["questions"]["risky"]["type"] == "noul"
    assert result.answers.complexity_score == 2.3
    assert result.answers.complexity_probabilities == {"2": 0.7, "3": 0.3}
    assert result.answers.risky == 0.2
    assert result.model == "kev-1"
    assert result.usage == {"input_tokens": 500}


@pytest.mark.parametrize(
    "response, match",
    [
        (httpx.Response(429, text="slow down"), "HTTP 429"),
        (httpx.Response(200, text="nope"), "non-JSON"),
        (
            httpx.Response(
                200, json={"answers": {"complexity": {"score": 1}, "risky": {}}}
            ),
            "complexity.confidence",
        ),
        (
            httpx.Response(
                200,
                json={
                    "answers": {
                        "complexity": {"score": 1, "confidence": 0.5},
                        "risky": {},
                    }
                },
            ),
            "risky.noul",
        ),
    ],
)
def test_ask_judge_errors(response, match):
    with pytest.raises(JudgeError, match=match):
        ask_judge(
            {},
            api_key="k",
            base_url="https://judge.example",
            model="m",
            timeout_s=1,
            transport=httpx.MockTransport(lambda r: response),
        )


def test_ask_judge_wraps_transport_errors():
    def handler(request):
        raise httpx.ConnectTimeout("timeout")

    with pytest.raises(JudgeError, match="ConnectTimeout"):
        ask_judge(
            {},
            api_key="k",
            base_url="https://judge.example",
            model="m",
            timeout_s=1,
            transport=httpx.MockTransport(handler),
        )


# -- policy ---------------------------------------------------------------------


def test_policy_maps_levels_risk_and_confidence():
    cfg = parse_config(RAW)
    assert apply_policy(answers(score=0.3), cfg).model == "mini"
    assert apply_policy(answers(score=1.6), cfg).model == "sonnet"
    assert apply_policy(answers(score=9), cfg).model == "opus"

    risky = apply_policy(answers(score=0.2, risky=0.9), cfg)
    assert risky.model == "sonnet" and any("risky" in n for n in risky.notes)
    # the floor never lowers a tier
    assert apply_policy(answers(score=3, risky=0.9), cfg).model == "opus"

    # default next_level: unsure between 0 and 1 -> level 1, still mini
    low = apply_policy(answers(score=0.2, confidence=0.3), cfg)
    assert low.model == "mini" and low.reason == REASON_ROUNDED_UP
    assert low.level == 0 and low.confidence == 0.3

    strict = parse_config({**RAW, "low_confidence": "fallback"})
    fb = apply_policy(answers(score=0.2, confidence=0.3), strict)
    assert fb.model == "opus" and fb.reason == REASON_LOW_CONFIDENCE

    no_floor = parse_config({k: v for k, v in RAW.items() if k != "risk_floor"})
    assert apply_policy(answers(score=0.2, risky=0.99), no_floor).model == "mini"


# -- router ---------------------------------------------------------------------


def test_select_routes_only_the_virtual_model(router):
    assert (
        router.select(
            agent_name="a",
            current_model="opus",
            prompt="hi",
            messages=[],
            session_id="s",
        )
        is None
    )
    assert (
        router.select(
            agent_name="a",
            current_model="auto",
            prompt="hi",
            messages=[],
            session_id="s",
        )
        == "mini"
    )
    assert router.last_decision and router.last_decision.reason == REASON_JUDGED
    state, kwargs = router._ask.calls[0]
    assert state["request"] == "hi"
    assert kwargs == {
        "api_key": "ts-key",
        "base_url": "https://api.typesafe.ai",
        "model": "jev-latest",
        "timeout_s": 4.0,
    }


def test_select_respects_disabled_and_missing_config(router):
    cfg = router.config()
    cfg.enabled = False
    save_config(cfg)
    assert (
        router.select(
            agent_name="a",
            current_model="auto",
            prompt="hi",
            messages=[],
            session_id="s",
        )
        is None
    )
    config_path().unlink()
    assert router.config() is None
    assert "no config" in (router.config_error or "")
    assert (
        router.select(
            agent_name="a",
            current_model="auto",
            prompt="hi",
            messages=[],
            session_id="s",
        )
        is None
    )


def test_config_cache_follows_file_changes(router):
    assert router.config().fallback_model == "opus"
    data = {
        **RAW,
        "fallback_model": "sonnet",
        "tiers": ["mini", "sonnet"],
        "complexity_to_tier": ["mini"] * 3 + ["sonnet"],
    }
    config_path().write_text(json.dumps(data), encoding="utf-8")
    os.utime(config_path(), (time.time() + 5, time.time() + 5))
    assert router.config().fallback_model == "sonnet"


def test_decide_falls_back_on_errors(router, monkeypatch):
    cfg = router.config()
    monkeypatch.setattr(router, "_ask", fake_ask(error="boom"))
    with patch("code_puppy_core_plugins.model_router.router.emit_warning", create=True):
        d = router.decide("hi", [], cfg)
    assert d.model == "opus" and d.reason == REASON_JUDGE_ERROR and d.error == "boom"

    monkeypatch.delenv("TYPESAFE_API_KEY")
    with patch("code_puppy.messaging.emit_warning") as warn:
        d = router.decide("hi", [], cfg)
        d2 = router.decide("hi", [], cfg)
    assert d.reason == REASON_NO_API_KEY and d2.reason == REASON_NO_API_KEY
    assert warn.call_count == 1  # warned once


def test_decide_rejects_unknown_tier_models(router, monkeypatch):
    cfg = router.config()
    monkeypatch.setattr(router, "known_models", lambda: {"opus"})
    with patch("code_puppy.messaging.emit_warning"):
        d = router.decide("hi", [], cfg)
    assert d.model == "opus" and d.reason == REASON_UNKNOWN_MODEL
    assert router.missing_models(cfg) == ["mini", "sonnet"]


def test_run_end_logs_routed_runs_only(router, state_dir):
    router.select(
        agent_name="a",
        current_model="auto",
        prompt="hi",
        messages=[],
        session_id="s1",
    )
    entry = router.record_run_end(
        agent_name="a",
        model_name="mini",
        session_id="s1",
        success=True,
        metadata={"usage_input_tokens": 1200, "usage_output_tokens": 80},
    )
    assert entry and entry["model"] == "mini" and entry["input_tokens"] == 1200
    assert entry["decision"]["reason"] == REASON_JUDGED and entry["decision"][
        "judge_usage"
    ] == {"input_tokens": 300}
    assert (
        router.record_run_end(
            agent_name="a",
            model_name="opus",
            session_id="other",
            success=True,
            metadata=None,
        )
        is None
    )
    lines = (state_dir / "model_routes.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["session_id"] == "s1"
    assert router.read_log()[0]["model"] == "mini"


# -- callbacks ------------------------------------------------------------------


def test_load_models_exposes_virtual_model(router, monkeypatch):
    monkeypatch.setattr(rc, "ROUTER", router)
    entry = rc._load_models()
    assert entry == {
        "auto": {
            "type": "model_router",
            "name": "auto",
            "context_length": 128000,
            "description": "Judge-routed: cheapest capable model per turn",
        }
    }
    config_path().unlink()
    router.invalidate()
    assert rc._load_models() == {}


def test_virtual_model_builds_as_fallback(router, monkeypatch):
    monkeypatch.setattr(rc, "ROUTER", router)
    with patch(
        "code_puppy.model_factory.ModelFactory.get_model", return_value="built"
    ) as get_model:
        assert (
            rc._create_virtual_model("auto", {"type": "model_router"}, {"cfg": 1})
            == "built"
        )
    get_model.assert_called_once_with("opus", {"cfg": 1})
    assert rc._register_model_types() == [
        {"type": "model_router", "handler": rc._create_virtual_model}
    ]


def test_model_select_and_run_end_hooks(router, monkeypatch):
    monkeypatch.setattr(rc, "ROUTER", router)
    assert (
        rc._model_select(
            agent_name="a",
            current_model="auto",
            prompt="hi",
            messages=[],
            session_id="s",
        )
        == "mini"
    )
    asyncio.run(
        rc._agent_run_end(
            "a", "mini", "s", True, None, "done", {"usage_input_tokens": 5}
        )
    )
    assert router.read_log()[0]["input_tokens"] == 5


def test_router_command(router, monkeypatch):
    monkeypatch.setattr(rc, "ROUTER", router)
    assert rc._handle_custom_command("/other", "other") is None
    with (
        patch.object(rc, "emit_info") as info,
        patch.object(rc, "emit_error") as error,
        patch.object(rc, "emit_success") as ok,
    ):
        assert (
            rc._handle_custom_command("/router route rename a variable", "router")
            is True
        )
        text = info.call_args[0][0]
        assert "decision: mini (judged" in text and "probabilities" in text
        assert router._ask.calls[-1][0]["request"] == "rename a variable"

        assert rc._handle_custom_command("/router", "router") is True
        assert "tiers:       mini < sonnet < opus" in info.call_args[0][0]

        assert rc._handle_custom_command("/router off", "router") is True
        assert router.config().enabled is False
        ok.assert_called()

        assert rc._handle_custom_command("/router bogus", "router") is True
        error.assert_called()

        assert rc._handle_custom_command("/router route", "router") is True
        assert "give a prompt" in error.call_args[0][0]


def test_router_init_writes_template(router, monkeypatch):
    monkeypatch.setattr(rc, "ROUTER", router)
    config_path().unlink()
    router.invalidate()
    monkeypatch.setattr(router, "known_models", lambda: set(KNOWN))
    with patch.object(rc, "emit_success") as ok, patch.object(rc, "emit_info"):
        rc._handle_custom_command(
            "/router init --flagship opus --cheap mini --mid sonnet", "router"
        )
        ok.assert_called()
        cfg = load_config()
        assert (
            cfg
            and cfg.tiers == ["mini", "sonnet", "opus"]
            and cfg.fallback_model == "opus"
        )
    with patch.object(rc, "emit_warning") as warn:
        rc._handle_custom_command("/router init --flagship opus", "router")
        assert "--force" in warn.call_args[0][0]
    with patch.object(rc, "emit_error") as error, patch.object(rc, "emit_info"):
        rc._handle_custom_command("/router init --flagship ghost --force", "router")
        assert "not registered" in error.call_args[0][0]


def test_help_entry():
    assert rc._custom_help() == [
        (
            "router",
            "Model router: status, models, init, route <prompt>, last, log, on, off",
        )
    ]


# -- completion -----------------------------------------------------------------


def _complete(text: str) -> list[str]:
    from termflow.tui.completion import Document

    from code_puppy_core_plugins.model_router.completer import RouterCompleter

    return [c.text for c in RouterCompleter().get_completions(Document(text), None)]


def test_completer_subcommands_flags_and_models(monkeypatch):
    from code_puppy_core_plugins.model_router import completer

    catalogue = {
        "copilot-gpt-5-mini": {"type": "copilot"},
        "copilot-claude-opus-4.6": {"type": "copilot"},
        "gpt-5": {"type": "openai"},
        "auto": {"type": "model_router"},
    }
    monkeypatch.setattr(completer, "_catalogue", lambda: catalogue)

    assert _complete("/model x") == []
    assert _complete("/router ") == list(completer.SUBCOMMANDS)
    assert _complete("/router in") == ["init"]
    assert _complete("/router init ") == ["--flagship", "--mid", "--cheap", "--force"]
    assert _complete("/router init --fl") == ["--flagship"]
    assert _complete("/router init --flagship ") == [
        "copilot-claude-opus-4.6",
        "copilot-gpt-5-mini",
        "gpt-5",
    ]
    assert _complete("/router init --flagship opus") == ["copilot-claude-opus-4.6"]
    assert _complete("/router init --flagship gpt-5 ") == [
        "--mid",
        "--cheap",
        "--force",
    ]
    assert _complete("/router init --flagship gpt-5 --mid ") == [
        "copilot-claude-opus-4.6",
        "copilot-gpt-5-mini",
        "gpt-5",
    ]
    assert _complete("/router route ") == []
    assert rc._completion_provider().trigger == "/router"


def test_judge_models_lists_registered_names(router, monkeypatch):
    monkeypatch.setattr(rc, "ROUTER", router)
    catalogue = {
        "copilot-gpt-5-mini": {"type": "copilot"},
        "copilot-claude-opus-4.6": {"type": "copilot"},
        "gpt-5": {"type": "openai"},
        "auto": {"type": "model_router"},
    }
    with (
        patch(
            "code_puppy.model_factory.ModelFactory.load_config", return_value=catalogue
        ),
        patch.object(rc, "emit_info") as info,
    ):
        assert rc._handle_custom_command("/router models", "router") is True
    text = info.call_args[0][0]
    assert (
        "[copilot]" in text and "copilot-claude-opus-4.6" in text and "[openai]" in text
    )
    assert "auto" not in text


def test_local_judge_needs_no_api_key(router, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY")
    cfg = parse_config(
        {**RAW, "judge": {"base_url": "http://localhost:8080", "api_key_env": None}}
    )
    assert cfg.judge.api_key_env is None and not cfg.judge.requires_key()
    d = router.decide("hi", [], cfg)
    assert d.reason == REASON_JUDGED and d.model == "mini"
    assert router._ask.calls[-1][1]["api_key"] is None
    assert cfg.to_dict()["judge"]["api_key_env"] is None


def test_ask_judge_omits_auth_header_without_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "answers": {
                    "complexity": {"score": 0, "confidence": 1},
                    "risky": {"noul": 0},
                }
            },
        )

    ask_judge(
        {},
        api_key=None,
        base_url="http://kev",
        model="kev",
        timeout_s=1,
        transport=httpx.MockTransport(handler),
    )
    assert seen["auth"] is None


def _unsure(
    probs: dict, score: float, confidence: float = 0.59, risky: float = 0.05
) -> JudgeAnswers:
    return JudgeAnswers(
        complexity_score=score,
        complexity_confidence=confidence,
        complexity_probabilities=probs,
        risky=risky,
    )


def test_low_confidence_next_level_takes_the_higher_likely_level():
    cfg = parse_config(RAW)
    # the real case from the log: torn between routine and involved -> sonnet, not opus
    d = apply_policy(_unsure({"0": 0.04, "1": 0.27, "2": 0.64, "3": 0.05}, 1.69), cfg)
    assert d.model == "sonnet" and d.reason == REASON_ROUNDED_UP
    assert any("likely levels [1, 2]" in n for n in d.notes)
    # torn between trivial and routine -> both mini
    assert (
        apply_policy(
            _unsure({"0": 0.5, "1": 0.45, "2": 0.05, "3": 0.0}, 0.55), cfg
        ).model
        == "mini"
    )
    # doubt about expert work still goes to the flagship
    d = apply_policy(_unsure({"0": 0.0, "1": 0.1, "2": 0.6, "3": 0.3}, 2.2), cfg)
    assert d.model == "opus" and d.reason == REASON_LOW_CONFIDENCE
    # no probabilities: round up one level
    d = apply_policy(_unsure({}, 1.2), cfg)
    assert d.model == "sonnet" and d.reason == REASON_ROUNDED_UP
    # the risk floor is never undone by rounding
    d = apply_policy(_unsure({"0": 0.55, "1": 0.45}, 0.4, risky=0.95), cfg)
    assert d.model == "sonnet"
    with pytest.raises(RouterConfigError, match="low_confidence"):
        parse_config({**RAW, "low_confidence": "guess"})
    assert parse_config(RAW).to_dict()["low_confidence"] == "next_level"


def test_route_warns_loudly_without_api_key(router, monkeypatch):
    monkeypatch.setattr(rc, "ROUTER", router)
    monkeypatch.delenv("TYPESAFE_API_KEY")
    with (
        patch.object(rc, "emit_warning") as warn,
        patch.object(rc, "emit_info"),
        patch("code_puppy.messaging.emit_warning"),
    ):
        rc._handle_custom_command("/router route run git status", "router")
    assert any("/set typesafe_api_key" in str(c.args[0]) for c in warn.call_args_list)
