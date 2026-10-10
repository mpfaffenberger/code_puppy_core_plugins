# herdr integration

Makes code-puppy a first-class citizen in
[**herdr**](https://herdr.dev), a terminal workspace manager for coding
agents. When you run several agents at once, herdr's sidebar rolls each
one up to a single glanceable state -- who's **working**, who's
**blocked** waiting on you, and who's **done** -- so you always know
which pane needs attention.

This plugin teaches code-puppy to report that state authoritatively.

## What it does

herdr injects four environment variables into every pane it owns:

| variable            | meaning                                   |
| ------------------- | ----------------------------------------- |
| `HERDR_ENV=1`       | this shell is running inside a herdr pane |
| `HERDR_SOCKET_PATH` | herdr's local control socket              |
| `HERDR_PANE_ID`     | the pane this process owns (e.g. `w1:p1`) |
| `HERDR_TAB_ID`      | the workspace tab (e.g. `w1:t1`)          |

On startup the plugin checks for those. If they're absent it does
**nothing** -- zero overhead, zero output, no behaviour change, no socket,
no worker thread. If they're present it opens a background reporter that
reports code-puppy's state **authoritatively**: herdr never has to infer
it from the screen.

## Launch and prompt another puppy

From a Code Puppy running inside herdr:

```text
/herdr spawn fixer --prompt-file brief.md
/herdr spawn reviewer --direction down --cwd ./project --prompt-file review.md -- --model MODEL
/herdr send fixer Don't forget the tests
/herdr send fixer --file follow-up.txt
/herdr commands
/herdr command fixer /model gpt-5
```

`spawn` creates a sibling split in the caller's tab, preserves the caller's
working directory unless `--cwd` is given, and does not change focus. Wide
panes split right; tall or approximately square panes split down (cell width
must exceed twice the cell height to count as wide). `--direction` overrides
that choice. Names must match `[a-z][a-z0-9_-]{0,31}` and be unique among
live agents. Invalid names, duplicate names, unreadable prompt files, and
invalid options are rejected before splitting.

The child uses the current Python interpreter with safe-path `-P` and the
installed `code_puppy` module,
not a shell alias, cwd-relative console wrapper, or executable from `PATH`.
`-P` excludes the child's cwd from Python's module search path. Caller
interpreter flags are deliberately not inherited; only child arguments
explicitly supplied after `--` are forwarded. Without a prompt the launcher
waits for reported idle. With a prompt it accepts the first Code Puppy report
in any state, names the child and returns without waiting for task completion.
Startup timeout is 30 seconds by default (`--timeout SECONDS`, up to 300).
A failed launch or readiness timeout reports the created pane for inspection;
it deliberately does not close it or retry a possibly delivered prompt.
If a split request itself times out, inspect `herdr pane list` before retrying:
the server may have created the pane without returning its ID.

`--prompt` and `--prompt-file` are mutually exclusive. Files are UTF-8.
Spawn writes the prompt to an exclusive private temporary file (mode 0600
on POSIX). A short, single-line Python bootstrap reads and deletes that file
before passing the complete text as **one initial-command argument** to the
installed module. This avoids shell canonical line-buffer limits and editor
readiness races. Startup failure cleans up the file; a timeout can delete it
before a slow child reads it, causing that child to fail without its prompt.
Inspect the reported pane rather than retrying blindly. A hard caller crash
before the child reads it can leave a private temporary file. The launcher
never retries a possibly delivered initial command. A working child is named
promptly; a short task may already have completed when spawn returns.

**Core preprocessing limitation:** the interactive core parses attachments
before dispatching `/herdr`, removing quotes and normalizing whitespace.
Inline multiword `--prompt` values, quoted paths containing spaces, and quoted
multiword child arguments therefore are not supported through this boundary.
Use whitespace-free paths and `--prompt-file` for briefs. Inline send text
arrives already normalized; it is not byte-preserving, and attachment-like
paths/URLs may be consumed by the core before dispatch. This plugin cannot
recover raw input; a raw-command dispatch seam requires an upstream change.

`/herdr send` remains a **prompt-only helper**, not a way to run Code Puppy
commands such as `/model gpt-5` or `/new`. Use the separate explicit intent
`/herdr command fixer /model gpt-5` when intentionally requesting a command.
Commands may mutate state or open interactive menus: recognition is not a
safety guarantee. `/herdr commands` dynamically lists **sender candidates**
from the existing core registry (including aliases) and `custom_command_help`
plugin discovery, including advertised namespaced commands. Enumeration runs
plugin help callbacks; those callbacks are not guaranteed side-effect-free.
No handlers or `custom_command` callbacks are executed as validation probes.
Unknown or unadvertised commands fail closed. The target may run a different
version, configuration, or plugin set: this is not target validation.

The command payload must have an exact leading `/command` token, be one line
under 1,000 UTF-8 bytes, and contain no terminal controls. Shell `!` and bare
exit/quit/clear passthrough are not supported. Arguments can contain slash
paths; they are not blanket-rejected. The payload received by the launcher
is forwarded without rewriting, but both caller and child core preprocessing
may remove quotes, normalize spaces, or consume attachments. Inline quoted
or escaped leading commands may already be normalized before this plugin
sees them; do not rely on byte-preserved arguments through this boundary.
There is no command-file mode or automatic retry.

Command uses the same unique idle/terminal identity checks and `pane run`
terminal typing as send, not a structured child command route. Verify actual
foreground Code Puppy editor readiness before using it. A write acknowledgement
is **not confirmed execution**, child receipt, or completion. The occupant
check and write are not atomic; reported idle alone is not editor readiness.

The bundled [`herdr-code-puppy` skill](SKILL.md) is the canonical agent
operational playbook. Its `register_skills` callback checks `HERDR_ENV=1`
and a present `HERDR_PANE_ID` each time discovery runs; outside that context
it is absent, not merely disabled. The `load_prompt` hook tells agents to
activate it before controlling siblings and retains essential safety rules
even if skills are disabled. `/herdr help` and the command menu expose the
launcher to users. This README retains the technical transport constraints
and examples for maintainers.
Agents with shell tools can call the installed plugin's `launcher.execute`
API and print the result, using the same guards rather than raw input.
Its guards do **not** change Herdr's generic CLI transport. The existing
`herdr pane send-text <pane-id> ...` followed by
`herdr pane send-keys <pane-id> Enter` remains available. Verify the intended
pane and that its Code Puppy input editor is actually ready first: a named
agent's idle report alone does not establish editor readiness. Raw terminal
input can execute commands in whatever process owns the foreground; it is
not a safe arbitrary-text alternative to this helper.

The launcher forwards the send tail it receives without further shell parsing.
`--file PATH` is the explicit file form; it bypasses the caller's inline-text
normalization, but the child still applies its usual prompt preprocessing.
File sends remove one conventional trailing newline; other newlines are
rejected.
Send accepts **single-line** prompts only: multiline paste is unsafe in the
classic editor or before bracketed paste is enabled.
Use `spawn --prompt-file` for multiline work. Send rechecks a unique idle Code
Puppy's terminal identity and refuses working/blocked agents. It uses herdr's
atomic `pane.send_input` text+Enter through `pane run`. Empty prompts and
terminal controls are rejected. Send rejects bare `exit`, `quit`, and `clear`
(case-insensitive), and tokens beginning with `/` or `!` even inside quotes.
This conservative token guard also rejects prose containing absolute paths;
use prompted spawn for such briefs. It avoids reading attachments merely to
predict whether the child would expose a command. Spawn accepts leading `/`
as literal initial prompt text (so an initial `/model` or `/new` prompt does
not execute that editor command), but rejects leading `!` because the core
initial-command path supports shell passthrough. Success confirms submission,
not completion or byte-identical model/history text.

**Startup caveat:** a prompt sent immediately after a no-prompt spawn may be
dropped. The startup idle report precedes input-editor readiness; the editor
can flush queued terminal input when it starts. Neither `send` nor `command`
can distinguish this gap from an input-ready idle pane. For the child's first
task, prefer `spawn --prompt` or `spawn --prompt-file`, whose private-file
handoff does not depend on editor readiness. A successful send acknowledges
the pane write, not child receipt.

Spawn prompts are capped at 24,000 UTF-8 bytes. Single-line sends must stay
under 1,000 UTF-8 bytes because the classic editor's canonical line buffer can
otherwise discard input or Enter even after startup. Use `spawn --prompt-file`
for larger briefs. The actual typed launch command (bootstrap, path and quoted
child arguments) must remain under 1,000 UTF-8 bytes and contain no newline;
otherwise it fails before any split. This conservative cap avoids even an
unready macOS shell's line buffer.
The command blocks the caller while waiting, prints the new pane and timeout
as progress, and may not be interruptible under the core's Ctrl+C guard.
Use a short `--timeout` when appropriate. Only one pane-specific `agent get`
is polled per tick. A reported child that disappears fails promptly; a child
that exits before ever reporting still requires the bounded timeout.

The preflight uniqueness check cannot reserve a name: herdr only permits
renaming a running agent. Concurrent same-name spawns can both create panes;
the server rejects the losing rename, and the launcher reports that pane.
Use `herdr agent get/read/wait` to observe work.

Outside herdr, `/herdr` explains that it requires `HERDR_ENV=1` and performs
no file reads, socket requests, or subprocess launches. The CLI binary comes
from `HERDR_BIN_PATH` when available, otherwise `herdr` in `PATH`. On Windows,
launch commands assume herdr's default PowerShell shell.

### Why not native `herdr agent prompt`?

In herdr 0.9.1, custom lifecycle reports can display `codepuppy idle`, but
native prompting requires a hard-coded known agent kind. Reporting session
identity does not remove that gate. `agent start --kind` and `integration
install` likewise have fixed supported-kind lists. An upstream Code Puppy
kind/integration is a follow-up; no agent impersonation or parallel reporter
is needed here. An agent-callable launcher tool is also deferred.

## State is authoritative

State is a pure function of two facts the plugin observes directly:

```text
blocked   if awaiting the human
working   elif a run is in flight (run_depth > 0)
idle      otherwise
```

* **run depth** comes from `agent_run_start` / `agent_run_end`, refcounted
  so a finishing sub-agent doesn't flip the pane `idle` mid-turn (the same
  pattern the `puppy_spinner` plugin uses).
