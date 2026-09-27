"""Tests for the jev_grep plugin (smart_grep on TypeSafe's Jev).

Jev is replaced by a fake ``DecisionModel`` that answers the real decision
protocol, so Pydantic AI's output-type -> question mapping is exercised for
real without a network or an API key.
"""

from __future__ import annotations

import os
import textwrap
from dataclasses import dataclass, field

import pytest
from pydantic_ai.models.decision import (
    DecisionModel,
    DecisionRequest,
    DecisionResponse,
    NoulAnswer,
    NoulQuestion,
)

from code_puppy_core_plugins.jev_grep import chunks as chunks_mod
from code_puppy_core_plugins.jev_grep import config, register_callbacks, tool
from code_puppy_core_plugins.jev_grep.chunks import (
    Chunk,
    LineTooLong,
    discover,
    source_chunks,
    windows,
)
from code_puppy_core_plugins.jev_grep.judge import judge
from code_puppy_core_plugins.jev_grep.retrieve import rank, terms
from code_puppy_core_plugins.jev_grep.search import semantic_search


@dataclass(init=False)
class FakeJev(DecisionModel[None]):
    """Answers P(yes)=``hit`` when ``needle`` is in the state, else ``miss``."""

    needle: str
    hit: float
    miss: float
    requests: list[DecisionRequest] = field(default_factory=list)

    def __init__(self, needle: str, hit: float = 0.9, miss: float = 0.1):
        self.needle, self.hit, self.miss = needle, hit, miss
        self.requests = []
        super().__init__()

    @property
    def model_name(self) -> str:
        return "jev-fake"

    @property
    def system(self) -> str:
        return "fake"

    async def decide(self, request, model_settings) -> DecisionResponse:
        self.requests.append(request)
        p = self.hit if self.needle in str(request.state) else self.miss
        answers = {name: NoulAnswer(noul=p) for name in request.questions}
        return DecisionResponse(answers=answers, model_name=self.model_name)


PY_SOURCE = textwrap.dedent(
    '''\
    import os
    import sys


    def reject_expired(session):
        if session.expires_at < now():
            raise PermissionError("expired")
        return session


    class Cache:
        """A cache."""

        def get(self, key):
            return self._data.get(key)
    '''
)


# ---------------------------------------------------------------- chunks


def test_python_chunks_follow_declarations():
    chunks, parser = source_chunks(PY_SOURCE, "a.py")
    assert parser == "python"
    by_symbol = {c.symbol: c for c in chunks}
    imports = by_symbol[None]  # adjacent one-liners merge into one target
    assert (imports.line, imports.end_line) == (1, 2)
    fn = by_symbol["reject_expired"]
    assert (fn.line, fn.end_line) == (3, 8)  # preceding blank gap is absorbed
    assert "raise PermissionError" in fn.text
    assert by_symbol["Cache.get"].text.strip().startswith("def get")


def test_long_function_is_split_into_blocks_covering_every_line():
    body = "\n".join(
        f"    if x == {i}:\n        a = {i}\n        b = {i}\n        c = {i}\n        d = {i}\n        e = {i}"
        for i in range(6)
    )
    src = f"def router(x):\n{body}\n"
    chunks, _ = source_chunks(src, "r.py")
    assert len(chunks) > 1
    assert all(c.symbol == "router" for c in chunks)
    covered = {n for c in chunks for n in range(c.line, c.end_line + 1)}
    assert covered == set(range(1, len(src.splitlines()) + 1))


def test_non_python_and_broken_python_fall_back_to_windows():
    text = "\n".join(f"line {i}" for i in range(130))
    for path in ("x.txt", "broken.py"):
        src = text if path == "x.txt" else "def (:\n" + text
        chunks, parser = source_chunks(src, path)
        assert parser == "overlapping-lines"
        assert chunks[0].line == 1 and chunks[0].end_line == 60
        assert chunks[1].line == 51  # 10-line overlap


