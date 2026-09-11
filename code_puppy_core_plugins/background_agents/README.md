# Background agents

Handles `invoke_agent(agent_name, prompt, session_id=None, background=True)`
for the main agent in the persistent interactive CLI. Omitting `background`
or passing `False` preserves the existing foreground behavior.

- Returns `{task_id, agent_name, status: "running"}` immediately.
- Delivers the final response or error automatically; no polling tool needed.
- While the main agent runs, reports enter at a model-call boundary, like steer.
- After the main turn finishes, a report wakes the idle prompt for another turn.
- Reports are scoped to the original agent object. Switching agents does not
  deliver another agent's reports; switch back to process its pending reports.
- Reports are literal model input, not slash commands or attachment references.
- Existing delegation depth limits apply; nested background launches are rejected.
- Ctrl+C cancellation of active sub-agents produces a cancellation report.
- App shutdown cancels work. Jobs and pending reports are not restart-persistent.
- Requires a core with `code_puppy.agent_completion_inbox`; older cores do not
  advertise this tool. Headless, classic input, and ACP are not supported yet.

Install/release this plugin bundle together with the matching core changes.
