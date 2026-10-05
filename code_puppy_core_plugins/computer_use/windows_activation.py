"""Verified Windows activation, without keyboard tricks or input-queue attachment.

Kept independent of ctypes so refusal/race/cancellation cases can run on all CI
platforms. UIA success is only a request: the actual foreground HWND is authoritative.
"""

from __future__ import annotations

import time

from .backend_types import ComputerUseError


def _wait_for_window(user32, hwnd, check):
    for _ in range(20):
        check()
        if user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.025)
    check()
    return user32.GetForegroundWindow() == hwnd


def activate_window(user32, hwnd, *, focus_window=None, check=lambda: None):
    """Try Win32, then an optional real UIA SetFocus request; fail closed.

    The backend's check callback revalidates consent, pause, cancellation, desktop,
    process identity, and policy, including immediately after either request.
    """
    check()
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    check()
    if user32.GetForegroundWindow() == hwnd:
        return
    accepted = bool(user32.SetForegroundWindow(hwnd))
    if _wait_for_window(user32, hwnd, check):
        return

    uia_error = None
    if focus_window is not None:
        check()
        try:
            focus_window(hwnd)
        except ComputerUseError as exc:
            uia_error = str(exc)
        check()
        if uia_error is None and _wait_for_window(user32, hwnd, check):
            return

    actual = user32.GetForegroundWindow()
    detail = f" UIA focus failed: {uia_error}" if uia_error else ""
    raise ComputerUseError(
        f"Windows did not activate target hwnd:{hwnd}; foreground HWND is {actual}. "
        f"SetForegroundWindow accepted={accepted}; "
        f"UIA focus attempted={focus_window is not None}.{detail} "
        "This activation attempt sent no click or keystroke. "
        "Focus the target manually, avoid switching windows during the operation, "
        "and request fresh app state."
    )
