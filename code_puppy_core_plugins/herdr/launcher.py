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

_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")


class LauncherError(ValueError):
    """An actionable launcher failure."""


class ControlClient:
    """Bounded request/response adapter; herdr owns platform socket handling."""

    def __init__(self):
        self.binary = os.environ.get("HERDR_BIN_PATH") or "herdr"

    def call(self, *args):
        try:
            reply = subprocess.run(
                [self.binary, *args], capture_output=True, text=True, timeout=5
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
                body.get("error", {}).get("message", "herdr request failed")
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
    """Reuse this interpreter and entry point, never the user's shell alias."""
    original = getattr(sys, "orig_argv", [])
    index = 1
    while index < len(original):
        option = original[index]
        if option == "-m" and index + 1 < len(original):
            return [sys.executable, "-m", original[index + 1], *args]
        if option in ("-c", "--") or not option.startswith("-"):
            break  # application arguments after this boundary are not Python flags
        index += 2 if option in ("-W", "-X", "--check-hash-based-pycs") else 1
    entry = Path(sys.argv[0]).absolute()
    if not entry.is_file():
        raise LauncherError("Cannot resolve the current Code Puppy entry point.")
    return [sys.executable, str(entry), *args]


def _shell_command(argv):
    if any(any(ord(c) < 32 or ord(c) == 127 for c in arg) for arg in argv):
        raise LauncherError("Launch arguments cannot contain terminal controls.")
    if os.name == "nt":
        # Herdr's default Windows shell is PowerShell. Do not use cmd.exe's
        # list2cmdline escaping for a PowerShell command.
        return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
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
    command = _shell_command(launch_argv(args))
    agents = client.call("agent", "list")["agents"]
    if any(agent.get("name") == name for agent in agents):
        raise LauncherError(f"Agent name {name} is already in use.")
    caller = os.environ["HERDR_PANE_ID"]
    if direction is None:
        layout = client.call("pane", "layout", "--pane", caller)["layout"]
        rect = next(p["rect"] for p in layout["panes"] if p["pane_id"] == caller)
        direction = "right" if rect["width"] > rect["height"] else "down"
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
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # A fresh shell has no agent yet. agent.get may report not found;
            # list instead lets startup progress without masking other errors.
            agents = client.call("agent", "list")["agents"]
            agent = next((a for a in agents if a.get("pane_id") == pane), None)
            if agent is None:
                time.sleep(0.1)
                continue
            agent = _get(client, pane)
            if _ready(agent):
                break
            time.sleep(0.1)
        else:
            raise LauncherError(
                f"Timed out waiting {timeout:g}s for reported idle state"
            )
        # Rename only once an occupant exists. The server enforces uniqueness
        # again, catching a concurrent launch that won the name in the meantime.
        client.call("agent", "rename", pane, name)
        if prompt is not None:
            _submit(client, agent, prompt)
    except (LauncherError, KeyError, StopIteration) as exc:
        raise LauncherError(
            f"Pane {pane} created for {name}: {exc}. Inspect it; it was not closed. Do not blindly retry a prompt."
        ) from exc
    return f"Started {name} in {pane}" + (
        "; prompt submitted." if prompt is not None else "; idle."
    )


def send(client, name, prompt):
    _inside()
    _validate_name(name)
    prompt = _text(prompt)
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


def execute(command, *, client_factory=ControlClient):
    """Slash-command boundary: outside-herdr check precedes all side effects."""
    try:
        _inside()
        words = shlex.split(command)
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
        deliver = subs.add_parser("send", add_help=False)
        deliver.add_argument("name")
        deliver.add_argument("text", nargs="+")
        tail = words[1:]
        args = []
        if tail[:1] == ["spawn"] and "--" in tail:
            index = tail.index("--")
            args, tail = tail[index + 1 :], tail[:index]
        options = parser.parse_args(tail)
        _validate_name(options.name)
        if options.action == "send":
            text = " ".join(options.text)
            prompt = (
                Path(text[1:]).expanduser().read_text(encoding="utf-8")
                if text.startswith("@")
                else text
            )
            return send(client_factory(), options.name, prompt)
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
    except (ValueError, OSError, KeyError, StopIteration) as exc:
        return f"herdr error: {exc}"
