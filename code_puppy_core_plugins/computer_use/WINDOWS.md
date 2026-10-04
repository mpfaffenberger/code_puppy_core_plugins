# Windows Computer Use

This is the Windows backend of the existing `computer_use` plugin, not a second
plugin. It exposes the same twelve tools and `/computer-use` commands as macOS.
The earlier standalone `windows_desktop` prototype is not needed.

## Requirements

- Windows 10 (1903 or later) or Windows 11 with an unlocked interactive desktop
  and Windows Graphics Capture support. Native wheels must be available for your
  Python/architecture; the live tests used CPython 3.12 on Windows x64.
- The `computer-use` extra: pywinauto 0.6.9, windows-capture 2.x, and their dependencies.
  pywinauto supplies pywin32/comtypes; Pillow encodes screenshots.
- A vision-capable model for screenshots/pixel actions.
- Standard privileges. Do not elevate Code Puppy to bypass application protections.

## Install this source change safely

The implementation now lives in the upstream **code_puppy_core_plugins** package.
Do **not** copy this directory into `~/.code_puppy/plugins`: that would register
another copy of the existing plugin, potentially duplicating hooks and commands.

For this development checkout, try an isolated environment first (PowerShell):

```powershell
uv run --isolated --no-project --python 3.12 `
  --with "$HOME\github\code-puppy-computer-use[computer-use]" `
  --with "$HOME\github\code-puppy-core-plugins-computer-use[computer-use]" `
  code-puppy
