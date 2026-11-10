"""Phase 2 tests: multi-wing search, kennel_recall tool, /kennel commands."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.agent_test_support import FakeAgent


@pytest.fixture
def kennel_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Throwaway kennel dir, isolated per test."""
    root = tmp_path / "kennel"
    monkeypatch.setenv("PUPPY_KENNEL_ROOT", str(root))

    import importlib

    from code_puppy_core_plugins.puppy_kennel import config as kennel_config
    from code_puppy_core_plugins.puppy_kennel import kennel as kennel_mod
    from code_puppy_core_plugins.puppy_kennel import state as state_mod

    importlib.reload(kennel_config)
    importlib.reload(state_mod)
    importlib.reload(kennel_mod)
    kennel_mod.initialize()
    # Storage/search tests operate on an enabled kennel; the default-off
    # behavior is covered by test_puppy_kennel_toggle.
    state_mod.set_enabled(True)
    return root


# --------------------------------------------------------------------------- #
# Multi-wing search + dedup
# --------------------------------------------------------------------------- #


def test_search_drawers_multi_dedupes_same_content(kennel_root: Path) -> None:
    """Multi-wing search dedupes by content even when the same text appears
    in more than one wing (e.g. autosaved in repo, then echoed via
    ``kennel_remember`` into another wing).
    """
    from code_puppy_core_plugins.puppy_kennel import kennel, recorder, wings

    recorder.record_run_end(
        agent_name="code-puppy",
        model_name="m",
        session_id="s1",
        success=True,
        response_text="Distinctive verbatim text about porcupines.",
    )
    repo_w = wings.repo_wing()
    agent_w = wings.agent_wing("code-puppy")
    # Echo the same content into the agent wing via the storage layer,
    # mimicking what an explicit ``kennel_remember(wing="agent")`` would do.
    agent_wing_id = kennel.ensure_wing(agent_w)
    agent_room_id = kennel.ensure_room(agent_wing_id, "notes")
    kennel.add_drawer(
        agent_room_id,
        content="Distinctive verbatim text about porcupines.",
        role="note",
        session_id="s1",
        metadata={"agent": "code-puppy"},
    )
    # Sanity: same content now lives in both wings.
    assert kennel.count_drawers() == 2

    # Multi-wing search across BOTH wings must dedupe to ONE result.
    hits = kennel.search_drawers_multi(
        "porcupines", wing_names=[repo_w, agent_w], limit=5
    )
    assert len(hits) == 1
    assert "porcupines" in hits[0].content.lower()


def test_search_drawers_multi_all_wings_when_none(kennel_root: Path) -> None:
    """Passing None for wings = search every wing."""
    from code_puppy_core_plugins.puppy_kennel import kennel, recorder

    recorder.record_run_end(
        agent_name="agent-a",
        model_name="m",
        success=True,
        response_text="Whales communicate via ultrasound.",
    )
    hits = kennel.search_drawers_multi("whales", wing_names=None, limit=5)
    assert len(hits) == 1


def test_search_drawers_multi_empty_query(kennel_root: Path) -> None:
    from code_puppy_core_plugins.puppy_kennel import kennel

    assert kennel.search_drawers_multi("", wing_names=None) == []
    assert kennel.search_drawers_multi("   ", wing_names=None) == []


# --------------------------------------------------------------------------- #
# kennel_recall tool
# --------------------------------------------------------------------------- #


def _make_context(agent_name: str = "code-puppy") -> Any:
    return SimpleNamespace(agent_name=agent_name, deps=None)


def test_kennel_recall_returns_hits(kennel_root: Path) -> None:
    from code_puppy_core_plugins.puppy_kennel import recorder, tools

    recorder.record_run_end(
        agent_name="code-puppy",
        model_name="m",
        session_id="s1",
        success=True,
        response_text="The dingo ate my SQL homework yesterday.",
    )
    agent = FakeAgent()
    tools.register_kennel_recall(agent)
    recall = agent.registered["kennel_recall"]

    out = asyncio.run(recall(_make_context(), "dingo homework"))
    assert out.total_hits >= 1
    assert any("dingo" in d.content.lower() for d in out.drawers)


def test_kennel_recall_empty_query_returns_error(kennel_root: Path) -> None:
    from code_puppy_core_plugins.puppy_kennel import tools

    agent = FakeAgent()
    tools.register_kennel_recall(agent)
    recall = agent.registered["kennel_recall"]

    out = asyncio.run(recall(_make_context(), ""))
    assert out.error is not None
    assert out.total_hits == 0


