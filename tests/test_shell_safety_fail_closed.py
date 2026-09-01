"""shell_safety denies when it cannot complete its assessment.

Previously, configuration reads happened before the callback's fail-safe handler.
The dispatcher treated those exceptions as approval, so a command could run
without a safety assessment. These tests cover the regression and compatibility
with callback APIs before and after ``fail_closed`` was added.

The trigger is not hypothetical. ``ConfigParser`` interpolates lazily, so a
value like ``yolo_mode=%`` parses cleanly and raises only when the option is
read.
"""

import os
import shlex
import subprocess
import sys
from unittest.mock import Mock

import pytest

from code_puppy import callbacks
from code_puppy import config as cp_config
from code_puppy_core_plugins.shell_safety import register_callbacks as shell_safety
from code_puppy_core_plugins.shell_safety.command_cache import CachedAssessment


def _shell_quote(value):
    if os.name == "nt":
        return subprocess.list2cmdline([value])
    return shlex.quote(value)


def _sentinel_command(tmp_path):
    sentinel = tmp_path / "executed"
    script = tmp_path / "create_sentinel.py"
    script.write_text(
        "import sys\nfrom pathlib import Path\nPath(sys.argv[1]).touch()\n"
    )
    command = " ".join(
        _shell_quote(value) for value in (sys.executable, script.name, sentinel.name)
    )
    return command, sentinel, str(tmp_path)


@pytest.fixture
def broken_interpolation_config(monkeypatch, tmp_path):
    """Point config at a file that parses but raises when read."""
    cfg = tmp_path / "puppy.cfg"
    cfg.write_text("[puppy]\nyolo_mode=%\n")
    monkeypatch.setattr(cp_config, "CONFIG_FILE", str(cfg))
    return cfg


@pytest.fixture(autouse=True)
def _clean_shell_phase():
    callbacks.clear_callbacks("run_shell_command")
    yield
    callbacks.clear_callbacks("run_shell_command")


def test_registration_enables_fail_closed_on_supported_callback_api(monkeypatch):
    registrations = []

    def modern_register(phase, callback, *, fail_closed):
        registrations.append((phase, callback, fail_closed))

    monkeypatch.setattr(shell_safety, "register_callback", modern_register)
    shell_safety.register()

    assert registrations == [
        ("run_shell_command", shell_safety.shell_safety_callback, True)
    ]


def test_registration_supports_legacy_callback_api(monkeypatch):
    registrations = []

    def legacy_register(phase, callback):
        registrations.append((phase, callback))

    monkeypatch.setattr(shell_safety, "register_callback", legacy_register)
    shell_safety.register()

    assert registrations == [("run_shell_command", shell_safety.shell_safety_callback)]


def test_registration_treats_kwargs_adapter_as_legacy(monkeypatch):
    registrations = []

    def kwargs_adapter(phase, callback, **kwargs):
        registrations.append((phase, callback, kwargs))

    monkeypatch.setattr(shell_safety, "register_callback", kwargs_adapter)
    shell_safety.register()

    assert registrations == [
        ("run_shell_command", shell_safety.shell_safety_callback, {})
    ]


def test_registration_rethrows_internal_type_error(monkeypatch):
    registration_calls = []

    def broken_register(phase, callback, *, fail_closed):
        registration_calls.append((phase, callback))
        raise TypeError("internal fail_closed registry failure")

    monkeypatch.setattr(shell_safety, "register_callback", broken_register)

    with pytest.raises(TypeError, match="internal fail_closed registry failure"):
        shell_safety.register()

    assert registration_calls == [
        ("run_shell_command", shell_safety.shell_safety_callback)
    ]


async def test_legacy_registration_callback_still_fails_closed(monkeypatch):
    captured = {}

    def legacy_register(phase, callback):
        captured[phase] = callback

    monkeypatch.setattr(shell_safety, "register_callback", legacy_register)
    monkeypatch.setattr(
        shell_safety,
        "get_yolo_mode",
        Mock(side_effect=RuntimeError("config unavailable")),
    )
    monkeypatch.setattr(
        shell_safety.logger,
        "warning",
        Mock(side_effect=RuntimeError("logging unavailable")),
    )
    shell_safety.register()

    result = await captured["run_shell_command"](None, "echo hi", None, 60)

    assert result["blocked"] is True
    assert "config unavailable" not in result["error_message"]
    assert result["reasoning"] == "Safety assessment was unavailable."


async def test_failure_log_omits_exception_details(monkeypatch):
    warning = Mock()
    monkeypatch.setattr(
        shell_safety,
        "get_yolo_mode",
        Mock(side_effect=RuntimeError("sensitive config details")),
    )
    monkeypatch.setattr(shell_safety.logger, "warning", warning)

    result = await shell_safety.shell_safety_callback(None, "echo hi", None, 60)

    assert result["blocked"] is True
    assert warning.call_args.args == (
        "Shell-safety assessment unavailable (%s)",
        "RuntimeError",
    )
    assert "sensitive config details" not in str(warning.call_args)
    assert "YOLO" not in result["error_message"]


async def test_a_failed_assessment_denies(broken_interpolation_config):
    shell_safety.register()

    results = await callbacks.on_run_shell_command(None, "echo hi", None, 60)

    blocked = next(
        result
        for result in results
        if isinstance(result, dict) and result.get("blocked")
    )
    assert blocked["error_message"]
    assert "Interpolation" not in blocked["error_message"]


