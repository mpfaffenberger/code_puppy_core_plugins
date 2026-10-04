"""Opt-in activation diagnostic; touches only two newly created fixture windows.

Uses the current consent policy, never enables it. No synthetic Alt keys,
AttachThreadInput, elevation, foreground-lock changes, or user-document input.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from code_puppy_core_plugins.computer_use import windows_native as native
    from code_puppy_core_plugins.computer_use.policy import policy_store
    from code_puppy_core_plugins.computer_use.windows_runtime import WindowsRuntime

    policy_store.require_enabled()
    runtime = WindowsRuntime()
    fixtures = []

    def inspect_fixture(hwnd, pid):
        policy_store.require_enabled()
        native.desktop_ready()
        if native.emergency_stop():
            raise RuntimeError("Emergency stop: diagnostic aborted")
        info = native.window_info(hwnd)
        if info["pid"] != pid or info["class_name"] != "PuppyParityNativeFixture":
            raise RuntimeError("Fixture identity changed; refusing interaction")
        policy_store.require(info["executable"])
        policy_store.require(info["process"])
        return info

    def attempt(hwnd, pid, method):
        inspect_fixture(hwnd, pid)
        before = native.user32.GetForegroundWindow()
        accepted = None
        error = None
        if method == "win32":
            accepted = bool(native.user32.SetForegroundWindow(hwnd))
        elif method == "backend":
            adapter = runtime._backend.accessibility
            original_focus = adapter.focus_window
            fallbacks = []

            def observed_focus(target):
                fallbacks.append(target)
                return original_focus(target)

            adapter.focus_window = observed_focus
            try:
                result = runtime._backend.get_app_state(f"hwnd:{hwnd}", 30)
            finally:
                adapter.focus_window = original_focus
            assert result["success"] and result["nodes"]
            print(
                json.dumps(
                    {
                        "state_revision": result["state_revision"],
                        "uia_fallback_requests": fallbacks,
                        "screenshot_path": result["screenshot_path"],
                    }
                ),
                flush=True,
            )
        else:
            adapter = runtime._backend.accessibility
            try:
                root = adapter._root(hwnd)
                adapter.perform_action(root, "UIASetFocus")
            except Exception as exc:  # noqa: BLE001 - diagnostic records provider failures.
                error = str(exc)
        for _ in range(20):
            if native.user32.GetForegroundWindow() == hwnd:
                break
            time.sleep(0.025)
        after = native.user32.GetForegroundWindow()
        print(
            json.dumps(
                {
                    "method": method,
                    "target": hwnd,
                    "before": before,
                    "after": after,
                    "request_accepted": accepted,
                    "verified": after == hwnd,
                    "error": error,
                }
            ),
            flush=True,
        )

    try:
        for x in (180, 880):
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("windows_native_fixture.py")),
                    "--x",
                    str(x),
                    "--y",
                    "180",
                ],
                stdout=subprocess.PIPE,
                text=True,
            )
            fixtures.append((process, None, None))
            data = json.loads(process.stdout.readline())
            hwnd = data["hwnd"]
            info = native.window_info(hwnd)
            fixtures[-1] = (process, hwnd, info["pid"])
        for _ in range(3):
            for method in ("win32", "backend", "uia"):
                for _, hwnd, pid in fixtures:
                    runtime.run_request(
                        attempt, (hwnd, pid, method), {}, threading.Event()
                    )
    finally:
        import win32gui

        for process, hwnd, pid in fixtures:
            if hwnd and native.user32.IsWindow(hwnd):
                info = native.window_info(hwnd)
                if (
                    info["pid"] == pid
                    and info["class_name"] == "PuppyParityNativeFixture"
                ):
                    win32gui.PostMessage(
                        hwnd, 0x0010, 0, 0
                    )  # WM_CLOSE: owned fixture only
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print(
                    f"Fixture launcher {process.pid} remains; inspect before cleanup."
                )
        runtime._executor.shutdown()


if __name__ == "__main__":
    main()
