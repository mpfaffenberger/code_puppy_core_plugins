# Foreground activation investigation (2026-10-04)

## Report and reproduction

The interactive acceptance report for initial plugin commit `2bf27fc` recorded
successful early actions, then repeated "Target lost focus" errors while fresh
state was being requested. Background captures still worked. The recorded primary
fixture HWND no longer existed when this investigation began, so it was not reused.

A new diagnostic, `tests/live_windows_focus.py`, created two owned native fixtures
and exercised activation on the same COM worker used by the backend. Repeated runs
reproduced the failure: activating the first window succeeded; activating the
second with Win32 returned false and left the first window foreground.

Observed in the final instrumented run (these HWNDs are historical, not reusable):

| Operation | Observation |
|---|---|
| Raw Win32 request for HWND 108005828 | returned false; HWND 6097114 remained foreground |
| Patched backend requests state for HWND 108005828 | actual UIA fallback callback recorded `[108005828]` |
| Independent foreground verification | HWND 108005828 was foreground |
| State generation | valid new revision, UIA nodes, WGC PNG |
| Additional alternating state requests | all six succeeded |

This proves the original activation path can fail and the tested UIA recovery
works. It does not establish which internal Windows eligibility condition caused
every failure in the original interactive session.

## Fix and safety

- Try the ordinary Win32 request and allow a bounded foreground-confirmation wait.
- If still not foreground, request the target root's advertised UIA SetFocus.
  Call the COM method, not pywinauto's wrapper that may inject input.
- Independently verify the actual foreground HWND. A successful COM return alone
  is insufficient. If activation cannot be confirmed, refuse the operation.
- Recheck consent, pause, cancellation, desktop availability, process identity,
  and policy throughout the activation attempt. Existing pre-input checks remain.
- Report activation refusal separately from losing focus during an action, with
  useful HWND and request-result diagnostics and a manual recovery instruction.
- No synthetic Alt keys, input-queue attachment, changed system settings, elevation,
  or relaxed foreground checks were added. Providers without root focus support
  can still require manual focus.

## Validation

- 204 targeted computer-use tests passed, up from the original 187.
- New cases cover Win32 denial, UIA recovery, misleading success responses,
  unsupported focus, cancellation, policy revocation, and process-identity changes.
- Real two-window diagnosis confirmed the fallback actually ran and recovered.
- The native harness passed on primary `(180,180)`, secondary `(2050,180)`, and
  portrait `(-1300,180)` placements after strengthening its assertions:
  - fractional scroll changes native SCROLLINFO position;
  - pixel drag leaves a nonempty native EDIT selection;
  - the complete occluded capture matches an unfocused, unobscured reference PNG.
- The harness still is not an LLM-driven acceptance session. Remaining entries in
  the user's CHECKLIST.md must be resumed after restarting the development instance.
  Live macOS validation remains outstanding.

The focus diagnostic requires existing consent and never enables it. It prints
exact PNG paths as evidence, closes only its own verified fixture HWNDs, and does
not delete unrelated screenshots. Run from the plugin checkout:

```powershell
.venv\Scripts\python.exe -X utf8 tests\live_windows_focus.py
```
