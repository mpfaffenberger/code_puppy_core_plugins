"""ACPSession.steer: content validation, running state, per-session routing."""

from __future__ import annotations

import base64
from contextlib import nullcontext

import pytest
from acp.schema import (
    BlobResourceContents,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    TextContentBlock,
)

from tests.acp_session_support import (  # noqa: F401 (autouse fixture)
    FakeAgent,
    install_pause_store,
    make_session,
    running_turn,
    text_block,
)

B64 = base64.b64encode(b"%PDF-1.4").decode()

UNSUPPORTED_BLOCKS = {
    "image": ImageContentBlock(type="image", data=B64, mime_type="image/png"),
    "blob-resource": EmbeddedResourceContentBlock(
        type="resource",
        resource=BlobResourceContents(
            uri="file:///a.pdf", blob=B64, mime_type="application/pdf"
        ),
    ),
    "empty-text": TextContentBlock(type="text", text="   "),
}


@pytest.mark.parametrize("running", [True, False], ids=["running", "idle"])
@pytest.mark.parametrize("block_name", list(UNSUPPORTED_BLOCKS))
async def test_unsupported_content_is_rejected_before_running_state(
    monkeypatch, block_name, running
):
    store = install_pause_store(monkeypatch)
    session = make_session("s1", FakeAgent(script=["block"]))
    context = running_turn(session) if running else nullcontext()

    async with context:
        result = session.steer([UNSUPPORTED_BLOCKS[block_name]])

    assert result == {"accepted": False, "reason": "unsupported_content"}
    assert store.controller_for("s1").steers == []


async def test_steer_when_idle_reports_not_running(monkeypatch):
    store = install_pause_store(monkeypatch)

    result = make_session("s1").steer([text_block("hi")])

    assert result == {"accepted": False, "reason": "not_running"}
    assert store.controller_for("s1").steers == []


async def test_steer_while_running_reaches_its_own_controller(monkeypatch):
    store = install_pause_store(monkeypatch)
    session = make_session("s1", FakeAgent(script=["block"]))

    async with running_turn(session):
        result = session.steer([text_block("hello")])

    assert result == {"accepted": True}
    assert store.controller_for("s1").steers == [("hello", "now", True)]


async def test_steer_routes_only_to_the_session_it_targets(monkeypatch):
    store = install_pause_store(monkeypatch)
    session_a = make_session("A", FakeAgent(script=["block"]))
    session_b = make_session("B", FakeAgent(script=["block", "block"]))

    async with running_turn(session_a):
        async with running_turn(session_b):
            assert session_a.steer([text_block("steer-a")]) == {"accepted": True}
            assert session_b.steer([text_block("steer-b")]) == {"accepted": True}
            assert store.controller_for("A").steers == [("steer-a", "now", True)]
            assert store.controller_for("B").steers == [("steer-b", "now", True)]

        # B's first run has finished; a new B run starts with fresh state.
        async with running_turn(session_b):
            assert session_b.steer([text_block("steer-b2")]) == {"accepted": True}
            assert store.controller_for("A").steers == [("steer-a", "now", True)]
            assert store.controller_for("B").steers == [
                ("steer-b", "now", True),
                ("steer-b2", "now", True),
            ]

    assert session_a.steer([text_block("late")]) == {
        "accepted": False,
        "reason": "not_running",
    }
    assert store.controller_for("A").steers == [("steer-a", "now", True)]
