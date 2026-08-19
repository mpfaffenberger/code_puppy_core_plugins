"""The blocked-command hint must be actionable.

The safety callback only ever runs while ``yolo_mode`` is already **true**
(it returns ``None`` otherwise), so an override hint of ``/set yolo_mode
true`` is a guaranteed no-op. Every blocking message must instead point at
the one setting that actually changes behavior.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from code_puppy import callbacks
from code_puppy import config as cp_config
from code_puppy_core_plugins.shell_safety import register_callbacks as shell_safety


@pytest.fixture(autouse=True)
def _clean_shell_callback():
    callbacks.clear_callbacks("run_shell_command")
    yield
    callbacks.clear_callbacks("run_shell_command")


def _set_config(monkeypatch, tmp_path, raw: str):
    cfg = tmp_path / "puppy.cfg"
    cfg.write_text(raw)
    monkeypatch.setattr(cp_config, "CONFIG_FILE", str(cfg))


async def test_blocked_message_never_suggests_yolo_mode_true(monkeypatch, tmp_path):
    _set_config(
        monkeypatch, tmp_path, "[puppy]\nyolo_mode=true\nsafety_permission_level=low\n"
    )
    from code_puppy_core_plugins.shell_safety.command_cache import cache_assessment

    cache_assessment("rm -rf /tmp/x", None, "high", "destructive")
    shell_safety.register()

    with patch("code_puppy.messaging.emit_info"):
        result = (
            await callbacks.on_run_shell_command(None, "rm -rf /tmp/x", None, 60)
        )[0]

    assert result is not None and result.get("blocked") is True
    msg = result["error_message"]
    assert "/set yolo_mode true" not in msg
    assert "/set safety_permission_level high" in msg


async def test_fault_message_never_suggests_raising_permission(monkeypatch, tmp_path):
    _set_config(
        monkeypatch, tmp_path, "[puppy]\nyolo_mode=true\nsafety_permission_level=low\n"
    )
    shell_safety.register()

    with (
        patch(
            "code_puppy_core_plugins.shell_safety.register_callbacks"
            ".get_cached_assessment",
            return_value=None,
        ),
        patch(
            "code_puppy_core_plugins.shell_safety.agent_shell_safety.ShellSafetyAgent",
            side_effect=RuntimeError("LLM unreachable"),
        ),
        patch("code_puppy.messaging.emit_info"),
    ):
        result = (await callbacks.on_run_shell_command(None, "echo hi", None, 60))[0]

    assert result is not None and result.get("blocked") is True
    msg = result["error_message"]
    assert "/set yolo_mode true" not in msg
    assert "/set safety_permission_level high" not in msg
    assert "fail-closed" in msg
