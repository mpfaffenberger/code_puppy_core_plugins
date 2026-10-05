"""Cross-platform tests for Win32 refusal, UIA recovery, and fail-closed races."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from code_puppy_core_plugins.computer_use import windows_activation as activation
from code_puppy_core_plugins.computer_use.backend_types import ComputerUseError


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(activation.time, "sleep", lambda seconds: None)
    state = SimpleNamespace(foreground=7)
    return SimpleNamespace(
        state=state,
        IsIconic=Mock(return_value=False),
        ShowWindow=Mock(),
        GetForegroundWindow=lambda: state.foreground,
        SetForegroundWindow=Mock(return_value=False),
    )


def focus_callback(api):
    def focus(hwnd):
        api.state.foreground = hwnd

    return Mock(side_effect=focus)


def test_already_foreground_needs_no_request(api):
    api.state.foreground = 42
    focus = Mock()
    activation.activate_window(api, 42, focus_window=focus)
    api.SetForegroundWindow.assert_not_called()
    focus.assert_not_called()


def test_successful_win32_does_not_use_uia(api):
    def win32(hwnd):
        api.state.foreground = hwnd
        return True

    api.SetForegroundWindow.side_effect = win32
    focus = Mock()
    activation.activate_window(api, 42, focus_window=focus)
    focus.assert_not_called()


def test_actual_foreground_is_authoritative_even_if_return_false(api):
    api.SetForegroundWindow.side_effect = lambda hwnd: setattr(
        api.state, "foreground", hwnd
    )
    activation.activate_window(api, 42)
    assert api.state.foreground == 42


@pytest.mark.parametrize("accepted", [False, True])
def test_uia_recovers_unconfirmed_win32_request(api, accepted):
    api.SetForegroundWindow.return_value = accepted
    focus = focus_callback(api)
    activation.activate_window(api, 42, focus_window=focus)
    focus.assert_called_once_with(42)
    assert api.state.foreground == 42


def test_uia_success_response_without_actual_focus_is_not_success(api):
    focus = Mock(return_value={"success": True})
    with pytest.raises(
        ComputerUseError, match="Windows did not activate target hwnd:42"
    ):
        activation.activate_window(api, 42, focus_window=focus)
    assert api.state.foreground == 7


def test_no_uia_callback_reports_refusal_not_lost_focus(api):
    with pytest.raises(ComputerUseError, match="UIA focus attempted=False"):
        activation.activate_window(api, 42)


def test_unsupported_uia_fails_with_manual_recovery_instructions(api):
    focus = Mock(side_effect=ComputerUseError("No focus support"))
    with pytest.raises(ComputerUseError, match="Focus the target manually") as error:
        activation.activate_window(api, 42, focus_window=focus)
    assert "No focus support" in str(error.value)
    assert "accepted=False" in str(error.value)


def test_minimized_window_is_restored(api):
    api.IsIconic.return_value = True
    activation.activate_window(api, 42, focus_window=focus_callback(api))
    api.ShowWindow.assert_called_once_with(42, 9)


def test_guard_failure_prevents_all_requests(api):
    check = Mock(side_effect=ComputerUseError("paused"))
    focus = Mock()
    with pytest.raises(ComputerUseError, match="paused"):
        activation.activate_window(api, 42, focus_window=focus, check=check)
    api.ShowWindow.assert_not_called()
    api.SetForegroundWindow.assert_not_called()
    focus.assert_not_called()


def test_cancellation_during_win32_wait_prevents_fallback(api):
    def check():
        if api.SetForegroundWindow.called:
            raise ComputerUseError("cancelled")

    focus = Mock()
    with pytest.raises(ComputerUseError, match="cancelled"):
        activation.activate_window(api, 42, focus_window=focus, check=check)
    focus.assert_not_called()


def test_policy_revoked_during_uia_request_is_not_success(api):
    focus = focus_callback(api)

    def check():
        if focus.called:
            raise ComputerUseError("denied")

    with pytest.raises(ComputerUseError, match="denied"):
        activation.activate_window(api, 42, focus_window=focus, check=check)


@pytest.mark.parametrize("focusable", [True, False])
def test_adapter_requests_only_advertised_raw_com_focus(focusable):
    from code_puppy_core_plugins.computer_use.windows_accessibility import (
        WindowsAccessibility,
    )

    raw = SimpleNamespace(
        CurrentIsEnabled=True,
        CurrentIsPassword=False,
        CurrentIsKeyboardFocusable=focusable,
        SetFocus=Mock(),
    )
    root = SimpleNamespace(element_info=SimpleNamespace(element=raw), set_focus=Mock())
    adapter = WindowsAccessibility()
    adapter._root = Mock(return_value=root)
    if focusable:
        assert adapter.focus_window(42)["success"]
        raw.SetFocus.assert_called_once_with()
    else:
        with pytest.raises(ComputerUseError, match="not supported"):
            adapter.focus_window(42)
        raw.SetFocus.assert_not_called()
    adapter._root.assert_called_once_with(42)
    root.set_focus.assert_not_called()  # pywinauto's input fallback is forbidden


def test_unexpected_failure_does_not_fall_through_to_input(api):
    focus = Mock(side_effect=RuntimeError("unexpected"))
    with pytest.raises(RuntimeError, match="unexpected"):
        activation.activate_window(api, 42, focus_window=focus)
