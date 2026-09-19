"""Essential menu instructions must remain legible on dark theme palettes."""

import pytest

from code_puppy_core_plugins.termflow_tui import _sgr_for, fragments_to_lines
from code_puppy_core_plugins.steer_queue.queue_menu import QueueMenuApp
from unittest.mock import Mock


@pytest.mark.parametrize(
    "role", ["body", "input", "muted", "help", "help_text", "title"]
)
def test_menu_text_uses_theme_foreground_without_dim(monkeypatch, role):
    monkeypatch.setattr("code_puppy.callbacks.on_prompt_text_color", lambda: "#eadcce")
    prefix, suffix = _sgr_for("class:tui." + role)
    assert "234;220;206" in prefix
    assert "\x1b[2m" not in prefix
    assert suffix == "\x1b[0m"


def test_menu_foreground_fallback(monkeypatch):
    monkeypatch.setattr("code_puppy.callbacks.on_prompt_text_color", lambda: None)
    assert _sgr_for("class:tui.body")[0] == "\x1b[39m"


def test_empty_queue_and_controls_are_not_dim(monkeypatch):
    monkeypatch.setattr("code_puppy.callbacks.on_prompt_text_color", lambda: "#eadcce")
    controller = Mock()
    controller.peek_pending_steer_queued.return_value = []
    app = QueueMenuApp(controller)
    output = "\n".join(fragments_to_lines(app._render_list() + app._render_footer()))
    assert "Queue is empty" in output
    assert "add a prompt" in output
    assert "\x1b[2m" not in output
