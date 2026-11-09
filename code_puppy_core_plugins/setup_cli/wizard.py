"""``setup wizard`` — guided first-run setup over the primitive commands.

Plain-text prompts on purpose: the wizard must work over SSH and
``sbx exec``, where no TUI exists. Every step is skippable, and
``--provider``/``--model`` make the whole run scriptable.
"""

from __future__ import annotations

from typing import Optional

from . import auth_cmds, doctor, providers
from .model_cmds import _catalog, cmd_model_default
from .output import Out


def _ask(prompt: str, default: str = "") -> str:
    try:
        answer = input(prompt).strip()
    except EOFError:
        return default
    return answer or default


def _choose_provider(preferred: Optional[str]) -> Optional[str]:
    if preferred:
        return preferred
    status = providers.all_provider_status()
    options: list[tuple[str, str]] = []
    for entry in status.oauth:
        mark = "authenticated" if entry["authenticated"] else "not authenticated"
        options.append((entry["id"], f"{entry['display']} ({mark})"))
    options.append(("api-key", "Plain API key (choose variable next)"))
    print("Choose a model provider:")
    for index, (_, label) in enumerate(options, 1):
        print(f"  {index}. {label}")
    choice = _ask("Provider number (blank to skip): ")
    if not choice.isdigit() or not (1 <= int(choice) <= len(options)):
        return None
    return options[int(choice) - 1][0]


def run_wizard(out: Out, provider_id: Optional[str], model_key: Optional[str]) -> int:
    from code_puppy.config import get_value, set_config_value

    print("Code Puppy setup wizard")
    for key, label in (("puppy_name", "Puppy name"), ("owner_name", "Your name")):
        if not (get_value(key) or "").strip():
            value = _ask(f"{label}: ")
            if value:
                set_config_value(key, value)

    chosen = _choose_provider(provider_id)
    if chosen == "api-key":
        env_var = _ask(
            "API key variable (e.g. ANTHROPIC_API_KEY): ", "ANTHROPIC_API_KEY"
        ).upper()
        if not providers.credential_present(env_var):
            auth_cmds.cmd_auth_login(out, env_var, from_env=None, stdin=False)
    elif chosen:
        provider = providers.get_oauth_provider(chosen)
        entry = providers.oauth_status(provider) if provider else {}
        if not entry.get("authenticated"):
            print(f"Launching the {chosen} sign-in flow...")
            auth_cmds.cmd_auth_login(out, chosen, from_env=None, stdin=False)

    if model_key:
        cmd_model_default(out, model_key)
    else:
        catalog = _catalog()
        if catalog:
            keys = sorted(catalog)
            print("Configured models:")
            for index, key in enumerate(keys, 1):
                print(f"  {index}. {key}")
            choice = _ask("Default model number (blank to keep current): ")
            if choice.isdigit() and 1 <= int(choice) <= len(keys):
                cmd_model_default(out, keys[int(choice) - 1])
        else:
            print(
                "No models configured yet. After authenticating, add one with "
                "`code-puppy --setup model add <provider>/<model-id>`."
            )

    report, code = doctor.run_doctor()
    out.result(report, human=doctor.render_human(report))
    return code
