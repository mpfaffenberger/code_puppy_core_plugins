"""Shared fakes for the herdr plugin tests.

``FakeClient`` records report calls instead of touching a socket. It is
shared by ``test_herdr_plugin.py`` (state machine + core wiring) and
``test_herdr_titles.py`` (title / tab-label propagation) so both files
can stay well under the 600-line cap.
"""

from __future__ import annotations


class FakeClient:
    """Records report calls instead of touching a socket."""

    def __init__(self, active: bool = True) -> None:
        self._active = active
        self.states: list[tuple[str, str | None]] = []
        self.activity: list[tuple[str, str | None, bool]] = []
        self.sessions: list[tuple[str, str]] = []
        self.metadata: list[dict] = []
        self.tab_labels: list[tuple[str | None, str | None]] = []
        self.closed = False

    def report_state(
        self, state, agent_session_id=None, *, message=None, critical=True
    ):
        self.states.append((state, agent_session_id))
        self.activity.append((state, message, critical))

    def report_session(self, agent_session_id, session_path=None):
        self.sessions.append((agent_session_id, session_path))

    def report_metadata(self, tokens=None, *, title=None, clear_title=False):
        self.metadata.append((tokens, title, clear_title))

    def set_tab_label(self, label=None, *, expected=None):
        self.tab_labels.append((label, expected))

    @property
    def active(self):
        return self._active

    def release_and_close(self, timeout_s=1.0):
        self.closed = True

    def close(self):
        self.closed = True
