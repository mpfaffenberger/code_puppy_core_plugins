"""Which wing a kennel write lands in.

The agent wing resolved to `agent:unknown` for every agent on every call,
because `_agent_name_from_context` probed four attributes that do not exist
on `RunContext`. A default that is always taken looks exactly like a working
feature, so nothing noticed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from code_puppy_core_plugins.puppy_kennel.tools import (
    KennelScopeError,
    _agent_name_from_context,
    _resolve_wing,
)
from code_puppy_core_plugins.puppy_kennel.wings import agent_wing


def ctx(agent):
    """Shaped like the real thing: `.agent`, and nothing else.

    Deliberately not a Mock -- a Mock answers to every attribute, so it would
    satisfy the old probe and hide which field is actually read.
    """
    return SimpleNamespace(agent=agent, deps=None)


def test_the_name_comes_from_the_field_that_has_it():
    assert (
        _agent_name_from_context(ctx(SimpleNamespace(name="code-puppy")))
        == "code-puppy"
    )


def test_the_wing_is_the_agent_not_a_constant():
    name = _agent_name_from_context(ctx(SimpleNamespace(name="code-puppy")))
    assert agent_wing(name) == "agent:code-puppy"


def test_two_agents_do_not_share_one_wing():
    first = agent_wing(_agent_name_from_context(ctx(SimpleNamespace(name="alpha"))))
    second = agent_wing(_agent_name_from_context(ctx(SimpleNamespace(name="beta"))))
    assert first != second


def test_no_name_is_reported_as_absent_not_invented():
    assert _agent_name_from_context(ctx(None)) == ""
    assert _agent_name_from_context(ctx(SimpleNamespace(name=None))) == ""


def test_asking_for_the_agent_wing_without_a_name_refuses():
    with pytest.raises(KennelScopeError):
        _resolve_wing("agent", "", cwd=None)


def test_a_nameless_run_can_still_use_the_repo_wing():
    # The common path must not be collateral damage: writes default to `repo`
    # and never consult the agent name.
    assert _resolve_wing("", "", cwd=None).startswith("repo:")
    assert _resolve_wing("repo", "", cwd=None).startswith("repo:")


def test_a_misleading_context_attribute_does_not_win():
    # The old probe read `context.name` / `context.agent_name`. If either is
    # present alongside a real agent, the real agent must still win.
    misleading = SimpleNamespace(
        agent=SimpleNamespace(name="code-puppy"),
        name="not-the-agent",
        agent_name="also-not-the-agent",
        deps=SimpleNamespace(name="definitely-not"),
    )
    assert _agent_name_from_context(misleading) == "code-puppy"
