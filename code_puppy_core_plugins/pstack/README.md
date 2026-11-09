# pstack for Code Puppy

[pstack](https://github.com/cursor/plugins/tree/main/pstack) is poteto's
(Lauren Tan) agent skill pack for Cursor: playbooks for features, bug fixes,
perf work and reviews, plus 24 one-rule engineering principles. This plugin
vendors it at a pinned commit and adapts it to Code Puppy at load time.

## Turning it on

The plugin ships with the core bundle but is **off by default**: no skills,
agents, tools or prompt text until you opt in.

```text
/pstack          # what it adds, and whether it is on
/pstack on       # same as /set pstack=true
/pstack off
```

The switch takes effect on the next prompt; no restart. `/plugins` can still
disable the plugin outright, which also removes `/pstack`.

## What you get

| Piece | Code Puppy surface |
|---|---|
| 28 skills (`poteto-mode`, `how`, `why`, `architect`, `tdd`, `interrogate`, `unslop`, ...) | `register_skills`; each is also a `/<name>` slash command |
| `deslop`, `control-ui`, `control-cli` from Cursor's `cursor-team-kit` | Registered the same way, because pstack's playbooks call them by name |
| `create-skill` (written for this plugin; Cursor's is an editor built-in) | Draft, validate, test and tune skills. `python -m code_puppy_core_plugins.pstack.skill_validator <dir>` checks a skill with Code Puppy's own loader |
| 24 `principle-*` leaves | One `pstack-principles` index skill; leaves are read on demand |
| `poteto-agent`, `comment-sicko` | `register_agents` (invokable via `invoke_agent`, selectable with `/agent`) |
| Routing rules (when to reach for which skill) | `load_prompt`, about 380 tokens; sub-agents are told to skip the playbook and do their slice |
| `/setup-pstack` model roles | `~/.code_puppy/pstack/pstack-models.md`, injected into the prompt |
| Per-role models | `list_available_models` + `invoke_agent_with_model` added to `code-puppy` |
| `/pstack [on\|off]` | The switch, plus pin, skill groups and model-role file status |

Skills carry `pstack*` tags, so `namespace_skill_search` lists the pack as five
groups (`pstack`, `pstack-understand`, `pstack-design`, `pstack-verify`,
`pstack-write`) instead of 33 lines.

Start with `/poteto-mode <task>`. `/poteto-help` explains the rest.

## How the adaptation works

Vendored files under `vendor/` mirror the cursor/plugins layout (`pstack/`, plus the three `cursor-team-kit/` skills) and are
byte-identical to upstream. `adapter.py` does all translation while rendering:

- **Rewrites** of unambiguous names: `~/.cursor/rules/pstack-models.mdc`,
  `.cursor/skills/`, `AskQuestion`, "the Task tool".
- **A glossary** prepended to each skill, containing only the entries its body
  needs: `Task`/`subagent_type` to `invoke_agent`, a per-spawn `model` to
  `invoke_agent_with_model`, `run_in_background` to `background=True`,
  transcripts to Code Puppy's autosave dirs, the bundled `cursor-team-kit`
  skills to `activate_skill`, and the remaining Cursor-only extras (Bugbot,
  cloud VMs, other team-kit skills) to "use an installed equivalent or skip".
- **An absolute path anchor**, because Code Puppy materializes only `SKILL.md`
  into its skill cache. Relative `references/`, `playbooks/` and
  `../<skill>/SKILL.md` links resolve against the vendored directory.

Not registered: `make-bot-ui` (drives Cursor's Grok Bot webhook routines) and
the dormant `automations/benny` pack (Cursor Automations). Both stay vendored
for reference. The `docs/guide` images were dropped (2+ MB of screenshots).

## Updating upstream

```bash
python scripts/refresh_pstack_upstream.py --commit <sha>
uv run pytest tests/pstack --no-cov
```

The script fetches `pstack/` and `cursor-team-kit/` at that commit, replaces
the vendor tree, and rewrites `vendor/UPSTREAM.json`
(commit, version, per-file SHA-256). The integrity test fails until every new
upstream skill is classified in `catalog.py`, and the glossary tests flag new
Cursor vocabulary that no glossary entry covers.

## License

The plugin code is MIT, like the rest of the bundle. pstack is MIT, Copyright (c) 2026
Lauren Tan; its license ships in `vendor/pstack/LICENSE` and
the bundle's `THIRD_PARTY_NOTICES.md`. The three `cursor-team-kit` skills are MIT,
Copyright (c) 2026 Cursor, with their license in `vendor/cursor-team-kit/LICENSE`.
