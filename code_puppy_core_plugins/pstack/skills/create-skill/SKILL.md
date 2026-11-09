---
name: create-skill
description: >-
  Create, edit, test, or tune a Code Puppy skill (a SKILL.md with frontmatter
  and optional references/). Use for /create-skill, "make a skill for X",
  "turn this into a skill", substantive edits to an existing skill, or "tune
  description" when a skill doesn't trigger when it should.
---

# Create skill

A skill is a directory holding `SKILL.md` and, optionally, `references/`,
`scripts/`, or `assets/`. Code Puppy lists only each skill's name and
description, so the description decides whether the skill ever loads. The
body loads only on `activate_skill`. Files beside it load only when the body
tells the agent to read them.

## 1. Place it

| Scope | Path |
|---|---|
| This repo (default when the skill is about the repo) | `.code_puppy/skills/<name>/SKILL.md` |
| Every project for this user | `~/.code_puppy/skills/<name>/SKILL.md` |

The directory name is the skill name: lowercase kebab-case, at most 64
characters. Before you create one, check with `list_or_search_skills` that no
existing skill already covers the topic. Extending a real home beats a new
skill. Editing an existing skill keeps its path and its name.

## 2. Frontmatter (YAML rules)

```yaml
---
name: <name>
description: >-
  <What it does, in one sentence.> Use for <trigger phrases and situations>.
tags:
  - <namespace>
---
```

- `name` and `description` are required. Keep `description` one YAML scalar.
  Use `>-` with indented continuation lines, or quote it, whenever it contains
  `: `, `#`, or a leading quote.
- Description: under 1024 characters. Write the trigger in the user's words
  ("make this faster", "/perf"), not the topic in yours. Name what it is NOT
  for when a neighbour skill is close.
- `tags` is optional. The first tag is the namespace the skill catalogue
  groups it under, so reuse an existing namespace when one fits.
- Code Puppy has no `disable-model-invocation`. A skill that should run only
  when called by name needs a description that triggers only on its name or
  `/<name>`.

## 3. Body (writing guidelines)

- Imperative steps, in order. Tell the agent what to do and skip why, unless
  the rule is confusing without a reason.
- Every sentence must change a decision. Delete the rest. No intro, no
  summary of what the agent already knows.
- Use real tool names: `read_file`, `grep`, `shell`, `invoke_agent`,
  `ask_user_question`, `activate_skill`.
- Keep `SKILL.md` under 500 lines. Move long tables, examples, and variants
  into `references/<topic>.md`, and say exactly when to read each one.
- Link bundled files with relative paths (`references/x.md`). They resolve
  against the skill directory.
- Apply the `unslop` skill to every line.

## 4. Validate

```bash
$CODE_PUPPY_PYTHON -m code_puppy_core_plugins.pstack.skill_validator <skill-dir>
```

It runs Code Puppy's own frontmatter parser, then checks the name against the
directory, the description length and trigger clause, and every relative link.
Fix every `error`; justify or fix each `warning`. Then run `/skills refresh`
(or start a new session) so `activate_skill` and `/<name>` see the skill.

## 5. Draft / test / iterate loop

Skip this loop for subjective skills (personal style, tone). Show the draft
to the user instead.

1. Write 2 or 3 realistic requests the skill must handle, including one edge
   case.
2. For each request, start a fresh subagent with `invoke_agent("code-puppy",
   ...)`. The prompt is "Read `<abs path>/SKILL.md` and follow it for this
   request: <request>". Tell it to work in a scratch directory and not to
   edit the skill. Start them together.
3. Read each result against what the skill promises. Note where the agent
   guessed, skipped a step, or ignored a rule.
4. Fix the skill's wording at the cause, not the symptom. Re-run only the
   failing requests. Stop when all pass or after 3 rounds, and report what
   still fails.

## 6. Description-optimization loop

Use when a skill exists but doesn't trigger, or triggers when it shouldn't.

1. Write 4 requests that should load the skill and 4 near misses that should
   load a neighbour or nothing.
2. Get the catalogue with `list_or_search_skills()`. For each request, ask a
   fresh subagent: "Given these skills (name: description), which one would
   you activate for: <request>? Answer with one name or `none`." Do not show
   it the skill body.
3. Score hits and false triggers. Rewrite only the description: add the
   missed phrasings, add a "not for" clause for false triggers.
4. Repeat up to 3 rounds. Report the before and after score.

## Reply

Path, final description, validator result, test or tuning scores, and any
request that still fails.
