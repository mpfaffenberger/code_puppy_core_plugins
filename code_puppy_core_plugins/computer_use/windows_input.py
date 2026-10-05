"""Balanced Windows input sequences; never use the clipboard or shell."""

from __future__ import annotations

import math
import time

from .backend_types import ComputerUseError

KEYS = {
    "return": 13,
    "enter": 13,
    "tab": 9,
    "space": 32,
    "delete": 46,
    "backspace": 8,
    "escape": 27,
    "esc": 27,
    "home": 36,
    "end": 35,
    "pageup": 33,
    "pagedown": 34,
    "left": 37,
    "up": 38,
    "right": 39,
    "down": 40,
    **{chr(code).lower(): code for code in range(65, 91)},
    **{str(number): 48 + number for number in range(10)},
    **{f"f{number}": 111 + number for number in range(1, 13)},
}
MODIFIERS = {
    "control": 17,
    "ctrl": 17,
    "shift": 16,
    "option": 18,
    "alt": 18,
    "command": 91,
    "cmd": 91,
    "win": 91,
    "windows": 91,
}


def press_key(native, key, modifiers, guard):
    code = KEYS.get(key.casefold())
    if code is None:
        raise ComputerUseError(f"Unsupported Windows key: {key}")
    codes = []
    for name in modifiers or []:
        if name.casefold() not in MODIFIERS:
            raise ComputerUseError(f"Unsupported Windows modifier: {name}")
        modifier = MODIFIERS[name.casefold()]
        if modifier not in codes:
            codes.append(modifier)
    if code == 46 and 17 in codes and 18 in codes:
        raise ComputerUseError("Secure attention shortcuts are unsupported")
    if code == 76 and 91 in codes:
        raise ComputerUseError("Lock-screen shortcuts are unsupported")
    pressed = []
    try:
        for item in [*codes, code]:
            guard()
            native.key_event(item)
            pressed.append(item)
    finally:
        errors = []
        for item in reversed(pressed):
            try:
                native.key_event(item, up=True)
            except ComputerUseError as exc:
                errors.append(exc)
        if errors:
            raise errors[0]


def type_text(native, text, guard):
    if len(text) > 16000:
        raise ComputerUseError("Type at most 16000 characters per action")
    try:
        text.encode("utf-16-le")
    except UnicodeEncodeError as exc:
        raise ComputerUseError("Text contains an invalid Unicode surrogate") from exc
    for character in text.replace("\r\n", "\n").replace("\r", "\n"):
        guard()
        if character in "\n\t":
            code = 13 if character == "\n" else 9
            native.key_event(code)
            native.key_event(code, up=True)
        else:
            data = character.encode("utf-16-le")
            for index in range(0, len(data), 2):
                code = int.from_bytes(data[index : index + 2], "little")
                native.key_event(code, unicode=True)
                native.key_event(code, up=True, unicode=True)


def click(native, hwnd, point, button, count, guard):
    if button not in {"left", "right", "middle"} or count not in {1, 2, 3}:
        raise ComputerUseError("Use left/right/middle and click_count 1..3")
    down, up = {"left": (2, 4), "right": (8, 16), "middle": (32, 64)}[button]
    guard()
    native.check_point(hwnd, *point)
    native.move(*point)
    for _ in range(count):
        guard()
        native.check_point(hwnd, *point)
        native.mouse_event(down)
        try:
            time.sleep(0.025)
        finally:
            native.mouse_event(up)


def drag(native, hwnd, start, end, duration, guard):
    if not math.isfinite(duration) or not 0.05 <= duration <= 10:
        raise ComputerUseError(
            "Drag duration must be finite and between 0.05 and 10 seconds"
        )
    guard()
    native.check_point(hwnd, *start)
    native.check_point(hwnd, *end)
    native.move(*start)
    native.mouse_event(2)
    try:
        steps = max(2, math.ceil(duration / 0.025))
        for step in range(1, steps + 1):
            guard()
            point = tuple(round(a + (b - a) * step / steps) for a, b in zip(start, end))
            native.check_point(hwnd, *point)
            native.move(*point)
            time.sleep(duration / steps)
    finally:
        native.mouse_event(4)