```

This uses both modified source packages without replacing your normal Code Puppy
installation. `--isolated` isolates Python dependencies, **not** your Code Puppy
configuration: the normal user configuration/plugins and policy still apply.
If the prototype is installed, disable it with `/windows-desktop disable` before
using this implementation, and move its directory out of the plugin folder while
Code Puppy is closed. No prototype consent is silently migrated.

After the change is published in coordinated core/plugin releases, normal installs
will use `pip install "code-puppy[computer-use]"`. Until then, installing the
published package alone does **not** include this development change.

Inside the new session:

```text
/computer-use status
/computer-use enable
```

Enable consent yourself, then restart the isolated session to refresh tools.
Use `/computer-use disable` to revoke consent immediately. The setting is stored
in the normal `computer_use_policy.json`; existing macOS policy schema is retained.
Windows writes a protected DACL allowing the current user and LocalSystem, rather
than pretending POSIX chmod bits enforce Windows permissions.

## Workflow and API

1. Call `computer_get_app_state(app_name="notepad.exe")`.
2. Inspect the screenshot and UIA `nodes`. Use only each node's advertised actions.
3. Use the returned `state_revision` for one mutation or a guarded batch.
4. Inspect fresh state after every standalone mutation. Successful batches return it.

`app_name` accepts an exact executable basename (`notepad.exe`), basename without
extension (`notepad`), exact window title, full executable path, or `hwnd:NUMBER`.
An ambiguous match returns matching titles and `hwnd:` selectors instead of guessing
which document to change. State returns an HWND selector as `application` so a title
change does not cause subsequent batch verification to target another window.

All existing tools are present:

- `computer_get_app_state`, `computer_snapshot`, `computer_screenshot`
- `computer_click`, `computer_set_value`, `computer_perform_action`
- `computer_select_text`, `computer_press_key`, `computer_type_text`
- `computer_scroll`, `computer_drag`, `computer_use_batch`

Examples:

```json
{"state_revision":"...","element_id":7,"value":"Updated text"}
```

```json
{"state_revision":"...","element_id":9,"text":"dog","prefix":"blue ","mode":"cursor_after"}
```

```json
{"state_revision":"...","steps":[
  {"action":"perform_action","element_id":7,"action_name":"UIASetFocus"},
  {"action":"press_key","key":"a","modifiers":["control"]},
  {"action":"type_text","text":"Literal Unicode text"}
]}
```

The batch discriminator is `action`; `action_name` specifies the UIA/AX operation
for a `perform_action` step. Use `text`, `cursor_before`, or `cursor_after` for text
selection. Exact ambiguous matches require prefix/suffix context. Selection uses
UIA text ranges/endpoints, including text after supplementary Unicode characters.

`control`/`ctrl`, `shift`, `option`/`alt`, and `command`/`win` are accepted modifiers.
**command means the Windows key**, not Ctrl. Use `control` for Windows shortcuts.
Newline/tab in typed text send Enter/Tab; these can submit a form. The clipboard
is not modified. Secure-attention and lock-screen shortcuts are blocked.

## Multiple displays and DPI

Screenshots and x/y action coordinates use original **window-local physical pixels**.
Native input maps those pixels to global Windows physical coordinates. Portrait
screens, negative monitor origins, and different display scaling are handled via
per-thread DPI awareness and DWM visible-frame bounds. Windows display settings are
never changed. Moving/resizing a window requires fresh state.

The live fixture was tested on the user's two 1920x1080 landscape monitors and the
1440x2560 portrait monitor left of the primary screen, including real pixel clicks
and drags. These tests cover the current configuration, not every GPU/scaling/RDP
combination. Capture rejects mismatched frame geometry rather than guessing.

## Safety, semantics, and limits

- Consent is off by default. `/computer-use pause` stops subsequent operations;
  `/computer-use resume` requires the user to confirm it is safe to continue.
- Holding **Pause/Break** or moving to the primary display's top-left 2x2 pixels
  is an additional Windows emergency stop. It takes effect at the next check.
  A blocking provider/OS call must return before cooperative cancellation can act.
- Deny an executable with `/computer-use deny notepad.exe`; allow removes an
  explicit denial. Hardcoded security-process restrictions cannot be removed.
  Locked/UAC/non-default desktops are rejected. This is not a security sandbox.
- UIA password nodes and protected descendants have names/help/values redacted;
  semantic text operations on them are refused. **Screenshots are not redacted.**
  PNGs/metadata go to the configured model and may be retained in chat/session data.
- App state activates the target before inspecting it, as macOS does. Explicit
  `computer_screenshot(app_name=...)` captures the HWND in the background without
  activating it or including an overlapping window. Minimized/protected/uncapturable
  windows can fail. There is no desktop-crop or PrintWindow fallback.
- Semantic click/value/text operations require the relevant UIA patterns. Custom
  controls and some legacy EDIT providers may not implement them. Unsupported is
  returned honestly, not replaced by guessed typing. Pixel clicks/drags and keyboard
  input remain available where the app permits them.
- Fractional-page scrolling uses UIA ScrollPattern view size and extent. A window
  with no scroll provider returns unsupported; wheel notches are not mislabeled as
  pixels/pages. Horizontal support also depends on the provider.
- The tree examines at most 2,000 nodes and returns 1-500 nodes, with truncation
  metadata. A stability fingerprint is not proof that the user's task completed.
- Revisions are single-use and expire after 120 seconds. Batches stop on failure;
  actions are not transactional and cannot undo already delivered input.
- Screenshots use unique temporary PNG files, like the macOS backend. Explicit
  output paths are never overwritten. WGC startup/frame/cleanup waits are bounded;
  a wedged native driver may retain a daemon cleanup worker until process exit.
- Inline previews use the same best-effort terminal helper as macOS. Ordinary
  Windows Terminal may not support its protocols; the model still receives the PNG.

## Tests

The checkout's `.venv` is a test environment; normal installation is not altered.

```powershell
.venv\Scripts\python.exe -c "import glob,pytest; raise SystemExit(pytest.main([*glob.glob('tests/test_computer_use*.py'),'-q','--no-cov']))"
.venv\Scripts\python.exe tests\live_windows_computer_use.py --x 180 --y 180
.venv\Scripts\python.exe tests\live_windows_computer_use.py --x 2050 --y 180
.venv\Scripts\python.exe tests\live_windows_computer_use.py --x -1300 --y 180
```

Only use coordinates inside your own active monitor layout. The live test creates
native EDIT, password EDIT, RichEdit, button, checkbox, and solid-color occluder
fixtures. It uses temporary consent/screenshots and closes only its own processes.
It does not open or modify existing documents or enable persistent user consent.
