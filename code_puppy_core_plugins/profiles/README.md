# Configuration profiles

Use different model/agent harness settings without having terminals overwrite
each other's pins:

```text
/profile                  # keyboard menu: c creates, Enter activates
/profile create web       # snapshot this process's current config
/profile use web          # switch only this running process
/profile list
```

```sh
code-puppy --profile web
code-puppy --profile default
code-puppy                  # always the default profile
```

Named profiles store INI settings in
`~/.code_puppy/profiles/<name>/puppy.cfg` (or under
`$XDG_CONFIG_HOME/code_puppy/profiles` when configured). `default` keeps using
the existing `puppy.cfg`; no global active-profile pointer is needed.
A nonexistent CLI profile exits with status 2, rather than silently
running the wrong harness. Names are lowercase letters/digits/underscores/
hyphens, 1–64 characters, starting with a letter or digit.

All `puppy.cfg` settings are snapshotted, including agent model pins, model
settings, token limits and `/set` values. Changes persist within that profile.
Two processes deliberately using the **same** profile still share its settings;
use distinct names for independent harnesses. Switching rebuilds the current
agent while retaining its conversation, rather than changing agent identity.

Model catalogs, credentials, custom agent definitions, skills, MCP server files
and conversation storage remain shared. Profiles do not restrict which agents
a model may invoke. A profile is a settings preset, not a security sandbox.
Provider API keys are stored once in the OS keyring, with the existing
permission-hardened `secrets.json` fallback. Creating or selecting a profile
migrates recognized legacy credentials from all profile configs, checking for
conflicts before writing and removing legacy values only after verified storage.
Conflicting values block the operation without choosing a winner. Reconcile the
reported key in the legacy configs and shared store, then retry. Values are never
included in the conflict message. Environment-only keys are not copied.
Custom model API-key and secret-header environment references are recognized;
unrelated arbitrary secrets cannot be inferred, so configs still need care.
Data is INI, not executed Python. The plugin's `config.py` contains storage logic.

No deletion or overwrite operation is exposed in this initial version.
