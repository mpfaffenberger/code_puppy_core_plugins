"""Sender-side command candidates, never handler execution or target probing."""

import os
import re
import shlex

# Restrict the leading token, not arguments (which may contain slash paths).
_TOKEN = re.compile(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*")


def command_candidates():
    """Reuse dispatcher metadata, including aliases and custom plugin help.

    Help callbacks are the discovery hook, not custom_command callbacks.
    Unadvertised custom commands fail closed; this is not a target manifest.
    """
    from code_puppy import callbacks

    # Importing dispatcher registers core categories, just as actual dispatch.
    from code_puppy.command_line import command_handler  # noqa: F401
    from code_puppy.command_line import command_registry

    registry = command_registry.get_all_commands()
    names = {"/" + name for name in registry if _TOKEN.fullmatch("/" + name)}
    custom = set()
    for result in callbacks.on_custom_command_help():
        if isinstance(result, tuple) and len(result) == 2:
            entries = [result]
        elif isinstance(result, list) and result and isinstance(result[0], tuple):
            entries = result
        elif (
            isinstance(result, list)
            and result
            and isinstance(result[0], str)
            and result[0].startswith("/")
            and " - " in result[0]
        ):
            entries = [(result[0].split(" - ", 1)[0].lstrip("/"), "")]
        else:
            continue
        for entry in entries:
            if (
                isinstance(entry, tuple)
                and len(entry) == 2
                and isinstance(entry[0], str)
            ):
                token = "/" + entry[0]
                if _TOKEN.fullmatch(token):
                    custom.add(token)
    # The dispatcher routes multi-slash tokens through custom hooks ONLY.
    names = {token for token in names if token.count("/") == 1}
    return names, custom


def validate_command(text):
    """Validate a single exact leading token; return payload without rewriting."""
    if not text or any(
        ord(char) < 32 or 127 <= ord(char) <= 159 or (char.isspace() and char != " ")
        for char in text
    ):
        raise ValueError("Command must be one line without terminal controls.")
    if len(text.encode("utf-8")) >= 1000:
        raise ValueError("Command requires under 1,000 UTF-8 bytes.")
    token = text.split(" ", 1)[0]
    if not _TOKEN.fullmatch(token):
        raise ValueError("Command requires an exact leading /command token.")
    try:
        shlex.split(text, posix=os.name != "nt")
    except ValueError as exc:
        raise ValueError("Command has malformed quoting.") from exc
    builtin, custom = command_candidates()
    recognized = token in custom or any(
        token.lower() == name.lower() for name in builtin
    )
    if not recognized:
        raise ValueError(
            "Unknown sender command candidate. Use /herdr commands; nothing sent."
        )
    return text


def candidate_help():
    builtin, custom = command_candidates()
    return (
        "Sender command candidates (aliases included; target may differ).\n"
        "Not a safety or execution guarantee; unadvertised plugins fail closed.\n"
        + " ".join(sorted(builtin | custom))
    )


GUIDANCE = """## Herdr sibling input (inside herdr only)
Use /herdr spawn NAME --prompt-file PATH for first tasks, multiline or larger briefs.
Use /herdr send NAME ordinary prompt for prompt-only follow-up (one line, <1,000 UTF-8 bytes;
slash/bang tokens are rejected). To intentionally request a potentially mutating or interactive
Code Puppy command, use /herdr command NAME /model gpt-5, not send.
Discover known sender candidates/aliases with /herdr commands (core registry + plugin help).
Unknown/unadvertised commands fail closed. The target may have different commands/plugins;
recognized does not mean safe. Choose command only for intentionally authorized command intent.
Both actions type into the terminal; verify target and actual foreground editor readiness.
Idle is not editor-ready. Acknowledgement is only a write, not confirmed execution or receipt.
Core preprocessing removes quotes, normalizes spaces, and may consume attachments on both sides;
arguments are not byte-preserved. No automatic retries or atomic occupant-check guarantee.
Use /herdr help for options. From shell tools use the installed interpreter to call
code_puppy_core_plugins.herdr.launcher.execute('/herdr ...') and print the result;
this is the same guarded API, not raw herdr input. Never probe by executing commands.
"""
