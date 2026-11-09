"""Exercise the real cancel routes while the implementor is no longer running."""

from __future__ import annotations

import asyncio
import gc
import signal
import threading
import weakref
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from code_puppy import keymap
from code_puppy.agents import _key_listeners, _run_signals
from code_puppy.messaging import run_ui
from code_puppy.messaging.pause_controller import PauseController
from code_puppy.tools import command_runner
from code_puppy_core_plugins.wiggum import (
    cancellation,
    goal_runs,
    register_callbacks,
    state,
)
from code_puppy_core_plugins.wiggum.judge import GoalJudgement
from code_puppy_core_plugins.wiggum.judge_config import JudgeConfig


@pytest.fixture
def judge_phase(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "code_puppy.messaging.pause_controller._pause_controller", PauseController()
    )
    monkeypatch.setattr(goal_runs, "_RUNS_DIR", tmp_path)
    run = goal_runs.create("finish the work")
    state.start(run.prompt, mode="goal", run_id=run.run_id)
    monkeypatch.setattr(register_callbacks, "_display_llm_judge", Mock())
    monkeypatch.setattr(register_callbacks, "emit_info", Mock())
    monkeypatch.setattr(
        register_callbacks,
        "_resolve_judges",
        lambda _: [
            JudgeConfig(name="alpha", model="test"),
            JudgeConfig(name="beta", model="test"),
        ],
    )
    monkeypatch.setattr(run_ui, "_persistent", True)
    monkeypatch.setattr(run_ui, "_run_active", False)
    monkeypatch.setattr(run_ui, "_editor", None)
    monkeypatch.setattr(run_ui, "_ensure_persistent_listener_locked", lambda: None)
    monkeypatch.setattr(run_ui, "_clear_status_row", lambda: None)
    monkeypatch.setattr(run_ui, "_defer_undelivered_steers", lambda: None)
    monkeypatch.setattr(run_ui, "get_bottom_bar", Mock(return_value=Mock()))
    monkeypatch.setattr(_key_listeners, "_cancel_handler", None)
    monkeypatch.setattr(command_runner, "_AGENT_CANCEL_CB", None)
    monkeypatch.setattr(_run_signals, "_detach_event", None)
    monkeypatch.setattr(command_runner, "_SIGINT_CANCEL_REQUESTED", False)
    monkeypatch.setattr(command_runner, "_RUNNING_PROCESSES", {})
    monkeypatch.setattr(command_runner, "_tear_down_live_panels", Mock())
    monkeypatch.setattr(command_runner, "kill_all_running_shell_processes", Mock())
    monkeypatch.setattr(command_runner, "emit_warning", Mock())
    handle = SimpleNamespace(thread=Mock(), stop_event=threading.Event(), stop=Mock())
    handle.thread.is_alive.return_value = True
    monkeypatch.setattr(_key_listeners, "_active_handle", handle)
    previous_signal = signal.getsignal(signal.SIGINT)
    restore_signal = signal.signal
    yield SimpleNamespace(run=run, handle=handle)
    restore_signal(signal.SIGINT, previous_signal)
    state.stop()


@pytest.mark.parametrize("gesture", ["raw", "remapped", "sigint", "shell", "external"])
async def test_ctrl_c_stops_all_judges_and_preserves_resume(
    judge_phase, monkeypatch, gesture
):
    started: set[str] = set()
    cancelled: set[str] = set()
    ready = asyncio.Event()
    release = asyncio.Event()
    composing = gesture in {"raw", "sigint"}

    def absorb_composing() -> bool:
        nonlocal composing
        absorbed = composing
        composing = False
        return absorbed

    monkeypatch.setattr(run_ui, "absorb_ctrl_c_if_composing", absorb_composing)
    if gesture == "sigint":
        monkeypatch.setattr(cancellation, "sigint_fallback_cancels", lambda: True)
    monkeypatch.setattr(
        keymap,
        "get_cancel_agent_key",
        lambda: "ctrl+k" if gesture == "remapped" else "ctrl+c",
    )

    async def blocked_judge(*, judge_config, **kwargs):
        started.add(judge_config.name)
        if len(started) == 2:
            ready.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.add(judge_config.name)
            raise
        return GoalJudgement(
            judge_name=judge_config.name, complete=True, notes="", raw_response=""
        )

    monkeypatch.setattr(register_callbacks, "judge_goal", blocked_judge)
    task = asyncio.create_task(
        register_callbacks._on_interactive_turn_end(
            SimpleNamespace(get_message_history=lambda: []), judge_phase.run.prompt
        )
    )
    previous_signal = signal.getsignal(signal.SIGINT)

    def cancel() -> None:
        if gesture == "external":
            task.cancel()
        elif gesture == "sigint":
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        elif gesture == "shell":
            command_runner._shell_sigint_handler(signal.SIGINT, None)
        else:
            character = "\x0b" if gesture == "remapped" else "\x03"
            _key_listeners._dispatch_key(character, lambda: None, character, None)

    try:
        await asyncio.wait_for(ready.wait(), 2)
        assert run_ui.is_run_active(), "Judges must not leave the REPL in idle mode"
        assert _key_listeners._resolve_cancel_handler(None) is not None
        assert command_runner._AGENT_CANCEL_CB is not None
        if composing:
            cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert not cancelled
        cancel()
        assert await asyncio.wait_for(task, 2) is None
        assert cancelled == {"alpha", "beta"}
        assert not state.is_active()
        saved = goal_runs.load(judge_phase.run.run_id)
        assert saved.status == "interrupted"
        assert saved.loop_count == 1
        assert not run_ui.is_run_active()
        assert _key_listeners._resolve_cancel_handler(None) is None
        assert command_runner._AGENT_CANCEL_CB is None
        assert signal.getsignal(signal.SIGINT) is previous_signal
        judge_phase.handle.stop.assert_not_called()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("late_outcome", ["success", "error", "cancel"])
