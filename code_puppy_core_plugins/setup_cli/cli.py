"""Argument parsing and dispatch for ``code-puppy --setup ...``.

The host CLI hands us the raw token tail after ``--setup``
(``argparse.REMAINDER``); this module owns a private subparser tree so
setup flags never collide with the agent's own flags.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import (
    agent_cmds,
    auth_cmds,
    config_cmds,
    doctor,
    export_cmds,
    mcp_cmds,
    model_cmds,
    plugin_cmds,
    sbx_plan,
    skill_cmds,
    wizard,
)
from .output import Out, SetupError


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")

    parser = argparse.ArgumentParser(
        prog="code-puppy --setup",
        description="Configure Code Puppy non-interactively "
        "(auth, models, plugins, MCP servers, skills, agents, settings).",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="group")

    p = sub.add_parser("doctor", parents=[common], help="diagnose the current setup")
    p.add_argument("--network", action="store_true", help="also probe provider domains")
    p.set_defaults(func=_doctor)

    p = sub.add_parser("wizard", parents=[common], help="guided first-run setup")
    p.add_argument("--provider", help="provider id (see `auth list`)")
    p.add_argument("--model", help="model key to make the default")
    p.set_defaults(func=_wizard)

    p = sub.add_parser("config", parents=[common], help="puppy.cfg access")
    pc = p.add_subparsers(dest="action", required=True)
    g = pc.add_parser("get", parents=[common])
    g.add_argument("key", nargs="?")
    g.set_defaults(func=lambda o, a: config_cmds.cmd_config_get(o, a.key))
    s = pc.add_parser("set", parents=[common])
    s.add_argument("key")
    s.add_argument("value")
    s.set_defaults(func=lambda o, a: config_cmds.cmd_config_set(o, a.key, a.value))
    pc.add_parser("path", parents=[common]).set_defaults(
        func=lambda o, a: config_cmds.cmd_config_path(o)
    )

    p = sub.add_parser("model", parents=[common], help="model catalog")
    pm = p.add_subparsers(dest="action", required=True)
    pm.add_parser("list", parents=[common]).set_defaults(
        func=lambda o, a: model_cmds.cmd_model_list(o)
    )
    c = pm.add_parser("catalog", parents=[common])
    c.add_argument("provider", nargs="?")
    c.add_argument("--search")
    c.set_defaults(
        func=lambda o, a: model_cmds.cmd_model_catalog(o, a.provider, a.search)
    )
    a_ = pm.add_parser("add", parents=[common])
    a_.add_argument("ref", help="<provider>/<model-id>")
    a_.add_argument("--set-default", action="store_true")
    a_.set_defaults(func=lambda o, a: model_cmds.cmd_model_add(o, a.ref, a.set_default))
    r = pm.add_parser("remove", parents=[common])
    r.add_argument("key")
    r.set_defaults(func=lambda o, a: model_cmds.cmd_model_remove(o, a.key))
    d = pm.add_parser("default", parents=[common])
    d.add_argument("key", nargs="?")
    d.set_defaults(func=lambda o, a: model_cmds.cmd_model_default(o, a.key))
    pm.add_parser("refresh", parents=[common]).set_defaults(
        func=lambda o, a: model_cmds.cmd_model_refresh(o)
    )

    p = sub.add_parser("auth", parents=[common], help="provider authentication")
    pa = p.add_subparsers(dest="action", required=True)
    pa.add_parser("list", parents=[common]).set_defaults(
        func=lambda o, a: auth_cmds.cmd_auth_list(o)
    )
    s = pa.add_parser("status", parents=[common])
    s.add_argument("provider", help="OAuth id, models.dev provider id, or env var")
    s.set_defaults(func=lambda o, a: auth_cmds.cmd_auth_status(o, a.provider))
    li = pa.add_parser("login", parents=[common])
    li.add_argument("provider", help="OAuth id, models.dev provider id, or env var")
    li.add_argument(
        "--from-env", metavar="VAR", help="read an API key from this env var"
    )
    li.add_argument("--stdin", action="store_true", help="read an API key from stdin")
    li.set_defaults(
        func=lambda o, a: auth_cmds.cmd_auth_login(o, a.provider, a.from_env, a.stdin)
    )
    lo = pa.add_parser("logout", parents=[common])
    lo.add_argument("provider", help="OAuth id, models.dev provider id, or env var")
    lo.set_defaults(func=lambda o, a: auth_cmds.cmd_auth_logout(o, a.provider))

    p = sub.add_parser("mcp", parents=[common], help="MCP servers")
    pmcp = p.add_subparsers(dest="action", required=True)
    pmcp.add_parser("list", parents=[common]).set_defaults(
        func=lambda o, a: mcp_cmds.cmd_mcp_list(o)
    )
    a_ = pmcp.add_parser("add", parents=[common])
    a_.add_argument("name")
    a_.add_argument("--command")
    a_.add_argument("--url")
    a_.add_argument("--arg", action="append", default=[])
    a_.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    a_.add_argument("--bind", metavar="AGENT")
    a_.set_defaults(
        func=lambda o, a: mcp_cmds.cmd_mcp_add(
            o, a.name, a.command, a.arg, a.url, a.env, a.bind
        )
    )
    r = pmcp.add_parser("remove", parents=[common])
    r.add_argument("name")
    r.set_defaults(func=lambda o, a: mcp_cmds.cmd_mcp_remove(o, a.name))
    b = pmcp.add_parser("bind", parents=[common])
    b.add_argument("agent")
    b.add_argument("server")
    b.add_argument("--no-auto-start", action="store_true")
    b.set_defaults(
        func=lambda o, a: mcp_cmds.cmd_mcp_bind(o, a.agent, a.server, a.no_auto_start)
    )
    u = pmcp.add_parser("unbind", parents=[common])
    u.add_argument("agent")
    u.add_argument("server")
    u.set_defaults(func=lambda o, a: mcp_cmds.cmd_mcp_unbind(o, a.agent, a.server))

    p = sub.add_parser("plugin", parents=[common], help="user plugins")
    pp = p.add_subparsers(dest="action", required=True)
    pp.add_parser("list", parents=[common]).set_defaults(
        func=lambda o, a: plugin_cmds.cmd_plugin_list(o)
    )
    a_ = pp.add_parser("add", parents=[common])
    a_.add_argument("source", help="directory or git URL")
    a_.add_argument("--name")
    a_.add_argument("--yes", action="store_true")
    a_.add_argument("--force", action="store_true")
    a_.set_defaults(
        func=lambda o, a: plugin_cmds.cmd_plugin_add(
            o, a.source, a.name, a.yes, a.force
        )
    )
    r = pp.add_parser("remove", parents=[common])
    r.add_argument("name")
    r.add_argument("--yes", action="store_true")
    r.set_defaults(func=lambda o, a: plugin_cmds.cmd_plugin_remove(o, a.name, a.yes))
    for verb, disabled in (("enable", False), ("disable", True)):
        v = pp.add_parser(verb, parents=[common])
        v.add_argument("name")
        v.set_defaults(
            func=lambda o, a, d=disabled: plugin_cmds.cmd_plugin_set_disabled(
                o, a.name, d
            )
        )

    p = sub.add_parser("skill", parents=[common], help="user skills")
    ps = p.add_subparsers(dest="action", required=True)
    ps.add_parser("list", parents=[common]).set_defaults(
        func=lambda o, a: skill_cmds.cmd_skill_list(o)
    )
    a_ = ps.add_parser("add", parents=[common])
    a_.add_argument("source")
    a_.add_argument("--name")
    a_.add_argument("--yes", action="store_true")
    a_.add_argument("--force", action="store_true")
    a_.set_defaults(
        func=lambda o, a: skill_cmds.cmd_skill_add(o, a.source, a.name, a.yes, a.force)
    )
    r = ps.add_parser("remove", parents=[common])
    r.add_argument("name")
    r.add_argument("--yes", action="store_true")
    r.set_defaults(func=lambda o, a: skill_cmds.cmd_skill_remove(o, a.name, a.yes))

    p = sub.add_parser("agent", parents=[common], help="JSON agent definitions")
    pag = p.add_subparsers(dest="action", required=True)
    pag.add_parser("list", parents=[common]).set_defaults(
        func=lambda o, a: agent_cmds.cmd_agent_list(o)
    )
    a_ = pag.add_parser("add", parents=[common])
    a_.add_argument("file")
    a_.add_argument("--force", action="store_true")
    a_.set_defaults(func=lambda o, a: agent_cmds.cmd_agent_add(o, a.file, a.force))
    n = pag.add_parser("new", parents=[common])
    n.add_argument("name")
    n.add_argument("--description", default="")
    n.set_defaults(func=lambda o, a: agent_cmds.cmd_agent_new(o, a.name, a.description))
    r = pag.add_parser("remove", parents=[common])
    r.add_argument("name")
    r.add_argument("--yes", action="store_true")
    r.set_defaults(func=lambda o, a: agent_cmds.cmd_agent_remove(o, a.name, a.yes))

    p = sub.add_parser("export", parents=[common], help="write a secret-free bundle")
    p.add_argument("directory")
    p.add_argument("--sbx", action="store_true", help="Docker sbx mixin overlay layout")
    p.set_defaults(func=lambda o, a: export_cmds.cmd_export(o, a.directory, a.sbx))

    p = sub.add_parser("import", parents=[common], help="apply an exported bundle")
    p.add_argument("directory")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(
        func=lambda o, a: export_cmds.cmd_import(o, a.directory, a.yes, a.dry_run)
    )

    p = sub.add_parser(
        "sbx", parents=[common], help="Docker sandbox derivation helpers"
    )
    psbx = p.add_subparsers(dest="action", required=True)
    plan_p = psbx.add_parser(
        "plan",
        parents=[common],
        help="derive sandbox credentials/egress from configured models",
    )
    plan_p.add_argument("--provider", help="restrict to one service/provider id")
    plan_p.add_argument(
        "--prune",
        action="store_true",
        help="remove stored sbx secrets the current catalog no longer needs",
    )
    plan_p.set_defaults(func=lambda o, a: sbx_plan.cmd_sbx_plan(o, a.provider, a.prune))
    creds_p = psbx.add_parser(
        "creds-mixin",
        parents=[common],
        help="write the generated credentials mixin for sbx composition",
    )
    creds_p.add_argument("directory")
    creds_p.add_argument("--provider", help="restrict to one service/provider id")
    creds_p.set_defaults(
        func=lambda o, a: sbx_plan.cmd_sbx_creds_mixin(o, a.directory, a.provider)
    )
    return parser


def _doctor(out: Out, args: argparse.Namespace) -> int:
    report, code = doctor.run_doctor(check_network=args.network)
    out.result(report, human=doctor.render_human(report))
    return code


def _wizard(out: Out, args: argparse.Namespace) -> int:
    return wizard.run_wizard(out, args.provider, args.model)


def main(tokens: list[str]) -> int:
    """Entry point called from ``handle_cli_args`` with the raw tail."""
    parser = build_parser()
    args = parser.parse_args(tokens)
    out = Out(json_mode=bool(getattr(args, "json", False)))
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2
    try:
        return int(func(out, args) or 0)
    except SetupError as exc:
        if out.json_mode:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except BrokenPipeError:
        return 0
