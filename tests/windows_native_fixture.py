"""Disposable native Win32 UIA fixture. Only the live smoke script launches it."""

from __future__ import annotations

import argparse
import ctypes
import json


def main():
    import win32api
    import win32con as c
    import win32gui as gui

    parser = argparse.ArgumentParser()
    parser.add_argument("--x", type=int, default=180)
    parser.add_argument("--y", type=int, default=180)
    parser.add_argument("--cover", action="store_true")
    args = parser.parse_args()
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    ctypes.WinDLL("Msftedit.dll")
    instance = win32api.GetModuleHandle(None)
    children = {}
    clicks = 0

    def procedure(hwnd, message, wparam, lparam):
        nonlocal clicks
        if message == c.WM_COMMAND and (wparam & 0xFFFF) == 102:
            clicks += 1
            gui.SetWindowText(children["status"], f"Clicks: {clicks}")
            return 0
        if message == c.WM_CLOSE:
            gui.DestroyWindow(hwnd)
            return 0
        if message == c.WM_DESTROY:
            gui.PostQuitMessage(0)
            return 0
        return gui.DefWindowProc(hwnd, message, wparam, lparam)

    klass = gui.WNDCLASS()
    klass.hInstance = instance
    klass.lpszClassName = "PuppyParityNativeFixture"
    klass.lpfnWndProc = procedure
    brush = gui.CreateSolidBrush(0xFF0000) if args.cover else c.COLOR_WINDOW + 1
    klass.hbrBackground = brush
    klass.hCursor = gui.LoadCursor(0, c.IDC_ARROW)
    gui.RegisterClass(klass)
    hwnd = gui.CreateWindow(
        klass.lpszClassName,
        "Code Puppy Native Parity Fixture",
        c.WS_OVERLAPPEDWINDOW | c.WS_VISIBLE,
        args.x,
        args.y,
        650,
        610,
        0,
        0,
        instance,
        None,
    )

    if args.cover:
        gui.SetWindowText(hwnd, "Code Puppy Blue Occluder Fixture")
        gui.ShowWindow(hwnd, c.SW_SHOW)
        gui.UpdateWindow(hwnd)
        print(json.dumps({"hwnd": hwnd, "children": {}}), flush=True)
        gui.PumpMessages()
        return

    def child(name, cls, title, style, x, y, width, height, ident):
        handle = gui.CreateWindow(
            cls,
            title,
            c.WS_CHILD | c.WS_VISIBLE | style,
            x,
            y,
            width,
            height,
            hwnd,
            ident,
            instance,
            None,
        )
        children[name] = handle
        return handle

    child(
        "edit",
        "EDIT",
        "Initial value",
        c.WS_BORDER | c.ES_AUTOHSCROLL | c.WS_TABSTOP,
        20,
        20,
        560,
        28,
        101,
    )
    child(
        "button",
        "BUTTON",
        "Invoke test",
        c.BS_PUSHBUTTON | c.WS_TABSTOP,
        20,
        65,
        160,
        32,
        102,
    )
    child(
        "checkbox",
        "BUTTON",
        "Toggle test",
        c.BS_AUTOCHECKBOX | c.WS_TABSTOP,
        200,
        65,
        160,
        32,
        103,
    )
    child("status", "STATIC", "Clicks: 0", 0, 390, 65, 170, 32, 104)
    child(
        "password",
        "EDIT",
        "SECRET_FIXTURE_VALUE",
        c.WS_BORDER | c.ES_PASSWORD,
        20,
        115,
        300,
        28,
        105,
    )
    rich = child(
        "rich",
        "RICHEDIT50W",
        "",
        c.WS_BORDER
        | c.ES_MULTILINE
        | c.ES_AUTOVSCROLL
        | c.WS_VSCROLL
        | c.WS_TABSTOP
        | c.ES_NOHIDESEL,
        20,
        160,
        560,
        315,
        106,
    )
    text = "prefix \U0001f436 red dog, blue dog suffix\r\n" + "\r\n".join(
        f"Scrollable line {index}: native UI Automation content" for index in range(100)
    )
    gui.SetWindowText(rich, text)
    gui.ShowWindow(hwnd, c.SW_SHOW)
    gui.UpdateWindow(hwnd)
    print(json.dumps({"hwnd": hwnd, "children": children}), flush=True)
    gui.PumpMessages()


if __name__ == "__main__":
    main()
