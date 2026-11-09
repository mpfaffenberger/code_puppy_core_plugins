"""``setup config`` — puppy.cfg access without the interactive ``/set`` UI."""

from __future__ import annotations

from typing import Any

from .output import Out, SetupError, mask_value


def cmd_config_get(out: Out, key: str | None) -> int:
    from code_puppy.config import get_config_keys, get_value

    if key:
        value = get_value(key)
        out.result(
            {"key": key, "value": mask_value(key, value), "set": value is not None},
            human=f"{key} = {mask_value(key, value)}",
        )
        return 0
    data = {name: mask_value(name, get_value(name)) for name in get_config_keys()}
    out.result(data)
    return 0


def cmd_config_set(out: Out, key: str, value: str) -> int:
    from code_puppy.command_line.config_apply import apply_setting

    result = apply_setting(key, value, reload_agent=False)
    if not result.ok:
        raise SetupError(result.error or f"could not set '{key}'")
    out.result(
        {"key": key, "value": mask_value(key, value), "warning": result.warning},
        human=f"{key} = {mask_value(key, value)}"
        + (f"\n{result.warning}" if result.warning else ""),
    )
    return 0


def cmd_config_path(out: Out) -> int:
    from code_puppy import config as cp_config

    data: dict[str, Any] = {
        "config_file": cp_config.CONFIG_FILE,
        "config_dir": cp_config.CONFIG_DIR,
        "data_dir": cp_config.DATA_DIR,
        "mcp_servers_file": cp_config.MCP_SERVERS_FILE,
        "extra_models_file": cp_config.EXTRA_MODELS_FILE,
        "agents_dir": cp_config.AGENTS_DIR,
        "skills_dir": cp_config.SKILLS_DIR,
    }
    out.result(data)
    return 0
