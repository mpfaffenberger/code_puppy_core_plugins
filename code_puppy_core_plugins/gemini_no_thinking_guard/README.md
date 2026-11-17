# gemini_no_thinking_guard

Stops Gemini Flash from looping on lookups during turns where it did not think.

Gemini Flash decides per user turn whether to think, even though Code Puppy
asks for thinking on every request. It tends to skip thinking when the context
at the start of a turn is large or already covers the question's topic. That is
typical of later turns in a session, after docs or earlier answers about the
same subject. Without thinking, Flash investigates by trial and error. When
data-store results contradict what a doc says, it can keep querying for 100 or
more tool calls instead of answering. Turns where Flash does think are not
affected.

This plugin only acts when all of these hold: the model name contains `gemini`
and `flash`, the call is inside an agent run, and no thinking part has appeared
in the current turn. Every other model and every thinking turn is untouched.

## What it does (`pre_tool_call`)

| Step | When | Effect | Setting (default) |
|---|---|---|---|
| Step back | Call N+1 since the last edit, once per stretch | That call is not run. The model is told to write out the exact lookup the code makes, the qualifier/org/environment of every row it relies on (from a query result it actually saw), and the answer. An empty exact lookup means "not set where the code reads it" plus the code's fallback, and doc conflicts are reported rather than investigated. | `gemini_no_thinking_guard_step_back_after` (12, 0 = off) |
| Answer now | Every call past N since the last edit | Blocked. The model must answer from what it already has. | `gemini_no_thinking_guard_force_answer_after` (24, 0 = off) |

File edits (`edit_file`, `create_file`, `replace_in_file`, `delete_file`,
`delete_snippet`) reset the count, so a turn that is making changes is never
stopped. Only long stretches of reads, searches and queries count.

`/set gemini_no_thinking_guard off` disables it. Settings apply immediately.

## Known limits

- Thinking is detected from thinking parts in the message history. A provider
  path that drops thought parts before they reach history would make every turn
  look like a no-thinking turn.
- The step-back wording targets "what is this setting / where does it come from"
  investigations, which is the loop it was measured on. Other kinds of long
  read-only exploration are capped too, at 24 calls since the last edit by
  default.
