"""Launch and prompt sibling Code Puppy panes through herdr's portable CLI.

``pane run`` uses ``pane.send_input``: bracketed text and Enter in one
ordered write. Mutations are never retried: a timeout may mean they applied.
"""

import argparse
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from .launch_command import BOOTSTRAP, prompt_file

_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")


class LauncherError(ValueError):
    """An actionable launcher failure with an optional herdr error code."""

    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


def _windows():
    return os.name == "nt"


class ControlClient:
    """Bounded request/response adapter; herdr owns platform socket handling."""

    def __init__(self):
        self.binary = os.environ.get("HERDR_BIN_PATH") or "herdr"

    def call(self, *args):
        try:
            reply = subprocess.run(
                [self.binary, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LauncherError(f"herdr request failed: {exc}") from exc
        # pane run uses herdr's send_ok_request, which emits no JSON on success.
        if reply.returncode == 0 and not reply.stdout and args[:2] == ("pane", "run"):
            return {}
        try:
            body = json.loads(reply.stderr if reply.returncode else reply.stdout)
        except ValueError as exc:
            raise LauncherError("herdr returned an invalid response") from exc
        if reply.returncode or "error" in body:
            raise LauncherError(
                body.get("error", {}).get("message", "herdr request failed"),
                code=body.get("error", {}).get("code"),
            )
        result = body.get("result")
        if not isinstance(result, dict):
            raise LauncherError("herdr returned no result")
        return result


def _inside():
    if os.environ.get("HERDR_ENV") != "1":
        raise LauncherError("This command only works inside herdr (HERDR_ENV=1).")
    if not os.environ.get("HERDR_PANE_ID"):
        raise LauncherError("herdr did not provide the caller pane ID.")


def _validate_name(name):
    if not _NAME.fullmatch(name):
        raise LauncherError("Invalid name: use [a-z][a-z0-9_-]{0,31}.")


def _text(text):
    # ESC can terminate bracketed paste, and other controls can act as keys.
    text = text.replace("\r\n", "\n")
    if not text.strip() or any(
        ord(c) < 32 and c not in "\n\t" or 127 <= ord(c) <= 159 for c in text
    ):
        raise LauncherError("Prompt must be nonempty text without terminal controls.")
    return text


def launch_argv(args):
    """Use this interpreter's installed module, not cwd-sensitive wrappers.

    -P excludes the child cwd from Python's module search path. Interpreter
    flags and application flags from the caller are deliberately not inherited.
    """
    return [sys.executable, "-P", "-m", "code_puppy", *args]


def _shell_command(argv):
    if any(
        any((ord(c) < 32 and c not in "\n\t") or ord(c) == 127 for c in arg)
        for arg in argv
    ):
        raise LauncherError("Launch arguments cannot contain terminal controls.")
    if _windows():
        # Herdr's default Windows shell is PowerShell. Do not use cmd.exe's
        # list2cmdline escaping for a PowerShell command.
        def quote(arg):
            for char in "'\u2018\u2019\u201a\u201b":
                arg = arg.replace(char, char * 2)
            return "'" + arg + "'"

        return "& " + " ".join(quote(arg) for arg in argv)
    return shlex.join(argv)


def _ready(agent):
    return agent.get("agent") == "codepuppy" and agent.get("agent_status") in (
        "idle",
        "done",
    )


def _get(client, pane):
    return client.call("agent", "get", pane)["agent"]


def _submit(client, agent, prompt):
    pane = agent["pane_id"]
    current = _get(client, pane)
    if not _ready(current) or current.get("terminal_id") != agent.get("terminal_id"):
        raise LauncherError(
            f"Pane {pane} is not the same idle Code Puppy; nothing sent."
        )
    client.call("pane", "run", pane, _text(prompt))


def spawn(client, name, *, direction=None, cwd=None, prompt=None, args=(), timeout=30):
    _inside()
    _validate_name(name)
    if direction not in (None, "right", "down"):
        raise LauncherError("Direction must be right or down.")
    if not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise LauncherError(
            "Timeout must be greater than zero and at most 300 seconds."
        )
    if prompt is not None:
        prompt = _text(prompt)
    directory = Path(cwd or os.getcwd()).expanduser().resolve()
    if not directory.is_dir():
        raise LauncherError(f"Not a directory: {directory}")
    if prompt is not None and prompt.lstrip().startswith("!"):
        raise LauncherError("Initial commands cannot use shell passthrough (!).")
    if prompt is not None and len(prompt.encode("utf-8")) > 24000:
        raise LauncherError("Prompt is too large (24,000-byte limit).")
    agents = client.call("agent", "list")["agents"]
    if any(agent.get("name") == name for agent in agents):
        raise LauncherError(f"Agent name {name} is already in use.")
    caller = os.environ["HERDR_PANE_ID"]
    if direction is None:
        layout = client.call("pane", "layout", "--pane", caller)["layout"]
        caller_info = next((p for p in layout["panes"] if p["pane_id"] == caller), None)
        if caller_info is None:
            raise LauncherError(f"Cannot find caller pane {caller} in herdr layout.")
        rect = caller_info["rect"]
        direction = "right" if rect["width"] > 2 * rect["height"] else "down"
    handoff = prompt_file(prompt) if prompt is not None else None
    try:
        argv = launch_argv(args)
        if handoff is not None:
            argv = [argv[0], "-P", "-c", BOOTSTRAP, str(handoff), *args]
        command = _shell_command(argv)
        if len(command.encode("utf-8")) >= 1000 or "\n" in command:
            raise LauncherError(
                "Launch command is too large or multiline (under 1,000 bytes required)."
            )
        return _launch(
            client, name, caller, direction, directory, command, prompt, timeout
        )
    except BaseException:
        if handoff is not None:
            handoff.unlink(missing_ok=True)
        raise


def _launch(client, name, caller, direction, directory, command, prompt, timeout):
    created = client.call(
        "pane",
        "split",
        "--pane",
        caller,
        "--direction",
        direction,
        "--cwd",
        str(directory),
        "--no-focus",
    )
    pane = created["pane"]["pane_id"]
    try:
        client.call("pane", "run", pane, command)
        print(
            f"herdr: waiting for {pane} to report Code Puppy (up to {timeout:g}s)...",
            flush=True,
        )
        deadline = time.monotonic() + timeout
        seen = False
        while time.monotonic() < deadline:
            try:
                agent = _get(client, pane)
            except LauncherError as exc:
                if exc.code != "agent_not_found":
                    raise
                if seen:
                    raise LauncherError(
                        "Child agent disappeared before readiness"
                    ) from exc
                time.sleep(0.25)
                continue
            seen = True
            if (prompt is not None and agent.get("agent") == "codepuppy") or _ready(
                agent
            ):
                break
            time.sleep(0.25)
        else:
            raise LauncherError(
                f"Timed out waiting {timeout:g}s for reported Code Puppy"
            )
        # Rename only once an occupant exists. The server enforces uniqueness
        # again, catching a concurrent launch that won the name in the meantime.
        client.call("agent", "rename", pane, name)
        # Initial prompt is already one argv element; never inject into an
        # editor that may not exist yet or have bracketed paste enabled.
    except Exception as exc:
        raise LauncherError(
            f"Pane {pane} created for {name}: {exc}. Inspect it; it was not closed. Do not blindly retry a prompt."
        ) from exc
    return f"Started {name} in {pane}" + (
        "; prompt delivered as initial command." if prompt is not None else "; idle."
    )


def send(client, name, prompt):
    _inside()
    _validate_name(name)
    prompt = _text(prompt)
    if prompt.lstrip().startswith(("/", "!")):
        raise LauncherError("Send cannot execute slash commands or shell passthrough.")
    if "\n" in prompt:
        raise LauncherError(
            "Multiline send is unsafe in classic/startup input. Use spawn --prompt-file instead."
        )
    if len(prompt.encode("utf-8")) > 24000:
        raise LauncherError("Prompt is too large (24,000-byte limit).")
    agents = client.call("agent", "list")["agents"]
    matches = [a for a in agents if a.get("name") == name]
    if len(matches) != 1 or not _ready(matches[0]):
        raise LauncherError(f"{name} is not a unique idle Code Puppy; nothing sent.")
    agent = matches[0]
    try:
        _submit(client, agent, prompt)
    except LauncherError as exc:
        raise LauncherError(
            f"Pane {agent['pane_id']}: {exc}. Do not blindly retry a prompt."
        ) from exc
    return f"Prompt submitted to {name} in {agent['pane_id']}."


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise LauncherError(message)


def _words(command, *, windows=None):
    windows = _windows() if windows is None else windows
    words = shlex.split(command, posix=not windows)
    if windows:
        words = [
            w[1:-1] if len(w) >= 2 and w[0] == w[-1] and w[0] in "\"'" else w
            for w in words
        ]
    return words


_USAGE = "/herdr spawn NAME [--prompt TEXT | --prompt-file PATH] [-- CHILD_ARGS]\n/herdr send NAME TEXT\n/herdr send NAME --file PATH"


def execute(command, *, client_factory=ControlClient):
    """Slash-command boundary: outside-herdr check precedes all side effects."""
    try:
        _inside()
        head = command.split(maxsplit=2)
        if len(head) == 1 or head[1] == "help":
            return _USAGE
        if head[1] == "send":
            match = re.match(r"\S+\s+send\s+(\S+)(?:[ \t]+([\s\S]*))?$", command)
            if match is None or match[2] is None:
                raise LauncherError(_USAGE)
            name, text = match[1], match[2]
            _validate_name(name)
            if text.strip() == "--file" or re.match(r"--file\s", text):
                paths = _words(text)
                if len(paths) != 2:
                    raise LauncherError("Use send NAME --file PATH.")
                text = Path(paths[1]).expanduser().read_text(encoding="utf-8")
                if text.endswith("\n"):
                    text = text[:-1]
            return send(client_factory(), name, text)
        if head[1] != "spawn":
            return _USAGE
        words = _words(command)
        parser = _Parser(prog="/herdr", add_help=False)
        subs = parser.add_subparsers(dest="action", required=True)
        launch = subs.add_parser("spawn", add_help=False)
        launch.add_argument("name")
        launch.add_argument("--direction", choices=("right", "down"))
        launch.add_argument("--cwd")
        launch.add_argument("--timeout", type=float, default=30)
        prompts = launch.add_mutually_exclusive_group()
        prompts.add_argument("--prompt")
        prompts.add_argument("--prompt-file")
        tail = words[1:]
        args = []
        if tail[:1] == ["spawn"] and "--" in tail:
            index = tail.index("--")
            args, tail = tail[index + 1 :], tail[:index]
        options = parser.parse_args(tail)
        _validate_name(options.name)
        prompt = options.prompt
        if options.prompt_file:
            prompt = Path(options.prompt_file).expanduser().read_text(encoding="utf-8")
        return spawn(
            client_factory(),
            options.name,
            direction=options.direction,
            cwd=options.cwd,
            prompt=prompt,
            args=args,
            timeout=options.timeout,
        )
    except KeyError as exc:
        return f"herdr error: Invalid herdr response; missing field {exc}."
    except (ValueError, OSError) as exc:
        return f"herdr error: {exc}"
