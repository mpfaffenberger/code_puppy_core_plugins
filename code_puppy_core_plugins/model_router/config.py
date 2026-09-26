"""Settings for the Model router, stored in ``<DATA_DIR>/model_router.json``.

The file mirrors the ``routing`` block of jev-proxy but names code_puppy
models (the names shown by ``/model``), so nothing has to be registered twice.

Example::

    {
      "enabled": true,
      "virtual_model": "auto",
      "fallback_model": "copilot-claude-opus-4.6",
      "tiers": ["copilot-gpt-5-mini", "copilot-claude-sonnet-4.5", "copilot-claude-opus-4.6"],
      "complexity_to_tier": ["copilot-gpt-5-mini", "copilot-gpt-5-mini",
                             "copilot-claude-sonnet-4.5", "copilot-claude-opus-4.6"],
      "risk_floor": {"threshold": 0.7, "min_tier": "copilot-claude-sonnet-4.5"},
      "confidence_threshold": 0.6,
      "low_confidence": "next_level",
      "max_request_chars": 12000,
      "context_length": 200000,
      "judge": {"base_url": "https://api.typesafe.ai", "model": "jev-latest",
              "timeout_s": 4.0, "api_key_env": "TYPESAFE_API_KEY"}
    }
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

CONFIG_FILENAME = "model_router.json"
LOG_FILENAME = "model_routes.jsonl"

COMPLEXITY_LEVEL_COUNT = 4
DEFAULT_VIRTUAL_MODEL = "auto"
DEFAULT_JUDGE_BASE_URL = "https://api.typesafe.ai"
DEFAULT_JUDGE_MODEL = "jev-latest"
DEFAULT_API_KEY_ENV = "TYPESAFE_API_KEY"

# What to do when the judge is unsure about the complexity level.
LOW_CONFIDENCE_MODES = ("next_level", "fallback")
DEFAULT_LOW_CONFIDENCE = "next_level"


class RouterConfigError(ValueError):
    """The router config file is missing something or names an unknown model."""


def config_path() -> Path:
    from code_puppy import config

    return Path(config.DATA_DIR) / CONFIG_FILENAME


def log_path() -> Path:
    from code_puppy import config

    return Path(config.STATE_DIR) / LOG_FILENAME


@dataclass
class JudgeSettings:
    base_url: str = DEFAULT_JUDGE_BASE_URL
    model: str = DEFAULT_JUDGE_MODEL
    timeout_s: float = 4.0
    api_key_env: Optional[str] = DEFAULT_API_KEY_ENV

    def requires_key(self) -> bool:
        """The hosted TypeSafe endpoint needs a key; a local judge may not."""
        return bool(self.api_key_env) and self.base_url == DEFAULT_JUDGE_BASE_URL


@dataclass
class RiskFloor:
    threshold: float
    min_tier: str


@dataclass
class RouterConfig:
    fallback_model: str
    tiers: List[str]
    complexity_to_tier: List[str]
    enabled: bool = True
    virtual_model: str = DEFAULT_VIRTUAL_MODEL
    risk_floor: Optional[RiskFloor] = None
    confidence_threshold: float = 0.6
    low_confidence: str = DEFAULT_LOW_CONFIDENCE
    max_request_chars: int = 12000
    context_length: Optional[int] = None
    judge: JudgeSettings = field(default_factory=JudgeSettings)

    def tier_index(self, name: str) -> int:
        try:
            return self.tiers.index(name)
        except ValueError:
            return -1

    def referenced_models(self) -> List[str]:
        """Every model name the policy can return, in tier order, deduplicated."""
        seen: List[str] = []
        for name in [*self.tiers, *self.complexity_to_tier, self.fallback_model] + (
            [self.risk_floor.min_tier] if self.risk_floor else []
        ):
            if name not in seen:
                seen.append(name)
        return seen

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "enabled": self.enabled,
            "virtual_model": self.virtual_model,
            "fallback_model": self.fallback_model,
            "tiers": list(self.tiers),
            "complexity_to_tier": list(self.complexity_to_tier),
            "confidence_threshold": self.confidence_threshold,
            "low_confidence": self.low_confidence,
            "max_request_chars": self.max_request_chars,
            "judge": {
                "base_url": self.judge.base_url,
                "model": self.judge.model,
                "timeout_s": self.judge.timeout_s,
                "api_key_env": self.judge.api_key_env,
            },
        }
        if self.risk_floor:
            data["risk_floor"] = {
                "threshold": self.risk_floor.threshold,
                "min_tier": self.risk_floor.min_tier,
            }
        if self.context_length:
            data["context_length"] = self.context_length
        return data


def _require_str(data: Dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RouterConfigError(f"'{key}' must be a non-empty model name")
    return value.strip()


def _require_str_list(
    data: Dict[str, Any], key: str, length: int | None = None
) -> List[str]:
    value = data.get(key)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(v, str) and v.strip() for v in value)
    ):
        raise RouterConfigError(f"'{key}' must be a non-empty list of model names")
    if length is not None and len(value) != length:
        raise RouterConfigError(
            f"'{key}' must have exactly {length} entries (one per complexity level)"
        )
    return [v.strip() for v in value]


def parse_config(data: Dict[str, Any]) -> RouterConfig:
    """Validate a raw JSON object. Raises RouterConfigError with a readable message."""
    if not isinstance(data, dict):
        raise RouterConfigError("config must be a JSON object")

    tiers = _require_str_list(data, "tiers")
    fallback = _require_str(data, "fallback_model")
    complexity_to_tier = _require_str_list(
        data, "complexity_to_tier", COMPLEXITY_LEVEL_COUNT
    )

    risk_floor = None
    raw_floor = data.get("risk_floor")
    if raw_floor:
        if not isinstance(raw_floor, dict):
            raise RouterConfigError(
                "'risk_floor' must be an object with 'threshold' and 'min_tier'"
            )
        threshold = raw_floor.get("threshold", 0.7)
        if not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1:
            raise RouterConfigError("'risk_floor.threshold' must be between 0 and 1")
        risk_floor = RiskFloor(
            threshold=float(threshold), min_tier=_require_str(raw_floor, "min_tier")
        )

    for name in complexity_to_tier:
        if name not in tiers:
            raise RouterConfigError(
                f"'complexity_to_tier' names '{name}', which is not in 'tiers'"
            )
    if risk_floor and risk_floor.min_tier not in tiers:
        raise RouterConfigError(
            f"'risk_floor.min_tier' '{risk_floor.min_tier}' is not in 'tiers'"
        )

    confidence = data.get("confidence_threshold", 0.6)
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise RouterConfigError("'confidence_threshold' must be between 0 and 1")

    low_confidence = str(data.get("low_confidence") or DEFAULT_LOW_CONFIDENCE)
    if low_confidence not in LOW_CONFIDENCE_MODES:
        raise RouterConfigError(
            f"'low_confidence' must be one of {', '.join(LOW_CONFIDENCE_MODES)}"
        )

    max_chars = data.get("max_request_chars", 12000)
    if not isinstance(max_chars, int) or max_chars < 200:
        raise RouterConfigError(
            "'max_request_chars' must be an integer of at least 200"
        )

    context_length = data.get("context_length")
    if context_length is not None and (
        not isinstance(context_length, int) or context_length <= 0
    ):
        raise RouterConfigError("'context_length' must be a positive integer")

    raw_judge = data.get("judge") or {}
    if not isinstance(raw_judge, dict):
        raise RouterConfigError("'judge' must be an object")
    timeout = raw_judge.get("timeout_s", 4.0)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise RouterConfigError("'judge.timeout_s' must be a positive number")
    api_key_env_raw = raw_judge.get("api_key_env", DEFAULT_API_KEY_ENV)
    api_key_env = str(api_key_env_raw).strip() if api_key_env_raw else None
    judge = JudgeSettings(
        base_url=str(raw_judge.get("base_url") or DEFAULT_JUDGE_BASE_URL).rstrip("/"),
        model=str(raw_judge.get("model") or DEFAULT_JUDGE_MODEL),
        timeout_s=float(timeout),
        api_key_env=api_key_env,
    )

    virtual_model = str(data.get("virtual_model") or DEFAULT_VIRTUAL_MODEL).strip()
    if virtual_model in tiers or virtual_model == fallback:
        raise RouterConfigError(
            f"'{virtual_model}' is the virtual model and cannot also be a tier"
        )

    return RouterConfig(
        enabled=bool(data.get("enabled", True)),
        virtual_model=virtual_model,
        fallback_model=fallback,
        tiers=tiers,
        complexity_to_tier=complexity_to_tier,
        risk_floor=risk_floor,
        confidence_threshold=float(confidence),
        low_confidence=low_confidence,
        max_request_chars=max_chars,
        context_length=context_length,
        judge=judge,
    )


def load_config(path: Optional[Path] = None) -> Optional[RouterConfig]:
    """Return the parsed config, or None when the file does not exist."""
    path = path or config_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RouterConfigError(f"{path}: invalid JSON ({exc})") from exc
    return parse_config(data)


def save_config(cfg: RouterConfig, path: Optional[Path] = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path


def template_config(
    flagship: str, cheap: Optional[str] = None, mid: Optional[str] = None
) -> RouterConfig:
    """A starting config where every unspecified tier is the flagship."""
    cheap = cheap or mid or flagship
    mid = mid or flagship
    tiers: List[str] = []
    for name in (cheap, mid, flagship):
        if name not in tiers:
            tiers.append(name)
    return RouterConfig(
        fallback_model=flagship,
        tiers=tiers,
        complexity_to_tier=[cheap, cheap, mid, flagship],
        risk_floor=RiskFloor(threshold=0.7, min_tier=mid) if mid != cheap else None,
    )


def api_key_for(cfg: RouterConfig) -> Optional[str]:
    """Judge API key from code_puppy's config/secret store or the environment, or None."""
    if not cfg.judge.api_key_env:
        return None
    try:
        from code_puppy.model_factory import get_api_key

        value = get_api_key(cfg.judge.api_key_env)
    except Exception:
        value = None
    return value or os.environ.get(cfg.judge.api_key_env) or None
