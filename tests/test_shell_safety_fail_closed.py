"""shell_safety denies when it cannot complete its assessment.

`shell_safety_callback` reads three config values before its own `try`. The
dispatcher reports an exception from there as `None`, and `run_shell_command`
reads `None` as "no objection" — so before `fail_closed=True` an error in those
reads let the command run with no safety assessment at all.

The trigger is not hypothetical. `ConfigParser` interpolates lazily, so a value
like `yolo_mode=%` parses cleanly — the corruption quarantine never sees it —
and raises only when the option is read.
"""

from unittest.mock import patch

import pytest

from code_puppy import callbacks
from code_puppy import config as cp_config
from code_puppy_core_plugins.shell_safety import register_callbacks as shell_safety


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


def test_the_registration_opts_in():
    shell_safety.register()

    assert (
        "run_shell_command",
        shell_safety.shell_safety_callback,
    ) in callbacks._fail_closed_callbacks


def test_the_config_really_does_raise(broken_interpolation_config):
    """Guard the premise: if config reads ever stop raising, this suite lies."""
    with pytest.raises(Exception) as excinfo:
        cp_config.get_yolo_mode()

    assert "Interpolation" in type(excinfo.value).__name__


async def test_a_failed_assessment_denies(broken_interpolation_config):
    shell_safety.register()

    results = await callbacks.on_run_shell_command(None, "echo hi", None, 60)

    assert any(
        isinstance(result, dict) and result.get("blocked") is True for result in results
    ), "an assessment that could not run must not read as approval"


async def test_without_the_opt_in_the_same_failure_reads_as_approval(
    broken_interpolation_config,
):
    """Characterizes the behavior this flag exists to change."""
    callbacks.register_callback("run_shell_command", shell_safety.shell_safety_callback)

    results = await callbacks.on_run_shell_command(None, "echo hi", None, 60)

    assert results == [None]


async def test_the_command_never_reaches_the_shell(
    broken_interpolation_config, tmp_path
):
    """The observable end: no subprocess, and the refusal is attributable.

    An absent side effect alone would also hold if the call had failed for an
    unrelated reason, so the error is pinned to the guard by name.
    """
    from code_puppy.tools.command_runner import run_shell_command

    shell_safety.register()
    sentinel = tmp_path / "executed"

    with (
        patch("code_puppy.tools.command_runner.emit_info"),
        patch("code_puppy.tools.command_runner.emit_error"),
    ):
        result = await run_shell_command(None, f"touch {sentinel}", None, 60)

    assert not sentinel.exists(), "the shell ran despite the assessment failing"
    assert result.success is False
    assert "shell_safety_callback" in (result.error or ""), (
        f"refusal not attributable to the guard: {result.error!r}"
    )


async def test_a_healthy_assessment_is_unaffected(monkeypatch, tmp_path):
    """The opt-in must not disturb the normal path."""
    cfg = tmp_path / "puppy.cfg"
    cfg.write_text("[puppy]\nyolo_mode=false\n")
    monkeypatch.setattr(cp_config, "CONFIG_FILE", str(cfg))
    shell_safety.register()

    # yolo_mode off means the user reviews manually; the guard abstains.
    results = await callbacks.on_run_shell_command(None, "echo hi", None, 60)

    assert results == [None]