* **awaiting** comes from the `awaiting_user_input` callback, which fires
  from the single process-wide choke-point
  (`command_runner.set_awaiting_user_input`) that *every* interactive wait
  already passes through -- shell-command approval, file-permission
  approval, `ask_user_question`, and every menu/picker. One hook captures
  every block, so there is nothing left for herdr to guess.

User-initiated menus carry `notify=False`; the plugin suppresses their
`blocked` report entirely so quick pickers don't spam attention.

## Activity text is best-effort

Alongside the authoritative state, the plugin attaches a short activity
`message` (`thinking`, `running <tool>`, `awaiting input`) driven by
`pre_tool_call` / `post_tool_call`. This is decorative: it rides a separate
lane, deduplicates on the `(state, message)` pair, and can **never** delay
or override the authoritative state, the session reference, or the final
release. If a hook-blocked tool misses its completion callback, the next
run / tool / wait / turn event corrects the message; state stays correct
regardless.

## Pane metadata (model / context / tokens)

At the end of every interactive turn the plugin also reports pane
metadata via `pane.report_metadata`: the current model, context-window
fill percentage, and a compact token count. herdr stores these under the
pane with a 24h TTL (so stale numbers self-clear after an abrupt exit),
but it only *renders* them when your sidebar layout references the
matching `$name` fields. Add them to `rows_by_agent` in your herdr
config:

