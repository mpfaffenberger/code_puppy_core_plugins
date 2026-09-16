# Browser Harness

This opt-in plugin lets Code Puppy drive **your real browser** — your logins,
cookies, extensions, and tabs — through
[browser-harness](https://github.com/browser-use/browser-harness), a small CDP
harness that keeps one tab attached across calls. It is a thin, honest wrapper:
Code Puppy shells out to the `browser-harness` CLI, which owns the browser
connection.

## Two installs, on purpose

1. The plugin itself ships with `code-puppy-core-plugins`; nothing to do.
2. The harness is a separate tool, because it owns its own daemon and upgrades:

```bash
uv tool install --python 3.12 --upgrade --force browser-harness
```

`/browser status` tells you which of the two is missing.

## Consent and configuration

Browser control is off by default; its tools are not exposed to the model until
you opt in, exactly like macOS Computer Use.

```text
/browser status                 # install state, consent, endpoints, browsers
/browser enable | disable       # persisted one-time consent
/browser doctor                 # harness health, with the exact fix
/browser connect <devtools-url> # pin http://127.0.0.1:9222 or a wss:// URL
/browser disconnect             # back to auto-discovery
/browser install                # commands to install a drivable browser
/browser recordings [on|off]    # the harness's own local trace recording
```

Disabling removes the tools again and blocks further calls. `/browser enable`
merges the tools into the running session, so no restart is needed.

## Tools

- `browser_harness(script, browser_name=None, timeout=120)` — run Python with
  the harness helpers (`new_tab`, `js`, `click_at_xy`, `cdp`, `wait_for_load`,
  …) pre-imported. stdout is the only channel back.
- `browser_screenshot(full=False, max_dim=1568)` — capture the attached tab. It
  renders inline in Ghostty, Kitty, WezTerm, and iTerm2, and the same PNG rides
  along on the tool result for multimodal models.
- `browser_doctor()` — connection health with the fix that clears it.

The bundled `SKILL.md` teaches the workflow: one tab per task, accessibility
tree before pixels, verify each action, stop for passwords and purchases.

## Which browsers work

browser-harness speaks the Chrome DevTools Protocol, so it needs a
Chromium-family browser: **Chrome, Chrome Canary, Chromium, Brave, Edge, Arc,
and Helium** are detected and drivable.

**Firefox and Safari cannot be driven.** Firefox implements WebDriver BiDi and
Safari implements the Apple WebKit inspector protocol; neither exposes the CDP
endpoint the harness requires. `/browser status` lists them as *present but not
drivable* rather than pretending they are usable. Options:

- install a Chromium-family browser (`/browser install`), or
- point at any CDP endpoint elsewhere: `/browser connect http://host:9222` for a
  Chromium started with `--remote-debugging-port=9222`, or a hosted browser.

macOS shows a per-connection "Allow remote debugging?" sheet, and Chrome's
`chrome://inspect/#remote-debugging` toggle must be ticked once. `browser-harness
mac-approve` clears the sheet; `browser_doctor()` tells you when either is the
blocker.

## Privacy

Nothing here phones home beyond browser-harness's own optional telemetry
(`browser-harness telemetry disable`). Code Puppy stores only two settings — the
consent flag and an optional endpoint — in its config directory. Page content,
screenshots, and any recordings stay on your machine under the harness's state
directory.

## Test

```bash
uv run pytest tests/test_browser_harness_*.py -q --no-cov
```