def test_giant_line_raises_and_giant_window_halves():
    with pytest.raises(LineTooLong):
        windows(["x" * 13_000], "min.js")
    halves = windows(["y" * 5_000] * 4, "big.txt", size=4, overlap=0)
    assert [(c.line, c.end_line) for c in halves] == [(1, 2), (3, 4)]


def test_discover_skips_binary_and_non_utf8(tmp_path):
    (tmp_path / "good.py").write_text(PY_SOURCE)
    (tmp_path / "bin.dat").write_bytes(b"abc\0def")
    (tmp_path / "latin.txt").write_bytes("caf\xe9".encode("latin-1"))
    found = discover(str(tmp_path))
    assert found.files == 3
    assert {reason for _, reason in found.skipped} == {"binary", "not UTF-8"}
    assert found.parsers["python"] == 1


async def test_repo_root_scale_snippets_are_shortlisted_not_rejected(
    monkeypatch, tmp_path
):
    """Regression: >20k snippets used to abort the search, so agents searching
    a repo root fell back to long grep chains. Only the shortlist is judged."""
    (tmp_path / "big.py").write_text("x = 1\n")
    many = [Chunk(str(tmp_path / "big.py"), i, i, f"line {i}") for i in range(25_000)]
    many[24_321] = Chunk(str(tmp_path / "big.py"), 24_321, 24_321, "expired session")
    monkeypatch.setattr(chunks_mod, "source_chunks", lambda text, path: (many, "x"))

    found = discover(str(tmp_path))
    assert len(found.chunks) == 25_000

    model = FakeJev("expired")
    out = await semantic_search(model, "expired session", str(tmp_path))
    assert out.error is None
    assert out.coverage.snippets == 25_000 and out.coverage.evaluated == 128
    assert len(model.requests) == 128  # judged work stays bounded
    assert out.matches and out.matches[0].start_line == 24_321


def test_discover_still_enforces_byte_budget(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n" * 50)
    monkeypatch.setattr(chunks_mod, "MAX_TOTAL_BYTES", 10)
    with pytest.raises(RuntimeError, match="32 MiB"):
        discover(str(tmp_path))


def test_discover_without_ripgrep(monkeypatch, tmp_path):
    monkeypatch.setattr(chunks_mod, "find_ripgrep", lambda: None)
    with pytest.raises(RuntimeError, match="ripgrep"):
        discover(str(tmp_path))


# ---------------------------------------------------------------- retrieve


def test_terms_split_identifiers_and_drop_stopwords():
    assert terms("where is refreshToken_value for HTTPServer") == [
        "refresh",
        "token",
        "value",
        "httpserver",
    ]


def test_rank_prefers_lexical_and_synonym_hits():
    a = Chunk("a.py", 1, 2, "def render(): pass")
    b = Chunk("b.py", 1, 2, "def backoff(): attempt()")
    c = Chunk("retry.py", 1, 2, "x = 1")
    ranked = rank("retry the request", [a, b, c])
    assert ranked[-1] is a  # no overlap at all
    assert {ranked[0], ranked[1]} == {b, c}


# ---------------------------------------------------------------- judge


async def test_judge_asks_two_probability_questions_and_multiplies():
    model = FakeJev(needle="expired", hit=0.8, miss=0.1)
    hit = Chunk("s.py", 3, 4, "if expired: deny()", symbol="check")
    miss = Chunk("t.py", 1, 1, "print('hi')")
    scores = await judge(model, "reject expired sessions", [hit, miss])
    assert scores == pytest.approx([0.64, 0.01])

    request = model.requests[0]
    assert set(request.questions) == {"entity", "operation"}
    assert all(isinstance(q, NoulQuestion) for q in request.questions.values())
    states = {str(r.state) for r in model.requests}
    assert any("s.py:3-4 (check)" in s for s in states)  # path + symbol in state


# ---------------------------------------------------------------- search


def _repo(tmp_path):
    (tmp_path / "auth.py").write_text(PY_SOURCE)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_auth.py").write_text(
        "def test_expired_session_is_rejected():\n    assert reject_expired(old) is None\n"
    )
    (tmp_path / "noise.py").write_text("def unrelated():\n    return 42\n")
    return tmp_path