def test_kennel_recall_scope_repo_only(kennel_root: Path) -> None:
    """scope='repo' should restrict to the repo wing."""
    from code_puppy_core_plugins.puppy_kennel import recorder, tools

    # Autosaved content lives in the repo wing now (no dual-write).
    recorder.record_run_end(
        agent_name="code-puppy",
        model_name="m",
        session_id="s1",
        success=True,
        response_text="Aardvarks are nocturnal.",
    )
    agent = FakeAgent()
    tools.register_kennel_recall(agent)
    recall = agent.registered["kennel_recall"]

    out = asyncio.run(recall(_make_context(), "aardvarks", scope="repo"))
    assert len(out.wings_searched) == 1
    assert out.wings_searched[0].startswith("repo:")
    assert out.total_hits >= 1


def test_register_tools_callback_shape() -> None:
    """The callback contract: list of dicts with name + register_func.

    The full surface assertion lives in test_puppy_kennel_tools; this one
    just sanity-checks that kennel_recall is in the set.
    """
    from code_puppy_core_plugins.puppy_kennel import tools

    result = tools.register_tools_callback()
    assert isinstance(result, list) and len(result) >= 1
    names = {s["name"] for s in result}
    assert "kennel_recall" in names
    for spec in result:
        assert callable(spec["register_func"])


# --------------------------------------------------------------------------- #
# /kennel slash commands
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "cmd, name, record_text, expected",
    [
        ("/notkennel", "notkennel", None, None),
        ("/kennel help", "kennel", None, True),
        ("/kennel ?", "kennel", None, True),
        ("/kennel wings", "kennel", "Some wisdom.", True),
        ("/kennel wings", "kennel", None, True),
        ("/kennel stats", "kennel", None, True),
        ("/kennel search", "kennel", None, True),
        (
            "/kennel search octopi",
            "kennel",
            "Octopi have nine brains, technically.",
            True,
        ),
        ("/kennel", "kennel", None, True),
        # Should not return None — we own /kennel, we handle it.
        ("/kennel bogus", "kennel", None, True),
    ],
    ids=[
        "ignores_other_names",
        "help",
        "question_mark",
        "wings_with_data",
        "wings_empty",
        "stats",
        "search_no_query",
        "search_with_hits",
        "default_overview",
        "unknown_subcommand",
    ],
)
def test_kennel_commands(
    kennel_root: Path, cmd: str, name: str, record_text: str | None, expected
) -> None:
    from code_puppy_core_plugins.puppy_kennel import commands, recorder

    if record_text:
        recorder.record_run_end(
            agent_name="code-puppy",
            model_name="m",
            success=True,
            response_text=record_text,
        )
    assert commands.handle(cmd, name) is expected


def test_help_entries_advertised() -> None:
    from code_puppy_core_plugins.puppy_kennel import commands

    entries = commands.help_entries()
    assert any(name == "kennel" for name, _desc in entries)


