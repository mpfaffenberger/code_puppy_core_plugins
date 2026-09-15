"""Map code-puppy lifecycle events onto herdr's three semantic states.

herdr models every agent as ``working`` / ``blocked`` / ``idle``. This
reporter is the single writer of that state for the pane, and code-puppy
reports it *authoritatively* -- herdr never has to infer our state from the
screen.

State is a pure function of two facts we own directly:

* **run depth** -- how many agent runs are in flight (root + sub-agents).
  ``> 0`` means the model is doing work.
* **awaiting** -- whether code-puppy is parked on the human. This comes from
  the ``awaiting_user_input`` callback, which fires from the *one*
  process-wide choke-point every interactive wait passes through
  (shell-command approval, file-permission approval, ``ask_user_question``,
  and every menu/picker). Because that single source covers every prompt --
  including shell-command approval, which prompts from inside the tool -- the
  plugin sees every block directly. There is nothing left for herdr to guess.

Effective state::

    blocked   if awaiting              (parked on the human)
    working   elif run_depth > 0       (a run is in flight)
    idle      otherwise                (control is the human's)

Sub-agents fire the same ``agent_run_start`` / ``agent_run_end`` hooks as the
root agent, so we refcount active runs (the same pattern the puppy_spinner
plugin uses) rather than flipping idle when a sub-agent finishes.

Beyond state, the reporter propagates the session namer's auto-generated
conversation title to the pane (presentation title: herdr's pane border and
the sidebar ``pane`` token). The namer names the session asynchronously
after each autosave, so a bounded background wait re-reads the sidecar and
reports the title the moment it lands, without waiting for the next turn.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional, Tuple

from .client import HerdrClient
from . import sources

logger = logging.getLogger(__name__)

WORKING = "working"
BLOCKED = "blocked"
IDLE = "idle"

# Decorative activity strings (the ``message`` field on ``pane.report_agent``).
# State stays authoritative; these are best-effort colour commentary.
THINKING = "thinking"
AWAITING = "awaiting input"

# The namer's model call has its own 60s timeout; the wait for a fresh
# title must cover that plus queueing slack, then give up.
_TITLE_WAIT_INTERVAL_S = 2.0
_TITLE_WAIT_TIMEOUT_S = 90.0


class HerdrReporter:
    """Thread-safe, dedup-ing bridge from callbacks to :class:`HerdrClient`."""

    def __init__(self, client: HerdrClient) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._run_depth = 0
        self._awaiting = False
        self._awaiting_notify = True
        self._last_reported_state: Optional[str] = None
        self._last_reported_message: Optional[str] = None
        # Latest working activity (``thinking`` / ``running <tool>``). Only
        # surfaces while WORKING; BLOCKED and IDLE derive their own message.
        self._activity: Optional[str] = None
        # Durable session reference (name, pickle_path) — NOT the per-run group_id UUID,
        # which changes every turn and can't identify a resumable session.
        self._session_ref: Optional[Tuple[str, str]] = None
        # The title we believe herdr currently shows for our source (herdr
        # replaces the whole per-source metadata entry per report, so this
        # must always match the last envelope we sent).
        self._reported_title: Optional[str] = None
        # Single-flight background wait for the namer's async title write.
        self._title_wait: Optional[threading.Thread] = None
        self._title_wait_session: Optional[str] = None

    @property
    def active(self) -> bool:
        return self._client.active

    # -- state derivation ----------------------------------------------

    def _recompute_locked(self) -> Optional[str]:
        """Return the effective state from the facts we hold. Caller holds lock."""
        if self._awaiting:
            return BLOCKED
        if self._run_depth > 0:
            return WORKING
        return IDLE

    def _message_for_locked(self, state: str) -> Optional[str]:
        """Derive the decorative message for a state. Caller holds lock."""
        if state == BLOCKED:
            return AWAITING
        if state == WORKING:
            return self._activity or THINKING
        return None

    def _sync(self) -> None:
        """Report a changed (state, message) pair, optionally suppressing blocked.

        A genuine state edge rides the critical lane; a message-only change
        (same state, new activity) rides the decorative lane so it can never
        delay an authoritative edge. User-initiated menus retain
        ``notify=False`` for their whole lifetime: their internal ``blocked``
        state must not touch the edge trackers, so closing the menu does not
        re-send an unchanged state.
        """
        with self._lock:
            state = self._recompute_locked()
            if state == BLOCKED and not self._awaiting_notify:
                return
            message = self._message_for_locked(state)
            state_changed = state != self._last_reported_state
            message_changed = message != self._last_reported_message
            if not state_changed and not message_changed:
                return
            self._last_reported_state = state
            self._last_reported_message = message
            session_id = self._session_ref[0] if self._session_ref else None
            critical = state_changed
        self._client.report_state(state, session_id, message=message, critical=critical)

    def _refresh_session(self) -> None:
        """Resolve the durable session reference and report it on change.

        Ignores per-run group_ids entirely. Resolution happens OUTSIDE the
        lock (guardrail). The next prompt after ``/clear``, ``/session new``,
        ``/autosave_load``, ``/load_context``, a quick resume, or an agent
        switch refreshes herdr with no core session callback.
        """
        ref = sources.current_session_ref()
        if ref is None:
            return
        with self._lock:
            changed = ref != self._session_ref
            self._session_ref = ref
        if changed:
            self._client.report_session(ref[0], ref[1])

    # -- lifecycle handlers (all sync; safe from async or worker threads) --

    def on_startup(self) -> None:
        self._sync()  # depth 0, not awaiting -> idle

    def on_user_prompt(self, *_ignored) -> None:
        # Ignore the callback's per-run group_id; resolve the durable session.
        self._refresh_session()
        # The title belongs to the session: a resumed or fresh session shows
        # its own title (or none) without waiting for the next turn.
        self._refresh_title()

    def on_post_autosave(self, *_ignored) -> None:
        """A session was just saved: report a fresh title, or wait for one.

        The namer names the conversation asynchronously after this event, so
        a title typically lands seconds later. When the session is still
        unnamed and the namer is enabled, a bounded background wait reports
        the title as soon as it appears. No-op outside herdr.
        """
        if not self._client.active:
            return
        if sources.current_session_title() is not None:
            self._refresh_title()
            return
        if not sources.naming_enabled():
            return
        self._start_title_wait()

    def on_run_start(self, *_ignored) -> None:
        with self._lock:
            self._run_depth += 1
            # The OUTER run starting is the canonical "thinking" edge. Nested
            # sub-agent runs keep whatever activity is already showing.
            if self._run_depth == 1:
                self._activity = THINKING
        self._sync()

    def on_run_end(self, *_ignored) -> None:
        with self._lock:
            self._run_depth = max(0, self._run_depth - 1)
        # Depth 0 = model stopped. The turn boundary is the canonical idle signal but
        # headless (`-p`) runs never fire it, so depth-0 idle keeps panes honest too.
        self._sync()

    def on_run_cancel(self) -> None:
        with self._lock:
            self._run_depth = 0
            self._awaiting = False
            self._activity = None
        self._sync()

    def on_tool_start(self, tool_name: str) -> None:
        """A tool call started -> decorative ``running <tool>`` activity."""
        # Resolve the message OUTSIDE the lock (guardrail: reporter locks
        # never cover source resolution).
        activity = sources.activity_message(tool_name)
        with self._lock:
            self._activity = activity
        self._sync()

    def on_tool_complete(self) -> None:
        """A tool call finished -> back to ``thinking`` while the run continues."""
        with self._lock:
            self._activity = THINKING
        self._sync()

    def on_turn_end(self) -> None:
        with self._lock:
            self._run_depth = 0
            self._awaiting = False
            self._activity = None
        self._sync()
        # A completed turn is the canonical metadata refresh point: payload computed
        # once, outside the reporter lock, on the decorative lane. Sending every turn
        # also refreshes the TTL; tool callbacks / blocked edges do no token math.
        self._emit_metadata()

    def _emit_metadata(self) -> None:
        """Compute and enqueue pane metadata. Never holds the reporter lock.

        The tokens payload and the session title ride one envelope: herdr
        replaces the whole per-source metadata entry on each report, so a
        tokens report that omits the title would silently wipe it.
        """
        tokens = sources.current_tokens_payload()
        title = sources.current_session_title()
        changed, previous = self._claim_title(title)
        if tokens is None and not changed:
            return
        # Clear only on a set->None transition; when a tokens report is
        # present, its entry replacement clears any stale title for us.
        self._client.report_metadata(
            tokens, title=title, clear_title=changed and title is None
        )
        if changed:
            self._client.set_tab_label(title, expected=previous)

    # -- session title propagation --------------------------------------

    def _claim_title(self, title: Optional[str]) -> Tuple[bool, Optional[str]]:
        """Record ``title`` as the herdr-side value.

        Returns ``(changed, previous)`` -- ``previous`` is the value herdr
        showed before this claim, so a clear can tell the tab-label restore
        what label it should find there. Both title paths (turn-end
        metadata and title-only refresh) go through here, so
        ``_reported_title`` is the single source of truth for what herdr
        currently shows for our source.
        """
        with self._lock:
            previous = self._reported_title
            if title == previous:
                return False, previous
            self._reported_title = title
            return True, previous

    def _refresh_title(self) -> bool:
        """Report the current session's title (or its absence). Title-only.

        Returns True when a report was enqueued. Safe to call whenever:
        deduped by :meth:`_claim_title`.
        """
        title = sources.current_session_title()
        changed, previous = self._claim_title(title)
        if not changed:
            return False
        self._client.report_metadata(None, title=title, clear_title=(title is None))
        self._client.set_tab_label(title, expected=previous)
        return True

    def _start_title_wait(self) -> None:
        """Arm a bounded, session-pinned background wait for a fresh title.

        Single-flight: at most one wait at a time. The wait stops on title
        arrival, session change, shutdown (daemon thread), or timeout.
        """
        session = sources.current_session_name()
        with self._lock:
            wait = self._title_wait
            if wait is not None and wait.is_alive():
                return
            wait = threading.Thread(
                target=self._title_wait_loop, name="herdr-title-wait", daemon=True
            )
            self._title_wait = wait
            self._title_wait_session = session
        wait.start()

    def _title_wait_loop(self) -> None:
        """Re-check the sidecar until the title lands, the session changes,
        or the timeout elapses. Runs on its own daemon thread; every step
        is fail-soft."""
        deadline = time.monotonic() + _TITLE_WAIT_TIMEOUT_S
        pinned = self._title_wait_session
        while time.monotonic() < deadline:
            time.sleep(_TITLE_WAIT_INTERVAL_S)
            if sources.current_session_name() != pinned:
                return  # the new prompt's refresh owns the new session's title
            if self._refresh_title():
                return  # title landed (or its absence was reported)

    def on_awaiting_user_input(self, awaiting: bool, *, notify: bool = True) -> None:
        """Track an interactive wait, notifying only when requested."""
        with self._lock:
            self._awaiting = bool(awaiting)
            self._awaiting_notify = bool(notify)
        self._sync()

    def on_shutdown(self) -> None:
        # If we relabelled the workspace tab, restore its original label
        # first: the client drains the tab job ahead of the release on its
        # worker. A title currently displayed means the tab may be ours;
        # no title ever shown means no tab job is needed at all.
        with self._lock:
            expected = self._reported_title
        if expected is not None:
            self._client.set_tab_label(None, expected=expected)
        # Release pane authority directly (no intermediate idle report).
        # release_and_close() is idempotent and bounded, so calling it from both
        # session_end and shutdown can never delay process exit.
        self._client.release_and_close()


__all__ = ["HerdrReporter", "WORKING", "BLOCKED", "IDLE", "THINKING", "AWAITING"]