async def test_semantic_search_ranks_labels_and_reports_coverage(tmp_path):
    repo = _repo(tmp_path)
    out = await semantic_search(
        FakeJev("expired"), "reject expired sessions", str(repo)
    )
    assert out.error is None
    paths = [os.path.basename(m.file_path) for m in out.matches]  # \ on Windows
    assert set(paths) == {"auth.py", "test_auth.py"}
    test_match = next(m for m in out.matches if m.file_path.endswith("test_auth.py"))
    assert test_match.kind == "test"
    assert out.coverage.selection_complete and out.coverage.files == 3
    assert out.warnings == []


async def test_semantic_search_no_match_limit_and_shortlist(tmp_path):
    repo = _repo(tmp_path)
    none = await semantic_search(FakeJev("zzz"), "reject expired sessions", str(repo))
    assert none.matches == [] and "does not prove absence" in none.warnings[0]

    capped = await semantic_search(
        FakeJev("expired"), "reject expired sessions", str(repo), limit=1, candidates=2
    )
    assert len(capped.matches) == 1 and capped.omitted_matches == 1
    assert capped.coverage.evaluated == 2
    assert any("lexical shortlist" in w for w in capped.warnings)


@pytest.mark.parametrize("requested, expected", [(None, 128), (48, 48), (256, 140)])
async def test_candidate_budget_default_and_overrides(monkeypatch, requested, expected):
    from code_puppy_core_plugins.jev_grep import search
    from code_puppy_core_plugins.jev_grep.chunks import Discovery

    chunks = [Chunk(f"file_{i}.py", 1, 1, "expired") for i in range(140)]
    monkeypatch.setattr(
        search, "discover", lambda directory, glob: Discovery(chunks=chunks, files=140)
    )
    model = FakeJev("expired")
    kwargs = {} if requested is None else {"candidates": requested}
    result = await semantic_search(model, "expired sessions", ".", **kwargs)
    assert result.coverage.evaluated == expected
    assert len(model.requests) == expected


def test_all_candidate_defaults_and_advertised_schema_agree():
    import inspect

    from pydantic_ai import Agent

    from code_puppy_core_plugins.jev_grep.search import DEFAULT_CANDIDATES

    assert DEFAULT_CANDIDATES == 128
    for fn in (semantic_search, tool.run_smart_grep):
        assert inspect.signature(fn).parameters["candidates"].default == 128
    agent = Agent(FakeJev("expired"))
    tool.register_smart_grep(agent)
    registered = agent._function_toolset.tools["smart_grep"]
    schema = registered.function_schema.json_schema
    assert schema["properties"]["candidates"]["default"] == 128


async def test_semantic_search_rejects_bad_query(tmp_path):
    out = await semantic_search(FakeJev("x"), "   ", str(tmp_path))
    assert out.error and "1-2000" in out.error


async def test_overlapping_windows_dedupe_and_excerpt_is_capped(tmp_path):
    lines = [f"filler {i}" for i in range(100)]
    lines[70] = "the retry loop handles backoff"
    (tmp_path / "log.txt").write_text("\n".join(lines))
    out = await semantic_search(FakeJev("retry"), "retry with backoff", str(tmp_path))
    assert len(out.matches) == 1  # windows 1-60 and 51-100 overlap; one survives
    match = out.matches[0]
    assert match.end_line - match.start_line + 1 <= 12
    assert "retry loop" in match.text  # query-focused excerpt window


# ---------------------------------------------------------------- tool + wiring


def _no_key(monkeypatch):
    """No key anywhere: the developer's shell AND shared credential store
    (``get_api_key`` reads the latter first; conftest only isolates puppy.cfg)."""
    for name in config.API_KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "get_api_key", lambda name: "")


