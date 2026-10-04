"""Small, explicitly typed Win32 adapter; imported only on Windows.

Coordinates are physical pixels under a per-thread DPI awareness context.
No process-wide DPI changes, clipboard writes, shell commands, or elevation.
"""

from __future__ import annotations

import ctypes as ct
import time
from contextlib import contextmanager
from ctypes import wintypes as wt
from pathlib import PureWindowsPath

from .backend_types import ComputerUseError as DesktopError

user32 = ct.WinDLL("user32", use_last_error=True)
kernel32 = ct.WinDLL("kernel32", use_last_error=True)
dwmapi = ct.WinDLL("dwmapi", use_last_error=True)
ULONG_PTR = ct.c_size_t
WNDENUMPROC = ct.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


class MOUSEINPUT(ct.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ct.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ct.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class INPUTUNION(ct.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ct.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", wt.DWORD), ("data", INPUTUNION)]


def _bind(library, name, result, *arguments):
    function = getattr(library, name)
    function.restype = result
    function.argtypes = arguments
    return function


_bind(user32, "EnumWindows", wt.BOOL, WNDENUMPROC, wt.LPARAM)
_bind(user32, "IsWindow", wt.BOOL, wt.HWND)
_bind(user32, "IsWindowVisible", wt.BOOL, wt.HWND)
_bind(user32, "IsIconic", wt.BOOL, wt.HWND)
_bind(user32, "GetWindowTextLengthW", ct.c_int, wt.HWND)
_bind(user32, "GetWindowTextW", ct.c_int, wt.HWND, wt.LPWSTR, ct.c_int)
_bind(user32, "GetClassNameW", ct.c_int, wt.HWND, wt.LPWSTR, ct.c_int)
_bind(user32, "GetWindowRect", wt.BOOL, wt.HWND, ct.POINTER(wt.RECT))
_bind(user32, "GetWindowThreadProcessId", wt.DWORD, wt.HWND, ct.POINTER(wt.DWORD))
_bind(user32, "GetForegroundWindow", wt.HWND)
_bind(user32, "SetForegroundWindow", wt.BOOL, wt.HWND)
_bind(user32, "ShowWindow", wt.BOOL, wt.HWND, ct.c_int)
_bind(user32, "GetSystemMetrics", ct.c_int, ct.c_int)
_bind(user32, "GetAsyncKeyState", wt.SHORT, ct.c_int)
_bind(user32, "GetCursorPos", wt.BOOL, ct.POINTER(wt.POINT))
_bind(user32, "SetCursorPos", wt.BOOL, ct.c_int, ct.c_int)
_bind(user32, "WindowFromPoint", wt.HWND, wt.POINT)
_bind(user32, "GetAncestor", wt.HWND, wt.HWND, wt.UINT)
_bind(user32, "SendInput", wt.UINT, wt.UINT, ct.POINTER(INPUT), ct.c_int)
_bind(user32, "SetThreadDpiAwarenessContext", wt.HANDLE, wt.HANDLE)
_bind(user32, "OpenInputDesktop", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
_bind(
    user32,
    "GetUserObjectInformationW",
    wt.BOOL,
    wt.HANDLE,
    ct.c_int,
    wt.LPVOID,
    wt.DWORD,
    ct.POINTER(wt.DWORD),
)
_bind(user32, "CloseDesktop", wt.BOOL, wt.HANDLE)
_bind(kernel32, "OpenProcess", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
_bind(
    kernel32,
    "QueryFullProcessImageNameW",
    wt.BOOL,
    wt.HANDLE,
    wt.DWORD,
    wt.LPWSTR,
    ct.POINTER(wt.DWORD),
)
_bind(kernel32, "CloseHandle", wt.BOOL, wt.HANDLE)
_bind(dwmapi, "DwmGetWindowAttribute", wt.LONG, wt.HWND, wt.DWORD, wt.LPVOID, wt.DWORD)


@contextmanager
def physical_pixels():
    previous = user32.SetThreadDpiAwarenessContext(ct.c_void_p(-4))
    if not previous:
        raise DesktopError(
            "Per-monitor DPI context unavailable; Windows 10/11 required"
        )
    try:
        yield
    finally:
        user32.SetThreadDpiAwarenessContext(previous)


def desktop_ready() -> None:
    handle = user32.OpenInputDesktop(0, False, 1)  # DESKTOP_READOBJECTS
    if not handle:
        raise DesktopError(
            "Interactive desktop unavailable (locked, UAC, or disconnected)"
        )
    try:
        name = ct.create_unicode_buffer(256)
        required = wt.DWORD()
        if not user32.GetUserObjectInformationW(
            handle, 2, name, ct.sizeof(name), ct.byref(required)
        ):
            raise DesktopError("Cannot verify input desktop")
        if name.value.casefold() != "default":
            raise DesktopError("Secure/non-default desktop is never automated")
    finally:
        user32.CloseDesktop(handle)


def emergency_stop() -> bool:
    """Pause/Break held OR pointer at top-left corner of the primary monitor."""
    point = wt.POINT()
    return bool(user32.GetAsyncKeyState(0x13) & 0x8000) or bool(
        user32.GetCursorPos(ct.byref(point)) and 0 <= point.x <= 1 and 0 <= point.y <= 1
    )


def window_info(hwnd: int) -> dict:
    if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
        raise DesktopError("Target window no longer exists or is hidden")
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ct.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)
    if not handle:
        raise DesktopError(
            "Cannot inspect target process; privileged windows are unsupported"
        )
    try:
        path = ct.create_unicode_buffer(32768)
        size = wt.DWORD(len(path))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, path, ct.byref(size)):
            raise DesktopError("Cannot identify window executable")
    finally:
        kernel32.CloseHandle(handle)
    title = ct.create_unicode_buffer(min(user32.GetWindowTextLengthW(hwnd) + 1, 4096))
    user32.GetWindowTextW(hwnd, title, len(title))
    class_name = ct.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, class_name, len(class_name))
    rect = wt.RECT()
    # Visible frame bounds exclude invisible resize borders (often -8 at the
    # edge of a maximized window). DWM returns physical pixels even at high DPI.
    result = dwmapi.DwmGetWindowAttribute(hwnd, 9, ct.byref(rect), ct.sizeof(rect))
    if result != 0 and not user32.GetWindowRect(hwnd, ct.byref(rect)):
        raise DesktopError("Cannot read window bounds")
    return {
        "window_id": int(hwnd),
        "pid": pid.value,
        "title": title.value,
        "executable": path.value,
        "process": PureWindowsPath(path.value).name,
        "class_name": class_name.value,
        "bounds": [rect.left, rect.top, rect.right, rect.bottom],
        "minimized": bool(user32.IsIconic(hwnd)),
        "foreground": user32.GetForegroundWindow() == hwnd,
    }


def list_windows() -> list[dict]:
    windows = []

    @WNDENUMPROC
    def visit(hwnd, _):
        try:
            item = window_info(hwnd)
            if item["title"]:
                windows.append(item)
        except DesktopError:
            pass
        return True

    if not user32.EnumWindows(visit, 0):
        raise DesktopError("Cannot enumerate desktop windows")
    return windows


def foreground(hwnd: int) -> None:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    if user32.GetForegroundWindow() != hwnd:
        user32.SetForegroundWindow(hwnd)
        for _ in range(20):
            if user32.GetForegroundWindow() == hwnd:
                break
            time.sleep(0.025)
    assert_foreground(hwnd)


def assert_foreground(hwnd: int) -> None:
    if user32.GetForegroundWindow() != hwnd:
        raise DesktopError(
            "Target lost focus. Focus it manually and take a fresh snapshot"
        )


def check_point(hwnd: int, x: int, y: int) -> None:
    hit = user32.WindowFromPoint(wt.POINT(x, y))
    if not hit or user32.GetAncestor(hit, 2) != hwnd:  # GA_ROOT
        raise DesktopError(
            "Click point is covered by another window; take a new snapshot"
        )


def move(x: int, y: int) -> None:
    if not user32.SetCursorPos(x, y):
        raise DesktopError("Windows refused pointer movement")


def _send(event: INPUT) -> None:
    if user32.SendInput(1, ct.byref(event), ct.sizeof(INPUT)) != 1:
        raise DesktopError("Windows rejected input (possibly an elevated application)")


def mouse_event(flags: int, data: int = 0) -> None:
    event = INPUT(type=0)
    event.mi = MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, 0)
    _send(event)


def key_event(code: int, up: bool = False, unicode: bool = False) -> None:
    event = INPUT(type=1)
    flags = (2 if up else 0) | (4 if unicode else 0)
    if not unicode and code in {
        0x21,
        0x22,
        0x23,
        0x24,
        0x25,
        0x26,
        0x27,
        0x28,
        0x2D,
        0x2E,
        0x5B,
        0x5C,
    }:
        flags |= 1  # KEYEVENTF_EXTENDEDKEY
    event.ki = KEYBDINPUT(0 if unicode else code, code if unicode else 0, flags, 0, 0)
    _send(event)
