# gemini_loop_guard

Helps Gemini models stop searching once they stop learning anything.

Gemini Flash models can keep calling tools for a target that does not exist:
`grep "Foo"`, `grep -i foo`, `grep "Foo" code_puppy/`... each answer is
"nothing", and the model tries yet another variation. Larger models notice
and say "it isn't there". This plugin gives Gemini that same observation at
the moment it matters, without changing the system prompt or taking tools away.

It only acts for models whose name contains `gemini`, inside an agent run.
Every other model is untouched.

## How it decides

After each tool call (`post_tool_call`) the result is classified:

| Outcome | Meaning |
|---|---|
| productive | Contains something not seen earlier in the run (a new match, file, or output) |
| empty | No results |
| error | An error and nothing new |
| repeat | Only things already seen, e.g. a re-worded grep that finds the same lines |
| unknown | Another hook annotated the result, so it cannot be judged (it neither extends nor resets the streak) |

Consecutive non-productive calls form a *streak*; any productive call resets
it. Call count alone never triggers anything, so long refactors are unaffected.

## What it does (`pre_tool_call`)

| Level | When | Effect | Setting (default) |
|---|---|---|---|
| L1 reflect | Streak reaches N | Tool runs; the model also sees its last calls and that it may stop and say what it checked. Once per streak. | `gemini_loop_guard_reflect_after` (3, 0 = off) |
| L2 dedupe | Exact repeat of a call already in the streak | Not re-run; the model gets a short factual result instead. | `gemini_loop_guard_dedupe` (on) |
| L3 backstop | Streak reaches N | Tool blocked so the model must answer. Last resort. | `gemini_loop_guard_block_after` (0 = off) |
| Variation reflect | N near-identical `grep`/shell calls within the last 8, **whatever they returned** | Tool runs; the model sees the look-alike calls and that it may answer now. Once per run of look-alikes; never blocks. | `gemini_loop_guard_variation_after` (3, 0 = off) |

The variation check exists because a model digging in the wrong place keeps
getting *something* back, so the streak never builds. Calls are compared after
stripping `cd`, env assignments, `source .../activate` and absolute-path
prefixes; two calls are look-alikes at token similarity >= 0.6. Calls naming
different files are progress, `read_file`/`list_files` are not watched, edits
reset the history, and `sleep` polling is exempt.

`/set gemini_loop_guard off` disables everything. Settings apply immediately.

## Known limits

- Reflection arrives one call late: `post_tool_call` cannot talk to the model,
  so the note rides on the next call's result.
- "Productive" is a heuristic over the tool's output shape. Tools with unusual
  output may need their own rules.