```toml
[ui.sidebar.agents.rows_by_agent]
codepuppy = [
  ["state_icon", "workspace", "tab"],
  ["agent", "$model"],
  ["$context", "$tokens"],
]
```

The payload uses static keys (`model`, `context`, `tokens`) with
string values and no indicator glyphs -- herdr rows already carry their
own state icons. If context usage can't be computed for a turn, no
metadata is sent and the pane keeps its last good values until the TTL
expires.

## Session reference

On each prompt the plugin reports a **stable** session reference (the
durable autosave name and pickle path) via `pane.report_agent_session` --
not the per-run `group_id`, which changes every turn. It re-reports only
when the reference actually changes (after `/clear`, `/session new`,
`/autosave_load`, `/load_context`, a quick resume, or an agent switch).

## Auto-resume on launch

A herdr server restart kills pane processes, and herdr only *restores*
panes whose agent reported a native session reference through an **official**
herdr integration. code-puppy is not one of those today, so herdr stores
nothing for us -- verified against herdr 0.9.1: `pane.report_agent_session`
is accepted, but `pane.get` exposes no `agent_session` for the `codepuppy`
agent (the identical call for `codex` does).

So the plugin is self-sufficient. Whenever it reports a session reference it
also records a tiny **pane -> session map** (`herdr_pane_sessions.json`) under
`XDG_STATE_HOME` -- never inside the plugin dir, which would self-tamper the
trust hash. On startup inside a pane it resolves the previous session and
loads it into the agent, exactly like `-r`:

1. herdr's own stored reference via `pane.get` (forward-compatible: the day
   herdr recognises codepuppy, or grows a custom-integration hook, this wins);
2. else the local pane map.

An explicit `-r` / `--quick-resume` always wins (the plugin captures the
intent from the `handle_cli_args` hook), and headless `-p` runs never
auto-resume. Set `HERDR_NO_AUTO_RESUME=1` for a deliberately fresh session
in a reused pane. Everything is fail-soft: a missing socket, a vanished
session file, or an unreadable map simply yields a fresh session.

Net effect: restart herdr, start code-puppy in the restored pane, and your
conversation is already back on screen.

> This ships in the `code-puppy-core-plugins` bundle. Most people run code-puppy
> through `uvx`, so nothing to install locally: the change reaches them on the
> next PyPI release. Because `code-puppy` depends on the bundle by range
> (`>=0.0.58`), a fresh resolve picks the new version up automatically -- but
> `uvx` reuses a warm cache, so a user may need `uvx --refresh code-puppy`
> (or `uvx -U code-puppy`) once.

## Conversation titles