@pytest.mark.parametrize("external_cancel", [False, True])
async def test_escalation_detaches_stubborn_judge_without_touching_next_run(
    judge_phase, monkeypatch, late_outcome, external_cancel
):
    from code_puppy.messaging.pause_controller import get_pause_controller

    ready = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    next_ready = asyncio.Event()
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(
        _run_signals, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    loop = asyncio.get_running_loop()
    unobserved = Mock()
    monkeypatch.setattr(loop, "call_exception_handler", unobserved)
    late_task = None

    async def stubborn_judges(**kwargs):
        nonlocal late_task
        late_task = weakref.ref(asyncio.current_task())
        pc = get_pause_controller()
        pc.pause()
        pc.request_steer("discard on detach", mode="now", history_recorded=True)
        ready.set()
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
            if late_outcome == "error":
                raise RuntimeError("late judge failure")
            if late_outcome == "cancel":
                raise asyncio.CancelledError
            return True, "", []
        finally:
            finished.set()

    monkeypatch.setattr(register_callbacks, "_run_goal_judges", stubborn_judges)
    task = asyncio.create_task(
        register_callbacks._on_interactive_turn_end(
            SimpleNamespace(), judge_phase.run.prompt
        )
    )
    next_task = None
    baseline = asyncio.all_tasks()
    try:
        await asyncio.wait_for(ready.wait(), 2)
        cancel = _key_listeners._resolve_cancel_handler(None)
        if external_cancel:
            task.cancel()
        cancel()
        await asyncio.wait_for(cancelled.wait(), 2)
        assert not task.done()
        clock.now += _run_signals.CANCEL_ESCALATE_AFTER_S
        cancel()
        done, _ = await asyncio.wait({task}, timeout=0.2)
        assert task in done, "Escalated cancellation must return before judges finish"
        assert task.result() is None
        assert goal_runs.load(judge_phase.run.run_id).status == "interrupted"
        assert not state.is_active()
        assert not run_ui.is_run_active()
        assert _run_signals._detach_event is None
        assert command_runner._AGENT_CANCEL_CB is None
        assert _key_listeners._resolve_cancel_handler(None) is None
        assert get_pause_controller().pending_steer_counts() == (0, 0)
        assert not get_pause_controller().is_paused()
        assert late_task() in cancellation._detached_tasks
        assert not late_task().done()
        assert asyncio.all_tasks() - baseline == {late_task()}

        async def next_judges(**kwargs):
            next_ready.set()
            await asyncio.Event().wait()

        next_run = goal_runs.create("next goal")
        state.start(next_run.prompt, mode="goal", run_id=next_run.run_id)
        monkeypatch.setattr(register_callbacks, "_run_goal_judges", next_judges)
        next_task = asyncio.create_task(
            register_callbacks._on_interactive_turn_end(
                SimpleNamespace(), next_run.prompt
            )
        )
        await asyncio.wait_for(next_ready.wait(), 2)
        next_cancel = command_runner._AGENT_CANCEL_CB
        next_detach = _run_signals._detach_event
        assert next_cancel is not None and next_cancel is not cancel
        assert next_detach is not None
        del cancel
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
        # Let task completion and its observer release the last strong reference.
        for _ in range(3):
            await asyncio.sleep(0)
        gc.collect()
        assert not cancellation._detached_tasks
        assert late_task() is None
        unobserved.assert_not_called()
        assert not next_task.done()
        assert state.get_state().run_id == next_run.run_id
        assert run_ui.is_run_active()
        assert command_runner._AGENT_CANCEL_CB is next_cancel
        assert _key_listeners._resolve_cancel_handler(None) is next_cancel
        assert _run_signals._detach_event is next_detach
        assert not next_detach.is_set()
        assert goal_runs.load(judge_phase.run.run_id).status == "interrupted"
        next_cancel()
        assert await asyncio.wait_for(next_task, 2) is None
        assert goal_runs.load(next_run.run_id).status == "interrupted"
    finally:
        # Release before cancelling: a regression must not wedge pytest shutdown.
        release.set()
        tasks = [pending for pending in (task, next_task) if pending is not None]
        for pending in tasks:
            pending.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if late_task is not None and late_task() is not None:
            await asyncio.gather(late_task(), return_exceptions=True)


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
@pytest.mark.parametrize("listener", ["persistent", "owned", "no-tty"])
async def test_judge_cancel_controls_always_clean_up(
    judge_phase, monkeypatch, outcome, listener
):
    handle = judge_phase.handle if listener != "no-tty" else None
    if listener != "persistent":
        monkeypatch.setattr(
            _key_listeners, "acquire_listener", lambda *a, **k: (handle, True)
        )
    monkeypatch.setattr(cancellation, "sigint_fallback_cancels", lambda: True)
    previous_signal = signal.getsignal(signal.SIGINT)

    async def judge(**kwargs):
        assert run_ui.is_run_active()
        if outcome == "cancel":
            raise asyncio.CancelledError
        if outcome == "error":
            raise RuntimeError("judge failure")
        return True, "", []

    monkeypatch.setattr(register_callbacks, "_run_goal_judges", judge)
    expectation = (
        pytest.raises(RuntimeError, match="judge failure")
        if outcome == "error"
        else nullcontext()
    )
    baseline = asyncio.all_tasks()
    with expectation:
        await register_callbacks._on_interactive_turn_end(
            SimpleNamespace(), judge_phase.run.prompt
        )
    assert not run_ui.is_run_active()
    assert not asyncio.all_tasks() - baseline
    assert _run_signals._detach_event is None
    assert _key_listeners._resolve_cancel_handler(None) is None
    assert command_runner._AGENT_CANCEL_CB is None
    assert signal.getsignal(signal.SIGINT) is previous_signal
    if listener == "owned":
        handle.stop.assert_called_once()
        handle.thread.join.assert_called_once()
    elif listener == "persistent":
        handle.stop.assert_not_called()


@pytest.mark.parametrize("non_main_thread", [False, True])
async def test_judging_preserves_sigint_when_not_cancel_gesture(
    judge_phase, monkeypatch, non_main_thread
):
    monkeypatch.setattr(
        cancellation, "sigint_fallback_cancels", lambda: non_main_thread
    )
    if non_main_thread:
        monkeypatch.setattr(
            cancellation,
            "threading",
            SimpleNamespace(
                Event=threading.Event,
                current_thread=lambda: object(),
                main_thread=threading.main_thread,
            ),
        )
    install_signal = Mock(side_effect=AssertionError("SIGINT must stay untouched"))
    monkeypatch.setattr(
        cancellation,
        "signal",
        SimpleNamespace(signal=install_signal, SIGINT=signal.SIGINT),
    )

    async def judge(**kwargs):
        assert _key_listeners._resolve_cancel_handler(None) is not None
        return True, "", []

    monkeypatch.setattr(register_callbacks, "_run_goal_judges", judge)
    assert (
        await register_callbacks._on_interactive_turn_end(
            SimpleNamespace(), judge_phase.run.prompt
        )
        is None
    )
    install_signal.assert_not_called()


async def test_listener_setup_failure_restores_controls(judge_phase, monkeypatch):
    previous_signal = signal.getsignal(signal.SIGINT)
    monkeypatch.setattr(cancellation, "sigint_fallback_cancels", lambda: True)

    def fail_listener(*args, **kwargs):
        raise RuntimeError("listener failure")

    monkeypatch.setattr(_key_listeners, "acquire_listener", fail_listener)
    judge = Mock()

    async def work():
        judge()

    baseline = asyncio.all_tasks()
    with pytest.raises(RuntimeError, match="listener failure"):
        await cancellation.run_with_cancel_controls(work())
    assert not asyncio.all_tasks() - baseline
    assert not run_ui.is_run_active()
    assert _run_signals._detach_event is None
    assert command_runner._AGENT_CANCEL_CB is None
    assert signal.getsignal(signal.SIGINT) is previous_signal
    judge.assert_not_called()


@pytest.mark.parametrize("outcome", ["success", "cancel"])
async def test_judge_cancellation_discards_steers_but_success_defers_them(
    judge_phase, monkeypatch, outcome
):
    from code_puppy.messaging.pause_controller import get_pause_controller

    pc = get_pause_controller()
    # Exercise the actual teardown deferral, not the fixture's no-op.
    monkeypatch.setattr(run_ui, "_defer_undelivered_steers", pc.defer_pending_steer_now)

    async def judge(**kwargs):
        pc.pause()
        pc.request_steer("now input", mode="now", history_recorded=True)
        pc.request_steer("queued input", mode="queue", history_recorded=True)
        if outcome == "cancel":
            raise asyncio.CancelledError
        return True, "", []

    monkeypatch.setattr(register_callbacks, "_run_goal_judges", judge)
    await register_callbacks._on_interactive_turn_end(
        SimpleNamespace(), judge_phase.run.prompt
    )
    if outcome == "cancel":
        assert not pc.is_paused()
        assert pc.pending_steer_counts() == (0, 0)
    else:
        assert pc.pending_steer_counts() == (0, 2)
        assert pc.drain_pending_steer_queued() == ["queued input", "now input"]
