# setup_cli

Scriptable configuration for Code Puppy: `code-puppy --setup <command>`.

The interactive UI is great for humans at a terminal and useless for
scripts, SSH sessions, CI, and sandbox provisioning. This plugin adds
a flag-driven front door over the exact same machinery the TUI uses —
no parallel config logic.

```console
code-puppy --setup wizard                     # guided first-run setup
code-puppy --setup doctor [--network]         # what's broken / missing?
code-puppy --setup auth login meta            # delegate to the plugin's own flow
code-puppy --setup auth login zai-coding-plan # models.dev id (raw env var also works)
code-puppy --setup auth list --json
code-puppy --setup model add anthropic/claude-haiku-5-5 --set-default
code-puppy --setup model list
code-puppy --setup mcp add fetch --command uvx --arg mcp-server-fetch --bind code-puppy
code-puppy --setup plugin add ./my-plugin     # shows code, asks first
code-puppy --setup skill add ./my-skill
code-puppy --setup agent new reviewer
code-puppy --setup config set yolo_mode true
code-puppy --setup export ./my-bundle --sbx   # secret-free, sandbox-ready
code-puppy --setup sbx plan                  # credentials/egress your models need
```

Design notes:

- `--setup` is registered via `register_cli_args` with
  `argparse.REMAINDER`; the plugin parses its own subcommands, prints
  to real stdout (messaging renderers do not exist yet at
  `handle_cli_args` time), and exits with its own code.
- OAuth logins delegate to each plugin's existing interactive command
  in a subprocess (`code-puppy -p "/x-auth"`), so browser/callback/
  device flows keep working untouched. Status reads use the plugins'
  own loaders and never surface credential values.
- `providers.py` carries the provider registry, including how each
  credential can be fed to a Docker sandbox (`sandbox_transport`).