async def test_the_command_never_reaches_the_shell(
    broken_interpolation_config, tmp_path
):
    """The refusal must happen before the subprocess executor is called."""
    from code_puppy.tools.command_runner import run_shell_command

    shell_safety.register()
    command, sentinel, cwd = _sentinel_command(tmp_path)

    result = await run_shell_command(None, command, cwd, 60)

    assert not sentinel.exists(), "the shell ran despite the assessment failing"
    assert result.success is False
    assert result.error


@pytest.mark.parametrize(
    "failing_getter", ["get_global_model_name", "get_safety_permission_level"]
)
async def test_model_or_permission_failure_never_reaches_shell(
    monkeypatch, tmp_path, failing_getter
):
    """Failures in each pre-assessment lookup must block command execution."""
    from code_puppy.tools.command_runner import run_shell_command

    monkeypatch.setattr(shell_safety, "get_yolo_mode", lambda: True)
    monkeypatch.setattr(shell_safety, "get_global_model_name", lambda: "test-model")
    monkeypatch.setattr(
        shell_safety,
        failing_getter,
        Mock(side_effect=RuntimeError("lookup unavailable")),
    )
    shell_safety.register()
    command, sentinel, cwd = _sentinel_command(tmp_path)

    result = await run_shell_command(None, command, cwd, 60)

    assert not sentinel.exists(), "the shell ran despite the lookup failing"
    assert result.success is False
    assert "lookup unavailable" not in (result.error or "")


@pytest.mark.parametrize(
    "model_name",
    ["claude-code-test", "codex-test", "chatgpt-test", "gemini-oauth-test"],
)
async def test_oauth_models_skip_shell_assessment(monkeypatch, model_name):
    monkeypatch.setattr(shell_safety, "get_yolo_mode", lambda: True)
    monkeypatch.setattr(shell_safety, "get_global_model_name", lambda: model_name)
    assessment_lookup = Mock(side_effect=AssertionError("OAuth model was assessed"))
    monkeypatch.setattr(shell_safety, "get_cached_assessment", assessment_lookup)

    result = await shell_safety.shell_safety_callback(None, "echo hi", None, 60)

    assert result is None
    assessment_lookup.assert_not_called()


async def test_yolo_mode_allows_cached_low_risk_assessment(monkeypatch):
    monkeypatch.setattr(shell_safety, "get_yolo_mode", lambda: True)
    monkeypatch.setattr(shell_safety, "get_global_model_name", lambda: "test-model")
    monkeypatch.setattr(shell_safety, "get_safety_permission_level", lambda: "medium")
    monkeypatch.setattr(
        shell_safety,
        "get_cached_assessment",
        lambda command, cwd: CachedAssessment("low", "safe"),
    )

    result = await shell_safety.shell_safety_callback(None, "echo hi", None, 60)

    assert result is None


async def test_block_message_only_names_permission_level_override(monkeypatch):
    monkeypatch.setattr(shell_safety, "get_yolo_mode", lambda: True)
    monkeypatch.setattr(shell_safety, "get_global_model_name", lambda: "test-model")
    monkeypatch.setattr(shell_safety, "get_safety_permission_level", lambda: "medium")
    monkeypatch.setattr(
        shell_safety,
        "get_cached_assessment",
        lambda command, cwd: CachedAssessment("high", "dangerous"),
    )

    result = await shell_safety.shell_safety_callback(None, "rm important", None, 60)

    assert result["blocked"] is True
    assert "Override: /set safety_permission_level high" in result["error_message"]
    assert "yolo" not in result["error_message"].lower()


async def test_manual_mode_does_not_require_model_lookup(monkeypatch):
    monkeypatch.setattr(shell_safety, "get_yolo_mode", lambda: False)
    model_lookup = Mock(side_effect=AssertionError("manual mode looked up a model"))
    monkeypatch.setattr(shell_safety, "get_global_model_name", model_lookup)
    shell_safety.register()

    results = await callbacks.on_run_shell_command(None, "echo hi", None, 60)

    assert results == [None]
    model_lookup.assert_not_called()


@pytest.mark.parametrize(
    "command",
    [
        "git worktree add ../new-tree -b topic main && git cherry-pick abc123",
        "git worktree add ../new-tree; git reset --hard HEAD",
        "git worktree add ../new-tree\ngit commit -m followup",
    ],
)
async def test_chained_worktree_mutation_is_blocked_before_mode_checks(
    monkeypatch, command
):
    mode_lookup = Mock(side_effect=AssertionError("guard consulted yolo mode"))
    monkeypatch.setattr(shell_safety, "get_yolo_mode", mode_lookup)

    result = await shell_safety.shell_safety_callback(None, command, None, 60)

    assert result["blocked"] is True
    assert result["risk"] == "medium"
    assert "original working directory" in result["reasoning"]
    assert "cwd" in result["error_message"]
    mode_lookup.assert_not_called()


@pytest.mark.parametrize(
    "command",
    [
        "git worktree add ../new-tree -b topic main",
        "git worktree add ../new-tree && git status",
        "git worktree add ../new-tree && cd ../new-tree && git cherry-pick abc123",
        "git worktree add ../new-tree && git -C ../new-tree cherry-pick abc123",
        'echo "git worktree add ../new-tree && git cherry-pick abc123"',
    ],
)
async def test_worktree_guard_allows_explicit_or_read_only_followups(
    monkeypatch, command
):
    monkeypatch.setattr(shell_safety, "get_yolo_mode", lambda: False)

    result = await shell_safety.shell_safety_callback(None, command, None, 60)

    assert result is None
