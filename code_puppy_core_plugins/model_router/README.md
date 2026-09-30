# Model router

Routes each turn to the cheapest model that can handle it.

A judge model scores every request on two questions, how demanding it is for
a coding assistant and whether a mistake would be costly or hard to notice.
The plugin maps those answers to one of your configured tiers and runs the
turn there. The judge never generates text; a decision costs a fraction of a
cent and takes a few hundred milliseconds.

The judge is any model that speaks the TypeSafe `systemone` API: Jev hosted by
TypeSafe (the default), a local [Kev](https://github.com/jaredpalmer/kev), or a
successor with the same shape.

## Quick start

```text
/router models                                   # names you can use as tiers
/router init --flagship copilot-claude-opus-5 --mid copilot-claude-sonnet-5 --cheap copilot-claude-haiku-4.5
/set typesafe_api_key=<key>                      # or export TYPESAFE_API_KEY
/model auto                                      # routing on
```

Any other `/model` choice switches routing off. Tab completion covers the
subcommands, the `init` flags and the registered model names.

Get a TypeSafe key at <https://console.typesafe.ai>.

## How a turn is routed

1. Once per run, before the agent is built, the `model_select` hook fires with
   the prompt and the message history. It does nothing unless the current
   model is `auto`.
2. The plugin builds a compact state: the prompt, a short excerpt of the
   system prompt, the tail of the previous reply, and the turn count. Tool
   calls and tool results are never sent.
3. One `systemone` call asks the judge two questions:

   | Question | Type | Used for |
   | --- | --- | --- |
   | `complexity` | Score, 4 levels: trivial edit, routine single-file change, involved multi-file work, expert design or subtle bugs | `complexity_to_tier[level]` picks the tier |
   | `risky` | Noul: would a mistake be costly or hard to notice | `risk_floor` raises the tier when the probability is high |

4. Policy, in code: level picks a tier; `risk_floor` raises it. When the
   judge's confidence is below `confidence_threshold`, `low_confidence`
   decides: `next_level` (default) takes the higher of the two most likely
   levels, unless the expert level carries 20% or more, in which case the turn
   goes to `fallback_model`; `fallback` always goes to `fallback_model`.
5. The chosen model name goes back to code_puppy, which builds it through the
   normal factory. Copilot auth, reasoning profiles, Responses API handling and
   context length all come from the real model.

The whole tool loop of that turn stays on the chosen model. If the judge is
unreachable, returns an error, or a tier names a model that is not registered,
the turn goes to `fallback_model`. Routing never fails a run.

## Configuration

`model_router.json` in code_puppy's data directory, next to
`extra_models.json`. `/router init` writes it; edit the tiers afterwards.

```json
{
  "enabled": true,
  "virtual_model": "auto",
  "fallback_model": "copilot-claude-opus-5",
  "tiers": ["copilot-claude-haiku-4.5", "copilot-claude-sonnet-5", "copilot-claude-opus-5"],
  "complexity_to_tier": ["copilot-claude-haiku-4.5", "copilot-claude-haiku-4.5",
                         "copilot-claude-sonnet-5", "copilot-claude-opus-5"],
  "risk_floor": { "threshold": 0.7, "min_tier": "copilot-claude-sonnet-5" },
  "confidence_threshold": 0.6,
  "low_confidence": "next_level",
  "max_request_chars": 12000,
  "context_length": 200000,
  "judge": {
    "base_url": "https://api.typesafe.ai",
    "model": "jev-latest",
    "timeout_s": 4.0,
    "api_key_env": "TYPESAFE_API_KEY"
  }
}
```

| Field | Meaning |
| --- | --- |
| `tiers` | Model names ordered from cheapest to strongest. Names are the ones `/model` shows, so any registered model works, across providers. |
| `complexity_to_tier` | Four entries, one per complexity level 0 to 3. Each must be in `tiers`. |
| `risk_floor` | Optional. When the risky probability is at or above `threshold`, the tier is raised to at least `min_tier`. Remove the block to ignore risk. |
| `fallback_model` | Used on low confidence, judge errors, or an unknown tier. Usually the flagship. |
| `confidence_threshold` | Below this complexity confidence the `low_confidence` rule applies. |
| `low_confidence` | `next_level` (default): pick the higher of the two most likely levels, or the fallback when the expert level has 20% or more. `fallback`: always the fallback model. |
| `max_request_chars` | Cap on the prompt text sent to the judge. Longer prompts keep their head and tail. |
| `context_length` | Advertised context length of `auto` while idle. During a run the routed model's own length applies. |
| `judge.base_url` | The systemone endpoint. Point it at a local Kev to route without the hosted service. |
| `judge.model` | Model id sent in the request. |
| `judge.api_key_env` | Environment variable or `/set` key holding the API key. Set to `null` for a judge that needs none. The hosted TypeSafe endpoint requires one. |
| `judge.timeout_s` | Judge timeout. The hook runs synchronously, so keep it short. |

Precedence follows code_puppy's rules: an explicit runtime override, as used
for subagents, beats the router; the router beats pinned and global models.
An agent pinned to a specific model is left alone.

## Commands

| Command | Purpose |
| --- | --- |
| `/router` | Status: config, key, tiers, last decision, unknown model names. |
| `/router models` | Registered model names grouped by type. |
| `/router init [--flagship M] [--mid M] [--cheap M] [--force]` | Write a starting config. Unspecified tiers default to the flagship. |
| `/router route <prompt>` | Dry run: judge the prompt and show the decision with probabilities, without running anything. |
| `/router last` | The last decision in full. |
| `/router log [n]` | Recent routed runs with token counts, plus totals per model and per reason. |
| `/router on` / `/router off` | Toggle routing without editing the file. |
| `/router reload` | Re-read the config file. |

Decision reasons: `judged`, `rounded_up`, `low_confidence`, `judge_error`,
`no_api_key`, `unknown_model`, `empty_prompt`.

## Tuning

Use `/router route` on prompts you know the answer for and look at the level
probabilities. If easy prompts land on the flagship, check the reason first: `no_api_key`
means the judge was never asked. Otherwise lower `confidence_threshold` or
move level 1 to the cheap tier. If hard prompts land
on the cheap model, move level 2 up a tier or lower `risk_floor.threshold`.
The level descriptions live in `questions.py` if what "involved" means for
your codebase differs.

## Log

Every routed run appends a line to `model_routes.jsonl` in code_puppy's state
directory: agent, chosen model, reason, level, confidence, risky probability,
judge latency and usage, and the run's input and output tokens.

## Layout

```
register_callbacks.py   hooks: model_select, load_models_config, register_model_type,
                        agent_run_end, custom_command, completion provider
config.py               model_router.json schema, load/save, template
state.py                prompt + pydantic-ai history -> compact judge state
questions.py            the two questions in systemone wire format
judge_client.py         one synchronous POST to /v1/systemone
policy.py               answers -> model name
router.py               config cache, decision, catalogue check, JSONL log
completer.py            tab completion for /router
```
