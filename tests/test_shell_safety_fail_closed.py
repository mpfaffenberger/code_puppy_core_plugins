"""shell_safety denies when it cannot complete its assessment.

`shell_safety_callback` reads three config values before its own `try`. An
exception from any of them escapes the callback, the dispatcher reports it as
`None`, and `run_shell_command` reads `None` as "no objection".

Only one of those three reads actually produces an unassessed command, and
these tests are built around that one rather than the more obvious-looking
cases:

* `yolo_mode` and `model` are also read elsewhere on the execution path, so
  corrupting them stops the command for unrelated reasons. The guard's failure
  is masked there, not survivable.
* `safety_permission_level` is read *only* by this guard. Corrupting it leaves
  the rest of the system healthy while the assessment dies, and the command
  runs with no safety check at all. That is the case worth pinning.

The trigger is a value that parses but cannot be read: `ConfigParser`
interpolates lazily, so `%(nope)s` survives `read_string` — never reaching the
corruption quarantine — and raises at `get()` time.
"""

from unittest.mock import patch

import pytest

from code_puppy import callbacks
from code_puppy import config as cp_config
from code_puppy_core_plugins.shell_safety import register_callbacks as shell_safety

# Real enough to get past the OAuth short-circuit, so the guard reaches the
# read under test instead of abstaining earlier.
_MODEL = "gpt-4o"

_HEALTHY = f"[puppy]\nyolo_mode=true\nmodel={_MODEL}\nsafety_permission_level=medium\n"
_UNREADABLE_THRESHOLD = (
    f"[puppy]\nyolo_mode=true\nmodel={_MODEL}\nsafety_permission_level=%(nope)s\n"
)


@pytest.fixture(autouse=True)
def _clean_shell_phase():
    callbacks.clear_callbacks("run_shell_command")
    yield
    callbacks.clear_callbacks("run_shell_command")


def _point_config_at(monkeypatch, tmp_path, contents: str):
    cfg = tmp_path / "puppy.cfg"
    cfg.write_text(contents)
    monkeypatch.setattr(cp_config, "CONFIG_FILE", str(cfg))
    return cfg


async def _touch_via_shell(sentinel):
    from code_puppy.tools.command_runner import run_shell_command

    with (
        patch("code_puppy.tools.command_runner.emit_info"),
        patch("code_puppy.tools.command_runner.emit_error"),
    ):
        return await run_shell_command(None, f"touch {sentinel}", None, 60)


def test_the_registration_opts_in():
    shell_safety.register()

    assert (
        "run_shell_command",
        shell_safety.shell_safety_callback,
    ) in callbacks._fail_closed_callbacks


def test_the_premise_holds(monkeypatch, tmp_path):
    """If this read ever stops raising, every test below silently proves nothing."""
    _point_config_at(monkeypatch, tmp_path, _UNREADABLE_THRESHOLD)

    with pytest.raises(Exception) as excinfo:
        cp_config.get_safety_permission_level()

    assert "Interpolation" in type(excinfo.value).__name__


async def test_the_harness_can_observe_execution(monkeypatch, tmp_path):
    """Positive control.

    Without this, an absent sentinel below would be indistinguishable from a
    shell that simply never works in this environment.
    """
    _point_config_at(monkeypatch, tmp_path, _HEALTHY)
    sentinel = tmp_path / "control"

    def approves(*_args, **_kwargs):
        return None

    callbacks.register_callback("run_shell_command", approves)
    result = await _touch_via_shell(sentinel)

    assert sentinel.exists(), "the harness cannot observe command execution at all"
    assert result.success is True


async def test_without_the_opt_in_a_failed_assessment_lets_the_command_run(
    monkeypatch, tmp_path
):
    """Characterizes the behavior this change exists to remove."""
    _point_config_at(monkeypatch, tmp_path, _UNREADABLE_THRESHOLD)
    sentinel = tmp_path / "executed"
    callbacks.register_callback("run_shell_command", shell_safety.shell_safety_callback)

    await _touch_via_shell(sentinel)

    assert sentinel.exists(), (
        "expected the pre-change behavior: the assessment failed and the "
        "command ran anyway"
    )


async def test_with_the_opt_in_the_command_never_reaches_the_shell(
    monkeypatch, tmp_path
):
    """The point of the change, against the same config as the test above."""
    _point_config_at(monkeypatch, tmp_path, _UNREADABLE_THRESHOLD)
    sentinel = tmp_path / "executed"
    shell_safety.register()

    result = await _touch_via_shell(sentinel)

    assert not sentinel.exists(), "the shell ran despite the assessment failing"
    assert result.success is False
    assert "shell_safety_callback" in (result.error or ""), (
        f"refusal not attributable to the guard: {result.error!r}"
    )


async def test_a_healthy_assessment_is_unaffected(monkeypatch, tmp_path):
    """The opt-in must not disturb the normal path."""
    _point_config_at(
        monkeypatch, tmp_path, f"[puppy]\nyolo_mode=false\nmodel={_MODEL}\n"
    )
    shell_safety.register()

    # yolo_mode off means the user reviews every command; the guard abstains.
    results = await callbacks.on_run_shell_command(None, "echo hi", None, 60)

    assert results == [None]


async def test_an_escaping_exception_becomes_a_structured_refusal(
    monkeypatch, tmp_path
):
    """`yolo_mode` is read elsewhere too, so corrupting it does not produce an
    unassessed command. It does escape as a raw exception, though, and the
    opt-in turns that into a refusal the caller can render."""
    _point_config_at(monkeypatch, tmp_path, "[puppy]\nyolo_mode=%\n")
    shell_safety.register()

    results = await callbacks.on_run_shell_command(None, "echo hi", None, 60)

    assert results[0]["blocked"] is True


async def test_safety_off_cannot_be_blocked_by_a_config_error(monkeypatch, tmp_path):
    """With yolo_mode off the guard abstains before reading anything else.

    `model` used to be read before the yolo short-circuit, so a value this
    callback never actually needed could refuse a command for a user who had
    turned automatic assessment off entirely.
    """
    _point_config_at(
        monkeypatch, tmp_path, "[puppy]\nyolo_mode=false\nmodel=%(nope)s\n"
    )
    sentinel = tmp_path / "executed"
    shell_safety.register()

    result = await _touch_via_shell(sentinel)

    assert sentinel.exists(), "a guard that abstains must not be able to refuse"
    assert result.success is True


def test_the_core_floor_is_declared():
    """The opt-in needs a core new enough to accept it.

    Without the floor, `pip install -U` on the bundle against an older core
    raises TypeError at registration; the loader logs and skips, leaving no
    shell guard at all.
    """
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    deps = tomllib.loads((root / "pyproject.toml").read_text())["project"][
        "dependencies"
    ]

    assert any(d.startswith("code-puppy>=") for d in deps), deps
