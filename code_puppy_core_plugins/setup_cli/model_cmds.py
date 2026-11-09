"""``setup model`` — model catalog management without the ``/add_model`` TUI.

Reuses the same builders/writers as the interactive flow
(``build_model_config`` + ``add_config_to_extra_config``) so entries
written here are indistinguishable from TUI-added ones.
"""

from __future__ import annotations

from typing import Any, Optional

from .output import Out, SetupError


def _catalog() -> dict[str, Any]:
    from code_puppy.model_factory import ModelFactory

    return ModelFactory.load_config()


def _get_registry():
    from code_puppy.models_dev_parser import ModelsDevRegistry

    return ModelsDevRegistry()


def cmd_model_list(out: Out) -> int:
    from code_puppy.config import get_value

    catalog = _catalog()
    default = (get_value("model") or "").strip()
    rows = []
    for key in sorted(catalog):
        cfg = catalog[key] if isinstance(catalog[key], dict) else {}
        rows.append(
            {
                "key": key,
                "type": cfg.get("type", ""),
                "provider": cfg.get("provider", ""),
                "name": cfg.get("name", ""),
                "default": key == default,
            }
        )
    human = [
        f"{'* ' if row['default'] else '  '}{row['key']}  [{row['type']}] {row['name']}"
        for row in rows
    ] or ["(no models configured)"]
    out.result({"models": rows, "default": default or None}, human=human)
    return 0


def cmd_model_catalog(out: Out, provider: Optional[str], search: Optional[str]) -> int:
    registry = _get_registry()
    providers = registry.get_providers()
    provider_list = (
        list(providers.values()) if isinstance(providers, dict) else list(providers)
    )
    if not provider:
        rows = [
            {"id": p.id, "name": p.name, "env": list(p.env or [])}
            for p in provider_list
        ]
        out.result(
            {"providers": rows, "source": getattr(registry, "data_source", "unknown")},
            human=[f"{row['id']}: {row['name']}" for row in rows],
        )
        return 0
    models = registry.get_models(provider)
    rows = []
    for model in models:
        if search and search.lower() not in model.model_id.lower():
            continue
        rows.append(
            {
                "provider": provider,
                "model_id": model.model_id,
                "name": model.name,
                "context_length": model.context_length,
            }
        )
    out.result(
        {"models": rows},
        human=[f"{provider}/{row['model_id']}" for row in rows]
        or ["(no matching models)"],
    )
    return 0


def _split_model_ref(ref: str) -> tuple[str, str]:
    if "/" in ref:
        provider_id, _, model_id = ref.partition("/")
    else:
        raise SetupError(
            "expected <provider>/<model-id>, e.g. anthropic/claude-haiku-5-5"
        )
    if not provider_id or not model_id:
        raise SetupError(
            "expected <provider>/<model-id>, e.g. anthropic/claude-haiku-5-5"
        )
    return provider_id, model_id


def cmd_model_add(out: Out, ref: str, set_default: bool) -> int:
    provider_id, model_id = _split_model_ref(ref)
    registry = _get_registry()
    providers = registry.get_providers()
    provider = (
        providers.get(provider_id)
        if isinstance(providers, dict)
        else next((p for p in providers if p.id == provider_id), None)
    )
    if provider is None:
        raise SetupError(f"unknown provider '{provider_id}' (see `model catalog`)")
    model = next(
        (m for m in registry.get_models(provider_id) if m.model_id == model_id), None
    )
    if model is None:
        raise SetupError(
            f"model '{model_id}' not found under provider '{provider_id}' "
            f"(see `model catalog {provider_id}`)"
        )

    from code_puppy.command_line.add_model_menu import (
        add_config_to_extra_config,
        build_model_config,
    )
    from code_puppy.models_dev_parser import extra_model_key

    key = extra_model_key(provider.id, model.model_id)
    existing = _catalog()
    added = add_config_to_extra_config(key, build_model_config(model, provider))
    if not added and key not in existing:
        raise SetupError(f"failed to add model '{key}'")
    if set_default:
        cmd_model_default(out, key)
    out.result(
        {"key": key, "added": key not in existing, "default": set_default},
        human=f"{'added' if key not in existing else 'already present'}: {key}",
    )
    return 0


def cmd_model_remove(out: Out, key: str) -> int:
    from code_puppy import atomic_json
    from code_puppy.config import EXTRA_MODELS_FILE

    removed = False

    def _mutate(data: Any) -> Any:
        nonlocal removed
        if isinstance(data, dict) and key in data:
            del data[key]
            removed = True
        return data

    atomic_json.mutate_json(EXTRA_MODELS_FILE, _mutate, default={})
    if not removed:
        if key in _catalog():
            raise SetupError(
                f"'{key}' is not in extra_models.json; OAuth/plugin models are "
                "removed via `setup auth logout`, bundled models cannot be removed"
            )
        raise SetupError(f"model '{key}' not found")
    out.result({"key": key, "removed": True}, human=f"removed: {key}")
    return 0


def cmd_model_default(out: Out, key: Optional[str]) -> int:
    from code_puppy.config import get_value, set_config_value

    if key is None:
        current = (get_value("model") or "").strip() or None
        out.result({"default": current}, human=f"default model: {current or '(auto)'}")
        return 0
    if key not in _catalog():
        raise SetupError(f"model '{key}' is not configured (see `setup model list`)")
    set_config_value("model", key)
    out.result({"default": key}, human=f"default model: {key}")
    return 0


def cmd_model_refresh(out: Out) -> int:
    from code_puppy.command_line.refresh_models import refresh_extra_models
    from code_puppy.config import EXTRA_MODELS_FILE

    report = refresh_extra_models(_get_registry(), EXTRA_MODELS_FILE)
    data = report if isinstance(report, dict) else {"report": str(report)}
    out.result(data, human=f"refreshed extra models: {data}")
    return 0
