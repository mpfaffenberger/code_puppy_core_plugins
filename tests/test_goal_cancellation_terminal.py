"""Real POSIX terminal input must cancel judges without losing the next prompt."""

from __future__ import annotations

import os
import sys
import textwrap

import pytest

pexpect = pytest.importorskip("pexpect")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pseudo-terminal test")
@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("cancel_key", ["ctrl+c", "ctrl+k"])
def test_terminal_cancel_during_goal_judging(tmp_path, persistent, cancel_key):
    program = textwrap.dedent(
        """
        import asyncio
        import signal
        from types import SimpleNamespace
        from code_puppy.interactive_support import _interactive_sigint_guard
        from code_puppy.config import set_value
        from code_puppy.messaging import run_ui
        from code_puppy.messaging.pause_controller import get_pause_controller
        from code_puppy_core_plugins.wiggum import goal_runs, register_callbacks, state
        from code_puppy_core_plugins.wiggum.judge_config import JudgeConfig

        async def main():
            set_value("cancel_agent_key", CANCEL_KEY)
            signal.signal(signal.SIGINT, _interactive_sigint_guard)
            if PERSISTENT:
                assert run_ui.start_persistent_ui()
            run = goal_runs.create("terminal cancellation test")
            state.start(run.prompt, mode="goal", run_id=run.run_id)
            started = set()
            cancelled = set()

            async def judge(*, judge_config, **kwargs):
                started.add(judge_config.name)
                try:
                    if len(started) == 2:
                        await asyncio.sleep(0.15)
                        print("JUDGES_READY", flush=True)
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.add(judge_config.name)
                    raise

            register_callbacks.judge_goal = judge
            register_callbacks._resolve_judges = lambda _: [
                JudgeConfig(name="alpha", model="test"),
                JudgeConfig(name="beta", model="test"),
            ]
            register_callbacks._display_llm_judge = lambda *a, **k: None
            pc = get_pause_controller()

            async def confirm_steers():
                while pc.pending_steer_counts() != (1, 1):
                    await asyncio.sleep(0.01)
                print("STEERS_READY", flush=True)

            monitor = asyncio.create_task(confirm_steers())
            try:
                result = await register_callbacks._on_interactive_turn_end(
                    SimpleNamespace(get_message_history=lambda: []), run.prompt
                )
                assert result is None
                assert cancelled == {"alpha", "beta"}, cancelled
                assert not state.is_active()
                assert not run_ui.is_run_active()
                assert goal_runs.load(run.run_id).status == "interrupted"
                assert pc.pending_steer_counts() == (0, 0)
                assert not pc.is_paused()
                print("PROMPT_RESTORED", flush=True)
                if PERSISTENT:
                    assert await run_ui.wait_for_idle_submission() == "next turn"
                print("TERMINAL_OK", flush=True)
            finally:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
                run_ui.stop_persistent_ui()

        asyncio.run(main())
        """
    )
    program = f"PERSISTENT = {persistent!r}\nCANCEL_KEY = {cancel_key!r}\n" + program
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("HERDR_")
    }
    environment.update(
        HOME=str(tmp_path),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_STATE_HOME=str(tmp_path / "state"),
        XDG_DATA_HOME=str(tmp_path / "data"),
        PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring",
        TERM="xterm-256color",
    )
    child = pexpect.spawn(
        sys.executable, ["-c", program], env=environment, encoding="utf-8", timeout=20
    )
    try:
        child.expect_exact("JUDGES_READY", timeout=60)
        child.send("queued input\r")
        child.send("now input\x1b[13;5u")
        child.expect_exact("STEERS_READY")
        child.send("\x03" if cancel_key == "ctrl+c" else "\x0b")
        child.expect_exact("PROMPT_RESTORED")
        if persistent:
            child.send("next turn\r")
        child.expect_exact("TERMINAL_OK")
        child.expect(pexpect.EOF)
        child.close()
        assert child.exitstatus == 0, child.before
    finally:
        if child.isalive():
            child.terminate(force=True)
