# mcp_binding_prompt

Three MCP-install affordances layered over core at `startup`, no core edits:

1. **Synchronous post-install binding prompt.** Core's installers call
   `mcp_binding_menu.prompt_bind_after_install_sync(server_name)` inside a
   `try/except ImportError` -- without this plugin the call silently no-ops.
   `register_callbacks.py` provides that function: it runs the existing
   `build_post_install_menu` inline in the installer's foreground (inside
   `menu_session()`, with `set_awaiting_user_input` toggled) instead of
   scheduling a competing UI task.
2. **`/mcp install` is custom-JSON only** (`custom_install.py`). The bundled
   marketplace is gone: `InstallCommand.execute` opens the custom server form
   directly, `SearchCommand.execute` explains the removal, `HelpCommand.execute`
   prints the trimmed help, and `MCPCompleter` stops advertising `search`.
3. **HTTP form fields** (`http_form.py`). When the custom form's type is
   `http`, `run_form_flow`'s `menu_factory` is swapped for `HTTPMenu`, which
   exposes URL / auth (none, OAuth, headers) / headers as proper fields on
   top of the raw JSON blob. Saving validates the URL (scheme, host, no
   embedded credentials or whitespace), the headers (string keys/values, no
   CRLF), and runs the config through core's `http_auth` transport policy.
   Malformed advanced JSON falls back to the core menu so it stays editable.
   Switching type to `http` with an untouched example seeds a minimal config.

All strings go through `code_puppy.i18n` (`mcp.custom_install.*`,
`mcp.http_form.*`); the keys live in core's `en-US` catalog.

## Notes

- Every patch is idempotent (`_custom_json_install` / `_http_fields` markers).
- `HelpCommand.execute` is fully replaced, so new core `/mcp` subcommands must
  also be added to the `mcp.custom_install.help` catalog string.
