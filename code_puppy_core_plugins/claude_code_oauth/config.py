from pathlib import Path
from typing import Any, Dict

from code_puppy import config

# Claude Code OAuth configuration
CLAUDE_CODE_OAUTH_CONFIG: Dict[str, Any] = {
    # OAuth endpoints inferred from official Claude Code OAuth flow
    "auth_url": "https://claude.ai/oauth/authorize",
    # console.anthropic.com moved here; the old host 404s token POSTs.
    "token_url": "https://platform.claude.com/v1/oauth/token",
    "api_base_url": "https://api.anthropic.com",
    # OAuth client configuration observed in Claude Code CLI flow
    "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
    "scope": "org:create_api_key user:profile user:inference",
    # Callback handling (we host a localhost callback to capture the redirect)
    "redirect_host": "http://localhost",
    "redirect_path": "callback",
    "callback_port_range": (8765, 8795),
    "callback_timeout": 180,
    # Local configuration (uses XDG_DATA_HOME)
    "token_storage": None,  # Set dynamically in get_token_storage_path()
    # Model configuration
    "prefix": "claude-code-",
    "default_context_length": 200000,
    "long_context_length": 1000000,
    "long_context_models": [
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-opus-5",
        "claude-opus-5-5",
        "claude-sonnet-5",
        "claude-fable-5",
        "claude-fable-5-1",
    ],
    "api_key_env_var": "CLAUDE_CODE_ACCESS_TOKEN",
    "anthropic_version": "2023-06-01",
    "oauth_beta": "oauth-2025-04-20",
    # The token endpoint answers a misleading 429 "Rate limited" to any other
    # User-Agent, so every direct request must send this (see oauth_headers).
    "user_agent": "claude-cli/2.1.280 (external, cli)",
    # Per-family caps applied by filter_latest_claude_models. Shared by the
    # save path (add_models_to_extra_config) and the load path
    # (load_claude_models_filtered) so the two can never drift apart.
    "model_family_limits": {"default": 1, "opus": 3, "fable": 3},
}


def oauth_headers() -> Dict[str, str]:
    """Base headers for every direct request to Anthropic's OAuth/API endpoints."""
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "anthropic-beta": CLAUDE_CODE_OAUTH_CONFIG["oauth_beta"],
        "User-Agent": CLAUDE_CODE_OAUTH_CONFIG["user_agent"],
    }


def get_token_storage_path() -> Path:
    """Get the path for storing OAuth tokens (uses XDG_DATA_HOME)."""
    data_dir = Path(config.DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return data_dir / "claude_code_oauth.json"


def get_config_dir() -> Path:
    """Get the Code Puppy configuration directory (uses XDG_CONFIG_HOME)."""
    config_dir = Path(config.CONFIG_DIR)
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return config_dir


def get_claude_models_path() -> Path:
    """Get the path to the dedicated claude_models.json file (uses XDG_DATA_HOME)."""
    data_dir = Path(config.DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return data_dir / "claude_models.json"
