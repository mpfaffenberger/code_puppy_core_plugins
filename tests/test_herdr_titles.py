"""Tests for herdr conversation-title and tab-label propagation.

Split out of ``test_herdr_plugin.py`` (which keeps the reporter state
machine + core wiring) so each file stays well under the 600-line cap.

Covers how the ``session_namer`` title reaches herdr: the turn-end
metadata envelope (title rides with tokens), title-only refreshes, the
bounded post-autosave wait for the namer's async write, and the
workspace-tab label that follows the same title changes (set, restore
on session switch, and the exit-time restore with its expected-label
guard).
"""

from __future__ import annotations

from unittest.mock import patch

from code_puppy_core_plugins.herdr.reporter import HerdrReporter
from tests.herdr_test_support import FakeClient

# --- Phase 5: session title propagation -------------------------------------


def test_turn_end_report_carries_title_alongside_tokens():
    """The title rides the same envelope as the tokens (per-source entry
    replacement would otherwise wipe it)."""
    fake = FakeClient()
    r = HerdrReporter(fake)
    payload = {"model": "claude", "context": "42%", "tokens": "48k/200k"}
    with (
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_tokens_payload",
            return_value=payload,
        ),
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_session_title",
            return_value="Fix the flaky test",
        ),
    ):
        r.on_turn_end()
    assert fake.metadata == [(payload, "Fix the flaky test", False)]


def test_turn_end_tokens_report_re_carries_unchanged_title():
    """Every tokens report re-sends the title, even when it didn't change."""
    fake = FakeClient()
    r = HerdrReporter(fake)
    payload = {"model": "claude", "context": "42%", "tokens": "48k/200k"}
    with (
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_tokens_payload",
            return_value=payload,
        ),
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_session_title",
            return_value="Fix the flaky test",
        ),
    ):
        r.on_turn_end()
        r.on_turn_end()
    assert fake.metadata == [
        (payload, "Fix the flaky test", False),
        (payload, "Fix the flaky test", False),
    ]


def test_turn_end_clears_title_on_set_to_none_transition():
    """A tokens report after the title disappeared reports clear_title."""
    fake = FakeClient()
    r = HerdrReporter(fake)
    payload = {"model": "claude", "context": "42%", "tokens": "48k/200k"}
    with (
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_tokens_payload",
            return_value=payload,
        ),
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_session_title",
            side_effect=["Old title", None, None],
        ),
    ):
        r.on_turn_end()  # sets "Old title"
        r.on_turn_end()  # title gone -> clear
        r.on_turn_end()  # still gone -> no clear needed (replacement suffices)
    assert fake.metadata == [
        (payload, "Old title", False),
        (payload, None, True),
        (payload, None, False),
    ]


def test_title_only_report_when_tokens_unavailable():
    """A renamed session reports its title even without a token payload."""
    fake = FakeClient()
    r = HerdrReporter(fake)
    with (
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_tokens_payload",
            return_value=None,
        ),
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_session_title",
            return_value="Plan the harness",
        ),
    ):
        r.on_turn_end()
    assert fake.metadata == [(None, "Plan the harness", False)]


def test_user_prompt_refreshes_title_for_new_session():
    """Resuming/switching sessions shows the new session's title at once,
    or clears a stale one when the new session is unnamed."""
    fake = FakeClient()
    r = HerdrReporter(fake)
    ref = ("auto_session_x", "/tmp/autosaves/auto_session_x.pkl")
    with (
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_session_ref",
            return_value=ref,
        ),
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_session_title",
            return_value="Second conversation",
        ),
    ):
        r.on_user_prompt()
    assert (None, "Second conversation", False) in fake.metadata
    # A switch to an unnamed session clears the previous title.
    with (
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_session_ref",
            return_value=("auto_session_y", "/tmp/autosaves/auto_session_y.pkl"),
        ),
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_session_title",
            return_value=None,
        ),
    ):
        r.on_user_prompt()
    assert (None, None, True) in fake.metadata


def test_post_autosave_reports_title_already_on_disk():
    """When the sidecar already carries a title, no wait is needed."""
    fake = FakeClient()
    r = HerdrReporter(fake)
    with patch(
        "code_puppy_core_plugins.herdr.reporter.sources.current_session_title",
        return_value="Name on disk",
    ):
        r.on_post_autosave()
    assert fake.metadata == [(None, "Name on disk", False)]
    assert r._title_wait is None


def test_post_autosave_no_wait_when_namer_disabled():
    fake = FakeClient()
    r = HerdrReporter(fake)
    with (
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_session_title",
            return_value=None,
        ),
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.naming_enabled",
            return_value=False,
        ),
    ):
        r.on_post_autosave()
    assert fake.metadata == []
    assert r._title_wait is None