def _set_flag(value: str) -> None:
    """Write the flag through real config, exactly as `/set smart_grep ...` does
    (conftest points puppy.cfg at a per-test temp file)."""
    from code_puppy.config import set_config_value

    set_config_value(config.ENABLED_KEY, value)


@pytest.fixture
def smart_grep_on():
    _set_flag("on")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("on", True),
        ("true", True),
        ("1", True),
        ("yes", True),
        ("ON", True),
        ("off", False),
        ("false", False),
        ("0", False),
        ("nope", False),
        ("", False),
    ],
)
def test_flag_parses_like_other_boolean_settings(raw, expected):
    _set_flag(raw)
    assert config.is_enabled() is expected


def test_flag_defaults_off():
    assert config.is_enabled() is False


@pytest.mark.parametrize("flag", ["on", "off", None])
@pytest.mark.parametrize("has_key", [True, False])
def test_one_gate_controls_tool_and_prompt(monkeypatch, flag, has_key):
    """Tool exposure and the prompt policy need BOTH the flag and a key."""
    _no_key(monkeypatch)
    if has_key:
        monkeypatch.setenv(config.API_KEY_NAME, "k")
    if flag is not None:
        _set_flag(flag)
    enabled = flag == "on" and has_key
    assert config.is_available() is enabled
    assert register_callbacks._advertise_when_configured() == (
        ["smart_grep"] if enabled else []
    )
    assert (register_callbacks._discovery_instructions() is not None) is enabled


async def test_execution_rechecks_flag_after_disable(monkeypatch, tmp_path):
    """An agent built while enabled must stop sending source once turned off."""
    monkeypatch.setenv(config.API_KEY_NAME, "k")
    sent = []
    monkeypatch.setattr(
        tool, "build_jev_model", lambda name, key: sent.append(1) or FakeJev("x")
    )
    (tmp_path / "a.py").write_text(PY_SOURCE)
    _set_flag("off")
    out = await tool.run_smart_grep("expired", str(tmp_path))
    assert out.error and "/set smart_grep on" in out.error
    assert not sent  # never reached the model


def test_smart_grep_declares_itself_speculatable():
    """Core launches tools early only when they declare this literally True."""
    from pydantic_ai import Agent

    agent = Agent(FakeJev("x"))
    tool.register_smart_grep(agent)
    assert agent._function_toolset.tools["smart_grep"].metadata == {
        "speculatable": True
    }


def test_settings_use_smart_grep_namespace(monkeypatch):
    seen = []
    monkeypatch.setattr(config, "get_value", lambda key: seen.append(key))
    config.get_jev_model_name()
    config.get_threshold()
    assert seen == ["smart_grep_model", "smart_grep_threshold"]


async def test_tool_requires_api_key(monkeypatch, smart_grep_on):
    _no_key(monkeypatch)
    out = await tool.run_smart_grep("anything")
    assert out.error and all(name in out.error for name in config.API_KEY_NAMES)


def test_api_key_precedence_env_then_config_official_then_alias(monkeypatch):
    _no_key(monkeypatch)
    stored = {"JEV_API_KEY": "cfg-alias"}
    monkeypatch.setattr(config, "get_api_key", lambda name: stored.get(name, ""))
    assert config.get_typesafe_api_key() == "cfg-alias"  # `/set jev_api_key` works
    stored["TYPESAFE_API_KEY"] = "cfg-official"
    assert config.get_typesafe_api_key() == "cfg-official"
    monkeypatch.setenv("JEV_API_KEY", "env-alias")
    assert config.get_typesafe_api_key() == "env-alias"  # env beats config
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-official")
    assert config.get_typesafe_api_key() == "env-official"


async def test_tool_reports_failures_instead_of_raising(
    monkeypatch, tmp_path, smart_grep_on
):
    monkeypatch.setenv(config.API_KEY_NAME, "k")

    class Boom(FakeJev):
        async def decide(self, request, model_settings):
            raise RuntimeError("backend down")

    monkeypatch.setattr(tool, "build_jev_model", lambda name, key: Boom("x"))
    (tmp_path / "a.py").write_text(PY_SOURCE)
    out = await tool.run_smart_grep("expired", str(tmp_path))
    assert out.error and "backend down" in out.error