def test_autosave_rooms_group_by_conversation_not_run_id(
    kennel_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same conversation → one short session-<8hex> room; run UUID stays on drawer."""
    import json

    import code_puppy.config as cp_config
    from code_puppy_core_plugins.puppy_kennel import kennel, recorder, wings

    room_a = wings.room_name("auto_session_window_a")
    room_b = wings.room_name("auto_session_window_b")
    assert room_a.startswith("session-") and len(room_a) == len("session-") + 8
    assert room_a != room_b

    monkeypatch.setattr(
        cp_config, "get_current_session_name", lambda: "auto_session_window_a"
    )
    recorder.record_run_end(
        agent_name="code-puppy",
        model_name="m",
        session_id="aaaaaaaa-1111-1111-1111-111111111111",
        success=True,
        response_text="Turn one in window A.",
    )
    recorder.record_run_end(
        agent_name="code-puppy",
        model_name="m",
        session_id="bbbbbbbb-2222-2222-2222-222222222222",
        success=True,
        response_text="Turn two in window A.",
    )

    monkeypatch.setattr(
        cp_config, "get_current_session_name", lambda: "auto_session_window_b"
    )
    recorder.record_run_end(
        agent_name="code-puppy",
        model_name="m",
        session_id="cccccccc-3333-3333-3333-333333333333",
        success=True,
        response_text="Turn one in window B.",
    )

    with kennel._connect() as conn:
        rooms = conn.execute(
            "SELECT r.name, COUNT(d.id) AS n FROM rooms r "
            "LEFT JOIN drawers d ON d.room_id = r.id "
            "GROUP BY r.id, r.name ORDER BY r.name"
        ).fetchall()
        drawers = conn.execute(
            "SELECT content, session_id, metadata FROM drawers ORDER BY id"
        ).fetchall()

    room_counts = {r["name"]: int(r["n"]) for r in rooms}
    assert room_counts == {room_a: 2, room_b: 1}

    # drawers.session_id keeps per-run UUID semantics.
    assert drawers[0]["session_id"] == "aaaaaaaa-1111-1111-1111-111111111111"
    assert drawers[1]["session_id"] == "bbbbbbbb-2222-2222-2222-222222222222"
    assert drawers[2]["session_id"] == "cccccccc-3333-3333-3333-333333333333"

    meta0 = json.loads(drawers[0]["metadata"])
    assert meta0["conversation_id"] == "auto_session_window_a"


def test_room_name_legacy_short_hex_shape() -> None:
    import hashlib

    from code_puppy_core_plugins.puppy_kennel.wings import UNKNOWN_ROOM, room_name

    conv = "auto_session_20260101_120000_123456_99"
    expected = "session-" + hashlib.sha256(conv.encode()).hexdigest()[:8]
    assert room_name(conv) == expected
    assert room_name("same") == room_name("same")
    assert room_name("a") != room_name("b")
    assert room_name("") == UNKNOWN_ROOM
    assert room_name("   ") == UNKNOWN_ROOM


def test_conversation_session_name_failure_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import code_puppy.config as cp_config

    from code_puppy_core_plugins.puppy_kennel import wings

    def _boom() -> str:
        raise RuntimeError("no session")

    monkeypatch.setattr(cp_config, "get_current_session_name", _boom)
    assert wings.conversation_session_name() == ""
    assert wings.room_name(wings.conversation_session_name()) == wings.UNKNOWN_ROOM


def test_recent_drawers_exclude_room_name(kennel_root: Path) -> None:
    from code_puppy_core_plugins.puppy_kennel import kennel, wings

    repo_w = wings.repo_wing()
    wing_id = kennel.ensure_wing(repo_w)
    room_cur = kennel.ensure_room(wing_id, "session-current")
    room_other = kennel.ensure_room(wing_id, "session-other")
    long = "x" * 100
    kennel.add_drawer(room_cur, content=f"CURRENT_ROOM_MARKER {long}", role="assistant")
    kennel.add_drawer(room_other, content=f"OTHER_ROOM_MARKER {long}", role="assistant")

    hits = kennel.recent_drawers(
        repo_w,
        limit=10,
        role="assistant",
        exclude_room_name="session-current",
    )
    texts = [d.content for d in hits]
    assert any("OTHER_ROOM_MARKER" in t for t in texts)
    assert not any("CURRENT_ROOM_MARKER" in t for t in texts)


def test_packer_p2_excludes_current_conversation_room(
    kennel_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2 must not re-inject assistants from the live conversation room."""
    import code_puppy.config as cp_config
    from code_puppy_core_plugins.puppy_kennel import kennel, packer, wings

    monkeypatch.setattr(
        cp_config, "get_current_session_name", lambda: "auto_session_live"
    )

    repo_w = wings.repo_wing()
    wing_id = kennel.ensure_wing(repo_w)
    live_room = wings.room_name("auto_session_live")
    other_room = wings.room_name("auto_session_other")
    live_id = kennel.ensure_room(wing_id, live_room)
    other_id = kennel.ensure_room(wing_id, other_room)
    pad = " " + ("detail " * 20)
    kennel.add_drawer(
        live_id,
        content=f"LIVE_SESSION_ASSISTANT_UNIQUE{pad}",
        role="assistant",
    )
    kennel.add_drawer(
        other_id,
        content=f"OTHER_SESSION_ASSISTANT_UNIQUE{pad}",
        role="assistant",
    )

    block = packer.pack()
    assert block is not None
    assert "OTHER_SESSION_ASSISTANT_UNIQUE" in block
    assert "LIVE_SESSION_ASSISTANT_UNIQUE" not in block
