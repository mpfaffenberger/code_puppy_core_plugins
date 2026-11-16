"""Create prompt commands without introducing another command execution path."""

import re
from pathlib import Path

from code_puppy.command_line.command_registry import get_command, register_command
from code_puppy.messaging import emit_error, emit_info

_USAGE = "/commands create [--project] <name> <instructions>"
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


def _create(arguments: str) -> None:
    from code_puppy.callbacks import on_custom_command_help

    from . import register_callbacks as commands

    project = arguments.startswith("--project ")
    if project:
        arguments = arguments[len("--project ") :].lstrip()
    parts = arguments.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        emit_error(f"Usage: {_USAGE}")
        return
    name, instructions = parts
    if not _NAME.fullmatch(name):
        emit_error(
            "Use a lowercase name starting with a letter (letters, digits, - or _)."
        )
        return

    # Include plugin commands and skills, not just Markdown files. A saved
    # command shadowed by another handler would be a rather unhelpful success.
    commands.reload_commands()
    advertised = {
        entry[0].lower()
        for group in on_custom_command_help()
        if isinstance(group, list)
        for entry in group
        if isinstance(entry, tuple) and entry and isinstance(entry[0], str)
    }
    if get_command(name) or name in advertised or commands.is_custom_command(name):
        emit_error(
            f"/{name} already exists. Choose another name; nothing was overwritten."
        )
        return

    root = Path(".agents/commands") if project else Path(commands._GLOBAL_COMMANDS_DIR)
    # Project output must not follow a repository-controlled directory link.
    if root.is_symlink() or (project and root.parent.is_symlink()):
        emit_error(f"Refusing to create a command through a symlink: {root}")
        return
    target = root / f"{name}.md"
    try:
        root.mkdir(parents=True, exist_ok=True)
        # Exclusive creation rejects existing files AND dangling symlinks.
        # A heading keeps supplied frontmatter-looking text in the prompt body,
        # so this helper can never create an exec: directive.
        with target.open("x", encoding="utf-8") as output:
            output.write(f"# Custom command\n\n{instructions.strip()}\n")
    except FileExistsError:
        emit_error(f"{target} already exists; nothing was overwritten.")
        return
    except OSError as exc:
        emit_error(f"Could not create command: {exc}")
        return
    commands.reload_commands()
    emit_info(f"Created /{name} at {target.absolute()}")
    emit_info(
        f"Run /{name} [additional context]. Edit the Markdown file for longer instructions."
    )


@register_command(
    name="commands",
    description="List custom slash commands or create a Markdown prompt command",
    usage="/commands [list | create [--project] <name> <instructions>]",
    category="config",
)
def handle_commands(command: str) -> bool:
    """A non-interactive helper usable from both terminal and headless clients."""
    from . import register_callbacks as commands

    parts = command.split(maxsplit=2)
    action = parts[1] if len(parts) > 1 else "list"
    if action == "create":
        _create(parts[2] if len(parts) > 2 else "")
    elif action == "list" and len(parts) < 3:
        commands.reload_commands()
        if not commands._command_descriptions:
            emit_info("No custom commands found.")
        for name, description in sorted(commands._command_descriptions.items()):
            emit_info(f"/{name}: {description}")
            emit_info(f"  {commands._command_sources[name]}")
        emit_info(f"Create a global command: {_USAGE}")
        emit_info("Use --project to save in .agents/commands for this project.")
    else:
        emit_error(f"Usage: /commands [list] or {_USAGE}")
    return True