def test_build_jev_model_is_a_real_typesafe_model():
    from pydantic_ai.models.typesafe import TypeSafeModel

    from code_puppy_core_plugins.jev_grep.judge import build_jev_model

    model = build_jev_model("jev-1.13.0", "secret")
    assert isinstance(model, TypeSafeModel)
    assert model.model_name == "jev-1.13.0"


async def test_tool_success_path_emits_summary_and_registers(
    monkeypatch, tmp_path, smart_grep_on
):
    from pydantic_ai import Agent

    monkeypatch.setenv(config.API_KEY_NAME, "k")
    monkeypatch.setattr(tool, "build_jev_model", lambda name, key: FakeJev("expired"))
    emitted: list[str] = []
    monkeypatch.setattr(tool, "emit_info", emitted.append)
    (tmp_path / "auth.py").write_text(PY_SOURCE)

    out = await tool.run_smart_grep("reject expired sessions", str(tmp_path))
    assert out.error is None and out.matches
    assert emitted and "smart_grep" in str(emitted[0])

    agent = Agent(FakeJev("x"))
    tool.register_smart_grep(agent)
    assert "smart_grep" in agent._function_toolset.tools


def test_tool_only_advertised_when_configured(monkeypatch, smart_grep_on):
    _no_key(monkeypatch)
    assert register_callbacks._advertise_when_configured("code-puppy") == []
    monkeypatch.setenv(config.API_KEY_NAME, "k")
    assert register_callbacks._advertise_when_configured("code-puppy") == ["smart_grep"]
    [entry] = register_callbacks._register_tools()
    assert entry["name"] == "smart_grep"


@pytest.mark.parametrize("name", config.API_KEY_NAMES)
@pytest.mark.parametrize("source", ["environment", "config"])
def test_discovery_prompt_follows_live_key_gate(
    monkeypatch, name, source, smart_grep_on
):
    _no_key(monkeypatch)
    assert register_callbacks._discovery_instructions() is None
    secret = "test-only-secret-never-in-model-prompt"
    if source == "environment":
        monkeypatch.setenv(name, secret)
    else:
        monkeypatch.setattr(
            config, "get_api_key", lambda key: secret if key == name else ""
        )
    prompt = register_callbacks._discovery_instructions()
    assert prompt is not None
    assert "smart_grep first" in prompt
    assert "first tool for gathering code" in prompt
    assert "exhaustive references" in prompt
    assert "known file directly" in prompt
    assert "no matches do not prove absence" in prompt
    assert "fall back to grep" in prompt
    assert "local-only work" in prompt
    assert secret not in prompt
    assert register_callbacks._advertise_when_configured() == ["smart_grep"]
    _no_key(monkeypatch)
    assert register_callbacks._discovery_instructions() is None
    assert register_callbacks._advertise_when_configured() == []


def test_discovery_policy_is_registered_as_prompt_hook(monkeypatch):
    import runpy

    import code_puppy.callbacks as callbacks

    registrations = {}
    monkeypatch.setattr(
        callbacks,
        "register_callback",
        lambda name, fn: registrations.update({name: fn}),
    )
    runpy.run_path(
        register_callbacks.__file__,
        run_name="code_puppy_core_plugins.jev_grep._test_registration",
    )
    assert registrations["load_prompt"].__name__ == "_discovery_instructions"
    assert "register_tools" in registrations
    assert "register_agent_tools" in registrations


def test_threshold_config_is_validated(monkeypatch):
    for raw, expected in (("0.7", 0.7), ("nope", 0.5), ("3", 0.5), (None, 0.5)):
        monkeypatch.setattr(config, "get_value", lambda key, raw=raw: raw)
        assert config.get_threshold() == expected
