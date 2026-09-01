---
name: browser-harness
description: Use for any web task that needs the user's real browser - logged-in
  sessions, their cookies, clicks, uploads, downloads, forms, or local web apps.
  Public read-only pages need no browser.
version: "1.0"
author: code-puppy
tags:
  - web
  - browser-automation
  - cdp
---

# Browser Harness

Drive Mike's actual browser through `browser-harness`, a CDP harness that keeps
one tab attached across calls. Adapted from
[browser-use/browser-harness](https://github.com/browser-use/browser-harness)
(MIT); the upstream `SKILL.md` (`browser-harness skill`) is the canonical,
version-matched reference and goes deeper than this file.

## Pick the right browser tool

- Plain HTTP gets it (public page, API, docs): use `curl`/`web_fetch`. Do not
  open a tab.
- Sandboxed scraping, crawling, or parallel extraction: delegate to the
  `web-retriever` agent, which drives a throwaway Playwright browser.
- Anything needing *this* browser - existing logins, cookies, local
  `localhost` apps, extensions, uploads, downloads, or "watch me do it":
  use the `browser_harness` tool.

## Tools

- `browser_harness(script)` - run Python with the harness helpers pre-imported.
  stdout is the only channel back, so `print()` what you need.
- `browser_screenshot()` - capture the attached tab; it renders inline for the
  user. Pixels never tell you what is clickable, so confirm with `page_info()`
  or `js()`.
- `browser_doctor()` - install/daemon/browser health, with the exact fix.

## Workflow

1. First navigation of a task: `new_tab(url)`. The attached tab survives across
   calls, so do **not** call `new_tab()` again in every script. Check
   `current_tab()` / `list_tabs()` and `switch_tab()` before opening duplicates;
   never close a tab you did not create.
2. After navigation call `wait_for_load()`; after a click that triggers a
   request, `wait_for_network_idle()`.
3. Find elements in the accessibility tree, not pixels:
   `cdp("Accessibility.getFullAXTree")["nodes"]` carries role, name, and
   `backendDOMNodeId`. Filter it in Python - it is thousands of nodes. Then box
   center -> `click_at_xy(x, y)` -> verify with a targeted `js()` or
   `page_info()` check.
4. Fall back to `js(...)` for DOM/extract work, and screenshots only when
   layout or imagery is the question.
5. An action that does nothing usually means the attached tab is hidden: call
   `activate_tab(current_tab())`, retry the same action once, then re-check.
   This visibly switches tabs, so skip it if the user asked you not to touch
   their foreground.
6. Write the reusable part into `$BH_AGENT_WORKSPACE/agent_helpers.py` when a
   site-specific trick took real discovery; keep task code in the tool call.

## Which browser

The harness speaks CDP, so Chrome, Chromium, Brave, Edge, Arc, or Helium work.
**Firefox and Safari cannot** - they expose no CDP endpoint. If asked for
Firefox, say so and offer a Chromium-family browser or an explicit endpoint.

- `/browser status` lists what is installed, running, and reachable.
- `/browser connect http://127.0.0.1:9222` (or a `wss://` URL) pins a specific
  endpoint; leave it unset to auto-discover the running Chromium browser.
- Cloud browsers: `browser_harness(script='start_remote_daemon("name")')`, then
  pass `browser_name="name"` on every later call. Ask before leaving one
  running, and stop it with `stop_remote_daemon("name")`.

## Ask first

Stop and ask before typing passwords, solving MFA, approving a payment or
purchase, deleting an account, sending a message, or anything that posts
content as the user. Being already signed in is not consent to act.

## When it will not connect

`browser_doctor()` (or `/browser doctor`) names the fix. The usual three:
Chrome's `chrome://inspect/#remote-debugging` toggle is off; macOS is waiting on
the "Allow remote debugging?" sheet (`browser-harness mac-approve`); or no
Chromium browser is running at all.
