# Cross-platform implementation and verification

## Architecture

`backend.py` selects macOS or Windows behind the existing twelve tool names.
`macos_backend.py` retains the original macOS implementation. `windows_backend.py`
implements the same dispatcher methods with UIA, WGC, and typed Win32 input.
Commands, policy, revision storage, terminal previews, tool-result images, batch
execution, and UI-settling logic are shared. Windows UIA objects and entire requests
stay on a single COM worker, never arbitrary event-loop/thread-pool threads.

The plugin keeps its existing package/entry-point name `computer_use`. Neither
platform needs a `windows_desktop` plugin or alternate tool schemas. The main
Code Puppy repository change adds Windows dependencies to its existing extra;
the companion core-plugins repository contains the implementation.

## Capability matrix

| Existing capability | Windows implementation | Verification |
|---|---|---|
| Twelve tools / opt-in registration | Same registrars and Pydantic-AI schemas | Automated, both platform gates |
| Revisioned application state | HWND identity, UIA tree, WGC screenshot | Live native fixture |
| Accessibility node IDs | Bounded UIA control-view traversal | Live + bounds/truncation tests |
| Element click | Invoke / SelectionItem / Toggle patterns | Live button/checkbox; mock pattern cases |
| Set control value | Value / RangeValue, read-only and password checks | Live EDIT + mock cases |
| Advertised control actions | Invoke, toggle, selection, expand/collapse, focus | Live invoke/focus; remaining mocked |
| Exact contextual text selection | TextPattern ranges, prefix/suffix, caret endpoints | Live RichEdit selection after supplementary Unicode; caret/suffix mocked |
| Keyboard and Unicode typing | Balanced SendInput events; literal text | Live shared batch |
| Fractional-page scrolling | UIA ScrollPattern view size/extent | Live vertical half-page; horizontal mocked |
| Pixel click and drag | DWM bounds, per-thread DPI awareness, SendInput | Live on all three monitors |
| Background window capture | WGC by HWND, no activation or desktop crop | Live with an opaque blue occluder |
| Guarded batches / UI settling | Shared batch engine and accessibility fingerprint | Live + failure/wait/cancellation tests |
| PNG attachment to model | Shared BinaryContent/ToolReturn | Live tools + schema tests |
| Inline terminal preview | Existing shared terminal helper | Existing mocked terminal tests; not live on Windows Terminal |
| Consent, pause, process restrictions | Shared policy plus Windows security process rules | Automated; live temporary-policy pause |
| Password-value redaction | UIA protected nodes/descendants, no semantic password operations | Live password EDIT + mock descendant cases |

## Results for this development change

- 220 targeted `test_computer_use*.py` tests passed on Windows after the
  [foreground activation fix](FOCUS_INVESTIGATION.md), coordinate-metadata
  correction, and checkpoint-based batch focus-loss fix (initial baseline: 187;
  foreground-fix baseline: 204; coordinate-fix baseline: 209).
- The batch focus-loss fix is verified by mocked regressions, including the
  serialized Windows runtime, and a subsequent focused public-session retest on
  `219210a`. The owned sink took focus, the batch stopped, and the sentinel stayed
  at zero. Cleanup and final owner pause were verified. The original failure is
  retained in the acceptance evidence; earlier live results below are prior
  evidence, not complete campaign reruns on this change.
- The existing macOS backend's 21 original methods were compared by Python AST
  against the upstream baseline: unchanged. A small `invalidate_state` method was
  added for shared failed-batch cleanup. Imports/formatting and module location changed.
- Existing macOS action, activation, accessibility, policy, image, and settling
  regressions passed with their mocked native APIs.
- The disposable live fixture passed at `(180,180)`, `(2050,180)`, and `(-1300,180)`:
  primary landscape, secondary landscape, and left-hand portrait monitor.
- Both the modified Code Puppy and core-plugins packages built as wheels.
- No tests enabled persistent user consent; no existing application documents
  were used. The follow-up focus diagnostic honors already granted consent.

## What this does not prove

This is implementation of the shared capability set, **not a claim that every
Windows application exposes the same accessibility features as every Mac app**.
A control without UIA Value/Text/Scroll patterns returns unsupported. In particular,
Windows semantic scrolling currently needs ScrollPattern, while the macOS backend
can inject pixel scroll events. The model can use supported keyboard/pixel actions
where appropriate, but the plugin does not silently pretend those are semantic
operations. Full application-by-application equivalence is not established.

Live tests covered native Win32 controls and the current three-monitor hardware.
Electron/Chromium apps, mixed-DPI configurations different from this machine,
RDP, ARM64, other Python versions, GPU/driver variations, and real macOS native
execution still need environment-specific validation. WGC rejects minimized,
protected, or mismatched-geometry captures rather than falling back to the desktop.

No live macOS host was available. Shared batch execution now runs off the event
loop and invalidates failed batches; this is tested with mocks but should receive
an actual Mac smoke test before publishing a coordinated release. An LLM-driven
chat session after installation/consent also remains a separate end-to-end check.
