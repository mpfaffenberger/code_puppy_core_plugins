# Computer Use (macOS and Windows)

One opt-in plugin, the same twelve `computer_*` tools, and the same `/computer-use`
commands on both platforms. macOS uses Accessibility, Quartz, and ScreenCaptureKit;
Windows uses UI Automation, Win32 input, and HWND-targeted Windows Graphics Capture.
No virtual machine, duplicate plugin, or alternate Windows tool API is required.

**Windows installation, examples, limitations, and live tests:** [WINDOWS.md](WINDOWS.md).
**Implementation and verification matrix:** [PARITY.md](PARITY.md).

The macOS setup and platform-specific behavior below remain supported.

## macOS installation

```bash
pip install "code-puppy[computer-use]"
```

On first use, macOS requests permissions for the terminal or application that
runs Code Puppy:

1. **Accessibility** for inspecting and operating controls.
2. **Screen Recording** for screenshots.

Grant these under **System Settings → Privacy & Security**. Restart Code Puppy
after changing Screen Recording access.

Computer Use is off by default: its tools are not exposed to the model until
you explicitly opt in. On macOS, Code Puppy shows a launch reminder with the
enable command. The choice is stored in the normal Code Puppy settings directory:

```text
/computer-use enable
/computer-use disable
/computer-use status
```

You can change this setting at any time. Once enabled, Computer Use runs
without per-application approval prompts or an allowlist.

Use `/computer-use pause` as an emergency stop and `/computer-use resume` only
after confirming it is safe. `/computer-use deny BUNDLE_ID` persists an
exception, and `/computer-use allow BUNDLE_ID` removes that exception. Login,
authorization, Keychain, and System Settings processes are always blocked.

Snapshots, explicit screenshots, and successful batches display a compact
inline preview in Ghostty, Kitty, WezTerm, and iTerm2. The same PNG is attached
to the tool result so multimodal models can inspect it directly. App-targeted
snapshots capture that application's largest standard window even when another
application is in front.

## Agent workflow

1. Call `computer_get_app_state` with an application name.
2. Use its screenshot, pruned focused-window accessibility tree, and
   `state_revision` to choose an action. Prefer an element ID and one of the
   element's advertised actions.
3. Run one mutation, or one guarded batch of at most 20 mutations.
4. Fetch fresh state. A completed batch does this automatically after waiting
   for the accessibility tree to stabilize. Check `ui_settle.settled`: a timeout
   is reported, not a guarantee that animations or network activity finished.

For a batch `perform_action` step, use `action_name` for the advertised AX/UIA
operation because `action` identifies the step itself, for example:
`{"action": "perform_action", "element_id": 7, "action_name": "AXPress"}`.
A failed batch invalidates the revision; the failing action may have partially
executed, so inspect before retrying.

On macOS, before every mutation, the target process is activated and verified as the
frontmost application. This is required because ScreenCaptureKit can capture a
background window, while Quartz sends global clicks and keystrokes to whichever
application is actually frontmost. Use a guarded batch for click-then-type
workflows so focus is retained and the result is verified in one control loop.

On Windows, standalone mutations and batch entry may activate the target. Once a
batch starts, validation only checks foreground ownership: detected focus loss
aborts the batch instead of reactivating the target. This applies during waits,
between actions, and through final state refresh. Already executed steps are not
rolled back. These are checkpoint checks, not continuous focus monitoring or
instant interruption of a blocking native call. See [WINDOWS.md](WINDOWS.md).

Pixel clicks and drags use window-local screenshot pixels. The backend converts
them to global Quartz points using the captured window bounds, so Retina scaling,
negative display origins, and windows on secondary displays remain consistent.
Scrolling accepts `up`, `down`, `left`, or `right` and a fractional page count.
Text selection supports an exact match, optional prefix/suffix disambiguation,
and cursor placement before or after the match.

## Safety

- Prefer accessibility element IDs over pixel coordinates.
- Persisted user consent is required before tools are exposed to the model;
  disabling the feature removes them again and blocks future captures/actions.
- State revisions and element IDs are single-use and expire after 120 seconds.
- Ordinary physical keyboard/pointer input is not an explicit cancellation signal.
  Windows batches additionally abort on focus loss detected at guard checkpoints.
  Use the dedicated emergency-stop mechanism to request a stop deliberately.
- Every capture and mutation re-checks the application policy; the emergency
  stop therefore also interrupts a batch at its next action boundary.
- Password-field values are never returned in snapshots.
- Batches stop at the first failed action, contain at most 20 steps, wait for
  deterministic UI stability, and return updated screenshot/tree state.
- The plugin does not pause for consequential-action confirmations in YOLO
  mode. Use `/computer-use pause` when autonomous interaction should stop.

## Native capture and privacy

Application screenshots use ScreenCaptureKit through the bundled Swift helper.
The helper is compiled locally, cached with a source hash, and returns stable
window metadata: bundle ID, PID, window ID/title, bounds, pixel dimensions, and
backing scale. Screenshot PNGs are temporary local files. This plugin does not
add telemetry, persistent action history, lock-screen automation, or privileged
system interaction.

The first native capture requires Xcode Command Line Tools because the bundled
helper is compiled on the Mac:

```bash
xcode-select --install
```

## Test

```bash
uv run ruff check code_puppy_core_plugins/computer_use
uv run pytest tests/test_computer_use_*.py -q --no-cov
```

For a live Ghostty smoke test, launch Code Puppy from Ghostty and ask for
`computer_get_app_state`. A compact image should appear inline; the tool result
still carries the PNG if the terminal protocol is unavailable.