def test_post_autosave_arms_bounded_title_wait(monkeypatch):
    """A still-unnamed session arms one background wait that picks the title
    up as soon as the (async) naming job writes it."""
    fake = FakeClient()
    r = HerdrReporter(fake)
    monkeypatch.setattr(
        "code_puppy_core_plugins.herdr.reporter._TITLE_WAIT_INTERVAL_S", 0.02
    )
    titles = iter([None, None, "Fresh name"])
    with (
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_session_title",
            side_effect=lambda: next(titles),
        ),
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.naming_enabled",
            return_value=True,
        ),
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_session_name",
            return_value="auto_session_x",
        ),
    ):
        r.on_post_autosave()
        assert r._title_wait is not None
        r._title_wait.join(timeout=5)
        assert not r._title_wait.is_alive()  # finished
    assert fake.metadata == [(None, "Fresh name", False)]


def test_title_wait_stops_when_session_changes(monkeypatch):
    fake = FakeClient()
    r = HerdrReporter(fake)
    monkeypatch.setattr(
        "code_puppy_core_plugins.herdr.reporter._TITLE_WAIT_INTERVAL_S", 0.02
    )
    monkeypatch.setattr(
        "code_puppy_core_plugins.herdr.reporter._TITLE_WAIT_TIMEOUT_S", 5.0
    )
    names = iter(["auto_session_x", "auto_session_y", "auto_session_y"])
    with (
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_session_title",
            return_value=None,
        ),
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.naming_enabled",
            return_value=True,
        ),
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_session_name",
            side_effect=lambda: next(names, "auto_session_y"),
        ),
    ):
        r.on_post_autosave()
        wait = r._title_wait
        assert wait is not None
        wait.join(timeout=5)
        assert not wait.is_alive()  # bailed early
    # The old session's (never-arriving) title was not reported.
    assert fake.metadata == []


def test_title_wait_single_flight(monkeypatch):
    fake = FakeClient()
    r = HerdrReporter(fake)
    monkeypatch.setattr(
        "code_puppy_core_plugins.herdr.reporter._TITLE_WAIT_INTERVAL_S", 0.2
    )
    monkeypatch.setattr(
        "code_puppy_core_plugins.herdr.reporter._TITLE_WAIT_TIMEOUT_S", 0.5
    )
    with (
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_session_title",
            return_value=None,
        ),
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.naming_enabled",
            return_value=True,
        ),
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_session_name",
            return_value="auto_session_x",
        ),
    ):
        r.on_post_autosave()
        first = r._title_wait
        r.on_post_autosave()  # second autosave while the first wait is live
        assert r._title_wait is first
    r._title_wait.join(timeout=5)
    assert fake.metadata == []


# --- tab label sync --------------------------------------------------------


def test_turn_end_tab_label_follows_title_changes():
    """The workspace tab is relabelled when the title lands or changes,
    and left alone while the title stays put."""
    fake = FakeClient()
    r = HerdrReporter(fake)
    with (
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_tokens_payload",
            return_value=None,
        ),
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_session_title",
            side_effect=["First name", "First name", "Second name"],
        ),
    ):
        r.on_turn_end()  # title lands -> relabel
        r.on_turn_end()  # same title -> no tab job
        r.on_turn_end()  # title changes -> relabel
    assert fake.tab_labels == [("First name", None), ("Second name", "First name")]


def test_session_switch_restores_tab_label():
    """Switching to an unnamed session restores the tab's original label,
    guarded by the label the tab should still be showing."""
    fake = FakeClient()
    r = HerdrReporter(fake)
    with (
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_session_ref",
            return_value=("auto_session_x", "/tmp/autosaves/auto_session_x.pkl"),
        ),
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_session_title",
            return_value="Named session",
        ),
    ):
        r.on_user_prompt()
    with (
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_session_ref",
            return_value=("auto_session_y", "/tmp/autosaves/auto_session_y.pkl"),
        ),
        patch(
            "code_puppy_core_plugins.herdr.reporter.sources.current_session_title",
            return_value=None,
        ),
    ):
        r.on_user_prompt()
    assert fake.tab_labels == [("Named session", None), (None, "Named session")]


def test_shutdown_restores_tab_label_with_expected_guard():
    fake = FakeClient()
    r = HerdrReporter(fake)
    with patch(
        "code_puppy_core_plugins.herdr.reporter.sources.current_session_title",
        return_value="Tab title",
    ):
        r.on_turn_end()
    r.on_shutdown()
    # The restore is enqueued ahead of the release and carries the label
    # the tab should still be showing.
    assert fake.tab_labels == [("Tab title", None), (None, "Tab title")]
    assert fake.closed


def test_shutdown_no_tab_job_when_title_never_set():
    fake = FakeClient()
    r = HerdrReporter(fake)
    r.on_shutdown()
    assert fake.tab_labels == []
    assert fake.closed


def test_post_autosave_noop_when_inactive():
    fake = FakeClient(active=False)
    r = HerdrReporter(fake)
    with patch(
        "code_puppy_core_plugins.herdr.reporter.sources.current_session_title",
        side_effect=AssertionError("must not touch sources when inactive"),
    ):
        r.on_post_autosave()
    assert fake.metadata == []
