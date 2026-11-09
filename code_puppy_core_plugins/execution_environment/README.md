# Execution environment

Bundled `load_prompt` provider that tells agents the host OS and the shell used
by the local shell tool. No configuration or core-agent changes are required.
The existing runtime-prompt path includes the fragment for main agents and
subagents, separate from their cacheable authored instructions.

## Shell selection

The provider describes the current core command runner's
`subprocess.Popen(shell=True)` behavior (no executable override):

| Python host | Local execution shell |
| --- | --- |
| Windows | `COMSPEC`, falling back to `SystemRoot/System32/cmd.exe` |
| macOS / Linux / other POSIX | `/bin/sh` |
| Android | `/system/bin/sh` |

The terminal launching Code Puppy is not the execution shell. For example,
PowerShell or Git Bash launching Windows Python still normally uses cmd.exe;
WSL running Linux Python uses /bin/sh. The provider does not assume Bash or
GNU utilities on macOS/Linux. It does not select or change the shell, and its
facts do not describe remote hosts or explicitly invoked interpreters.

Facts are computed whenever core assembles runtime instructions, not cached
at plugin import. A new process that resumes history builds new instructions.
Existing core may reuse a built agent and its runtime tail, so this is not a
promise of per-command refresh or support for changing execution backends
mid-process. Saved conversations can still contain old environment details.
If core introduces a configurable shell backend, update this provider to use
that backend's execution contract rather than inferring it from the terminal.

No date/time injection is added. The independent timestamp-heartbeat plugin
continues to stamp occasional tool results.

## Verification

Tests simulate Windows, macOS, Linux, FreeBSD, Cygwin, and Android, exercise
entry-point registration and the unmodified core's additive runtime path, and
check graceful failure. These are simulated platform tests, not native
end-to-end runs on every platform.

The original Windows model exercise using `llama-qwen3.8-27b` showed 1/5 correct
first-choice commands without environment instructions, versus 5/5 with the
same OS/shell guidance. See the replacement PR for verification scope; prompt
context reduces ambiguity but cannot guarantee correct model commands.
