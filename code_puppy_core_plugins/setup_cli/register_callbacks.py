"""Expose ``code-puppy --setup ...`` through the CLI-arg plugin hooks.

``--setup`` swallows the rest of the command line (``argparse.REMAINDER``)
so setup subcommands can own flags like ``--json`` without colliding with
the agent's own parser. Handling runs before messaging starts, so the
setup CLI prints to real stdout/stderr and terminates the process with
its own exit code; normal runs are untouched.
"""

from __future__ import annotations

import argparse
from typing import Any, Optional

from code_puppy.callbacks import register_callback


def _register_cli_args(parser: Any) -> None:
    parser.add_argument(
        "--setup",
        nargs=argparse.REMAINDER,
        default=None,
        metavar="COMMAND [ARGS...]",
        help=(
            "Configure Code Puppy and exit: auth, models, plugins, MCP, "
            "skills, agents, settings (try: --setup --help)"
        ),
    )


def _handle_cli_args(args: Any) -> Optional[dict]:
    tokens = getattr(args, "setup", None)
    if tokens is None:
        return None
    from .cli import main

    return {"handled": True, "exit_code": main(list(tokens))}


register_callback("register_cli_args", _register_cli_args)
register_callback("handle_cli_args", _handle_cli_args)
