"""Live parity checks in a disposable native window, never existing documents.

Run from the checkout with its test environment. Screenshots/consent use a temp
folder. The optional --x/--y place this fixture on a particular physical monitor.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from code_puppy_core_plugins.computer_use import tools
from code_puppy_core_plugins.computer_use.policy import PolicyStore
from code_puppy_core_plugins.computer_use.state import StateStore
from code_puppy_core_plugins.computer_use.windows_runtime import (
    WindowsRuntime,
)
from tests.agent_test_support import FakeAgent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--x", type=int, default=180)
    parser.add_argument("--y", type=int, default=180)
    args = parser.parse_args()
    fixture = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("windows_native_fixture.py")),
            "--x",
            str(args.x),
            "--y",
            str(args.y),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    runtime = WindowsRuntime()
    try:
        line = fixture.stdout.readline()
        if not line:
            raise RuntimeError("Native fixture failed to start")
        handles = json.loads(line)
        app = f"hwnd:{handles['hwnd']}"
        with tempfile.TemporaryDirectory(prefix="puppy-parity-") as directory:
            folder = Path(directory)
            counter = 0

            def setup():
                from code_puppy_core_plugins.computer_use.windows_capture import (
                    capture_window,
                )

                backend = runtime._backend
                backend.policy = PolicyStore(folder / "consent.json")
                backend.policy.set_enabled(True)
                backend.states = StateStore()

                def capture(info, target):
                    nonlocal counter
                    counter += 1
                    return capture_window(info, folder / f"capture-{counter}.png")

                backend._capture = capture

            runtime.run_request(setup, (), {}, threading.Event())
            tools.backend = runtime
            tools.emit_inline_image = lambda path: False
            agent = FakeAgent()
            for register in tools.REGISTRARS.values():
                register(agent)

            async def call(name, **kwargs):
                result = await agent.registered[name](None, **kwargs)
                if hasattr(result, "return_value"):
                    assert result.content, "PNG not attached to model result"
                    return result.metadata
                return result

            async def run():
                async def state():
                    result = await call(
                        "computer_get_app_state", app_name=app, max_nodes=100
                    )
                    assert result["success"], result
                    assert "SECRET_FIXTURE_VALUE" not in json.dumps(result)
                    return result

                def node(snapshot, *, title=None, action=None, role=None):
                    choices = [
                        item
                        for item in snapshot["nodes"]
                        if (title is None or item.get("title") == title)
                        and (action is None or action in item.get("actions", []))
                        and (role is None or item.get("role") == role)
                    ]
                    assert choices, (title, action, role, snapshot["nodes"])
                    return choices[0]["id"]

                first = await state()
                print(
                    "PASS: real shared get_app_state tool, native WGC PNG, UIA tree, password redaction"
                )
                print(
                    "Exposed roles/actions:",
                    [(n["role"], n.get("actions")) for n in first["nodes"]],
                )
                edit = node(first, action="UIASetValue", role="Edit")
                result = await call(
                    "computer_set_value",
                    state_revision=first["state_revision"],
                    element_id=edit,
                    value="Unicode caf\u00e9 \U0001f436 {literal}",
                )
                assert result["success"], result
                second = await state()
                assert any(
                    n.get("value") == "Unicode caf\u00e9 \U0001f436 {literal}"
                    for n in second["nodes"]
                )
                print(
                    "PASS: semantic ValuePattern set_value verified through a fresh snapshot"
                )
                button = node(second, title="Invoke test", action="UIAInvoke")
                result = await call(
                    "computer_perform_action",
                    state_revision=second["state_revision"],
                    element_id=button,
                    action="UIAInvoke",
                )
                assert result["success"], result
                third = await state()
                assert any(n.get("title") == "Clicks: 1" for n in third["nodes"])
                print(
                    "PASS: advertised InvokePattern action delivered to native button"
                )
                checkbox = node(third, title="Toggle test", action="UIAToggle")
                result = await call(
                    "computer_click",
                    state_revision=third["state_revision"],
                    element_id=checkbox,
                )
                assert result["success"], result
                fourth = await state()
                rich = node(fourth, action="UIASelectText", role="Document")
                result = await call(
                    "computer_select_text",
                    state_revision=fourth["state_revision"],
                    element_id=rich,
                    text="dog",
                    prefix="blue ",
                )
                assert result["success"], result

                def selected_text():
                    element = runtime._backend.states.current().elements[rich]
                    selection = element.iface_text.GetSelection()
                    return selection.GetElement(0).GetText(-1)

                selected = runtime.run_request(selected_text, (), {}, threading.Event())
                assert selected == "dog", selected
                print(
                    "PASS: UIA TextPattern contextual exact selection after supplementary Unicode"
                )
                fifth = await state()
                result = await call(
                    "computer_scroll",
                    state_revision=fifth["state_revision"],
                    direction="down",
                    pages=0.5,
                )
                assert result["success"], result
                print("PASS: semantic fractional-page scrolling")
                sixth = await state()
                edit = node(sixth, role="Edit", action="UIASetValue")
                result = await call(
                    "computer_use_batch",
                    state_revision=sixth["state_revision"],
                    steps=[
                        {
                            "action": "perform_action",
                            "element_id": edit,
                            "action_name": "UIASetFocus",
                        },
                    ],
                )
                # perform_action's argument is 'action', which collides with the
                # batch discriminator in the legacy API; check the documented
                # disambiguated action_name alias provided by the shared engine.
                assert result["success"], result
                seventh = await state()
                result = await call(
                    "computer_use_batch",
                    state_revision=seventh["state_revision"],
                    steps=[
                        {"action": "press_key", "key": "a", "modifiers": ["control"]},
                        {
                            "action": "type_text",
                            "text": "Typed through the shared batch",
                        },
                        {"action": "wait", "seconds": 0.1},
                    ],
                )
                assert result["success"], result
                assert result["ui_settle"]["settled"], result
                assert any(
                    n.get("value") == "Typed through the shared batch"
                    for n in result["nodes"]
                )
                print(
                    "PASS: shared batch, keyboard modifiers, Unicode input, UIA settling, attached PNG"
                )
                stale = await call(
                    "computer_press_key",
                    state_revision=seventh["state_revision"],
                    key="enter",
                )
                assert not stale["success"], stale
                print("PASS: stale revision rejection")
                current = await state()
                button_info = next(
                    n for n in current["nodes"] if n.get("title") == "Invoke test"
                )
                bounds = button_info["bounds"]
                window = current["window_bounds_points"]
                x = bounds["x"] + bounds["width"] / 2 - window["x"]
                y = bounds["y"] + bounds["height"] / 2 - window["y"]
                clicked = await call(
                    "computer_click", state_revision=current["state_revision"], x=x, y=y
                )
                assert clicked["success"], clicked
                current = await state()
                assert any(n.get("title") == "Clicks: 2" for n in current["nodes"])
                print(
                    f"PASS: physical-pixel click at monitor placement {args.x},{args.y}"
                )
                edit_info = next(
                    n
                    for n in current["nodes"]
                    if n.get("role") == "Edit" and "UIASetValue" in n["actions"]
                )
                bounds = edit_info["bounds"]
                start_x = bounds["x"] + 10 - window["x"]
                start_y = bounds["y"] + bounds["height"] / 2 - window["y"]
                dragged = await call(
                    "computer_drag",
                    state_revision=current["state_revision"],
                    start_x=start_x,
                    start_y=start_y,
                    end_x=start_x + 80,
                    end_y=start_y,
                )
                assert dragged["success"], dragged
                print("PASS: physical-pixel drag")

                # Cover only our own fixture with a solid-blue fixture. A desktop
                # crop would be blue; HWND capture must retain the editor pixels.
                cover = await asyncio.to_thread(
                    subprocess.Popen,
                    [
                        sys.executable,
                        str(Path(__file__).with_name("windows_native_fixture.py")),
                        "--x",
                        str(args.x),
                        "--y",
                        str(args.y),
                        "--cover",
                    ],
                    stdout=subprocess.PIPE,
                    text=True,
                )
                try:
                    cover_hwnd = json.loads(
                        await asyncio.to_thread(cover.stdout.readline)
                    )["hwnd"]
                    from code_puppy_core_plugins.computer_use import windows_native

                    await asyncio.to_thread(windows_native.foreground, cover_hwnd)
                    await asyncio.sleep(0.2)
                    screenshot = await call("computer_screenshot", app_name=app)
                    assert screenshot["success"], screenshot
                    assert windows_native.user32.GetForegroundWindow() == cover_hwnd
                    from PIL import Image

                    with Image.open(screenshot["path"]) as image:
                        assert image.convert("RGB").getpixel((400, 400)) != (0, 0, 255)
                    print(
                        "PASS: occluded background-window capture; foreground unchanged, no overlay pixels"
                    )
                finally:
                    if cover.poll() is None:
                        cover.terminate()
                        await asyncio.to_thread(cover.wait, timeout=5)
                runtime.run_request(
                    lambda: runtime._backend.policy.set_paused(True),
                    (),
                    {},
                    threading.Event(),
                )
                paused = await call("computer_screenshot", app_name=app)
                assert not paused["success"], paused
                print("PASS: emergency pause prevents further screenshots")
                return result

            asyncio.run(run())
            print("LIVE WINDOWS PARITY SMOKE PASSED")
    finally:
        if fixture.poll() is None:
            # Only this test's own subprocess is terminated; no user applications.
            fixture.terminate()
            fixture.wait(timeout=5)
        runtime._executor.shutdown(wait=True)


if __name__ == "__main__":
    main()