The `session_namer` plugin auto-names every conversation (the titles you
see in `/resume`). This plugin propagates the current session's title to
two herdr surfaces:

* the pane's **presentation title**, which herdr renders as the pane
  border label and the sidebar `pane` token, and
* the **workspace tab label** in herdr's tab bar -- but only while the
  tab holds a single pane (the one running this code-puppy). The tab bar
  is a user-managed surface, so a tab shared with other panes is never
  touched.

Add the `pane` token to `rows_by_agent` to show the title in the sidebar
too:

```toml
[ui.sidebar.agents.rows_by_agent]
codepuppy = [
  ["state_icon", "workspace", "tab"],
  ["agent", "$model", "pane"],
  ["$context", "$tokens"],
]
```

The title is read from the session metadata sidecar that `session_namer`
maintains (the same store `/resume` reads). The pane presentation title
rides the same `pane.report_metadata` envelope as the token payload:
herdr replaces the whole per-source metadata entry on each report, so
every turn-end report re-sends the title (or a `clear_title` on a
set-to-none transition) and a title change is reported title-only the
moment it is seen. The tab label follows the same title changes via
`tab.rename`, guarded by a `tab.get` read of the tab's `pane_count`; on
a clean exit the original label is restored -- but only while the tab
still shows the label we last set, so a tab you renamed by hand is never
clobbered (a crashed exit simply leaves the last title in place, like
the stale-pane case above).

Because the namer names the session *asynchronously* after each autosave,
a bounded background wait (2s ticks, 90s ceiling -- the namer's own model
call is capped at 60s) picks the title up a few seconds after the turn
ends instead of waiting for the next turn. The wait is single-flight,
pinned to the session that triggered it, and skipped when the
`session_namer` plugin is disabled via its `session_namer` config value.
A session switch clears the previous session's title on the next prompt
(and restores the tab label). As with the token payload, the pane title
self-expires after the 24h metadata TTL, and all of this is decorative:
it never touches the authoritative state and is a no-op outside herdr.

## Notifications are herdr's job

The plugin sends no notifications itself. herdr derives attention and
completion notifications from the agent-state transitions the plugin
reports, and herdr's own toast / sound settings control delivery. This
plugin's only job is to report accurate state.

## Release on exit

On `session_end` / `shutdown` the plugin restores the workspace tab label
(if it relabelled one) and then calls `pane.release_agent` once
(idempotent, bounded) so herdr knows code-puppy has let go of the pane --
no lingering stale `working`. There is no intermediate `idle` report on
shutdown; if herdr is unavailable the release is bounded and can never
delay process exit.

## No install needed on the herdr side

Because this plugin ships with code-puppy and self-activates inside a
pane, there is nothing to run -- `herdr integration install` is **not**
required for code-puppy. Just start code-puppy inside herdr:

```bash
herdr           # start / attach herdr
code-puppy      # (or: pup) -- herdr picks up its state automatically
```

herdr also recognises the `code-puppy` / `pup` process on its own, so even
with this plugin disabled you still get basic detection. The plugin
upgrades that from screen-scraped guessing to authoritative, event-driven
state, metadata, and activity.

## Design notes

* **Never disturbs the agent.** All socket I/O happens on a daemon
  worker thread; the sync file-permission and tool hot-paths just enqueue
  and return. The tool observers always return `None`, so they can never
  block or transform a tool call.
* **Critical vs decorative.** State edges, session references, and the
  release ride a critical lane that always overtakes decorative traffic
  (activity messages, metadata). Decorative saturation can never displace
  a critical report.
* **Edge-triggered + deduped.** Only genuine `(state, message)` changes
  hit the socket.
* **Fail-soft.** A missing/closed socket, a departed herdr, an
  unresolvable source -- all are swallowed to the debug log. Reporting
  your state is never worth crashing your agent over.

## Files

| file                    | responsibility                              |
| ----------------------- | ------------------------------------------- |
| `client.py`             | herdr socket transport (JSON, worker thread)|
| `reporter.py`           | event -> state machine (refcount + dedup)   |
| `sources.py`            | fail-soft adapters (tokens / title / session / msg) |
| `restore.py`            | pane -> session map + auto-resume on startup |
| `register_callbacks.py` | callback wiring + env activation guard       |
| `smoke.py`              | manual live smoke test (disposable pane + tab) |

## Live smoke test

The unit tests (`tests/plugins/test_herdr_*.py`) are the CI contract. For
an end-to-end check against a real herdr server, run the manual smoke test
from inside a herdr pane:

```bash
python -m code_puppy_core_plugins.herdr.smoke
```

It creates its **own** disposable pane, drives the real client through
every protocol-16 method (state edges, session reference, metadata,
conversation title, release), reads each result back with
`herdr pane get`, then closes the disposable pane -- your working pane is
never touched. Exits non-zero if any check fails.
