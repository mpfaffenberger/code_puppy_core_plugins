"""Conservative mutable result-shape support for the spill plugin."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import NoneType, UnionType
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel

logger = logging.getLogger(__name__)

_SAFE_SCALAR_TYPES = (str, bool, int, float)
_BASE_GETATTRIBUTE = BaseModel.__getattribute__
_BASE_SETATTR = BaseModel.__setattr__
_BASE_MODEL_DUMP = BaseModel.model_dump
_BASE_MODEL_VALIDATE = BaseModel.model_validate.__func__


@dataclass(frozen=True)
class ModelValidationSpec:
    model_type: type[BaseModel]
    values: dict[str, Any]


@dataclass(frozen=True)
class _ModelContract:
    model_type: type[BaseModel]
    serializer: object
    validator: object
    field_types: dict[str, tuple[type, ...]]
    field_objects: dict[str, object]
    model_config: dict[str, Any]
    getattribute_method: object
    setattr_method: object
    model_dump_method: object
    model_validate_method: object


def byte_size(text: str) -> int:
    return len(str.encode(text, "utf-8"))


def _annotation_runtime_types(annotation: Any) -> tuple[type, ...] | None:
    if any(annotation is allowed for allowed in (*_SAFE_SCALAR_TYPES, NoneType)):
        return (annotation,)
    origin = get_origin(annotation)
    if origin is not Union and origin is not UnionType:
        return None
    resolved: list[type] = []
    for item in get_args(annotation):
        item_types = _annotation_runtime_types(item)
        if item_types is None:
            return None
        resolved.extend(item_types)
    return tuple(dict.fromkeys(resolved))


def _candidate_builtin_models() -> tuple[type[BaseModel], ...]:
    candidates: list[type[BaseModel]] = []
    try:
        from code_puppy.tools import agent_tools

        for name in ("AgentInvokeOutput", "AgentInvokeWithModelOutput"):
            model_type = getattr(agent_tools, name, None)
            if isinstance(model_type, type) and issubclass(model_type, BaseModel):
                candidates.append(model_type)
    except ImportError:
        pass
    try:
        from code_puppy.tools.command_runner import ShellCommandOutput

        candidates.append(ShellCommandOutput)
    except (ImportError, AttributeError):
        pass
    try:
        from code_puppy.tools.file_operations import ListFileOutput

        candidates.append(ListFileOutput)
    except (ImportError, AttributeError):
        pass
    return tuple(candidates)


def _field_runtime_types(model_field: Any) -> tuple[type, ...] | None:
    runtime_types = _annotation_runtime_types(model_field.annotation)
    if (
        runtime_types is None
        or model_field.alias is not None
        or model_field.serialization_alias is not None
        or model_field.validation_alias is not None
        or model_field.exclude not in (None, False)
        or getattr(model_field, "exclude_if", None) is not None
        or model_field.metadata
    ):
        return None
    return runtime_types


def _build_model_contract(model_type: type[BaseModel]) -> _ModelContract | None:
    if type(model_type) is not type(BaseModel):
        return None
    if (
        model_type.__setattr__ is not _BASE_SETATTR
        or model_type.__getattribute__ is not _BASE_GETATTRIBUTE
        or model_type.model_dump is not _BASE_MODEL_DUMP
        or model_type.model_validate.__func__ is not _BASE_MODEL_VALIDATE
        or type(model_type.model_config) is not dict
        or bool(model_type.model_config)
    ):
        return None
    decorators = model_type.__pydantic_decorators__
    if any(
        getattr(decorators, group)
        for group in (
            "validators",
            "field_validators",
            "root_validators",
            "field_serializers",
            "model_serializers",
            "model_validators",
            "computed_fields",
        )
    ):
        return None

    field_types: dict[str, tuple[type, ...]] = {}
    for name, model_field in model_type.model_fields.items():
        runtime_types = _field_runtime_types(model_field)
        if runtime_types is None or type(name) is not str:
            return None
        field_types[name] = runtime_types

    return _ModelContract(
        model_type=model_type,
        serializer=model_type.__pydantic_serializer__,
        validator=model_type.__pydantic_validator__,
        field_types=field_types,
        field_objects=dict(model_type.model_fields),
        model_config=model_type.model_config,
        getattribute_method=model_type.__getattribute__,
        setattr_method=model_type.__setattr__,
        model_dump_method=model_type.model_dump,
        model_validate_method=model_type.model_validate.__func__,
    )


def _load_model_contracts() -> dict[type[BaseModel], _ModelContract]:
    contracts: dict[type[BaseModel], _ModelContract] = {}
    for model_type in _candidate_builtin_models():
        contract = _build_model_contract(model_type)
        if contract is not None:
            contracts[model_type] = contract
    return contracts


_MODEL_CONTRACTS = _load_model_contracts()


def _model_contract(result: BaseModel) -> _ModelContract | None:
    model_type = type(result)
    contract = _MODEL_CONTRACTS.get(model_type)
    if contract is None:
        return None
    try:
        model_fields = model_type.model_fields
        decorators = model_type.__pydantic_decorators__
        if (
            type(model_fields) is not dict
            or type(model_type.model_config) is not dict
            or model_type.model_config is not contract.model_config
            or bool(model_type.model_config)
            or model_type.__getattribute__ is not contract.getattribute_method
            or model_type.__setattr__ is not contract.setattr_method
            or model_type.__pydantic_serializer__ is not contract.serializer
            or model_type.__pydantic_validator__ is not contract.validator
            or model_type.model_dump is not contract.model_dump_method
            or model_type.model_validate.__func__ is not contract.model_validate_method
            or set(model_fields) != set(contract.field_types)
            or any(
                model_fields[name] is not field
                for name, field in contract.field_objects.items()
            )
            or any(
                _field_runtime_types(field) != contract.field_types[name]
                for name, field in contract.field_objects.items()
            )
            or any(
                getattr(decorators, group)
                for group in (
                    "validators",
                    "field_validators",
                    "root_validators",
                    "field_serializers",
                    "model_serializers",
                    "model_validators",
                    "computed_fields",
                )
            )
        ):
            return None
    except Exception:
        return None
    return contract


def _model_instance_is_safe(
    result: BaseModel,
    contract: _ModelContract | None = None,
) -> bool:
    contract = contract or _model_contract(result)
    if contract is None:
        return False
    try:
        raw_values = object.__getattribute__(result, "__dict__")
        extra = object.__getattribute__(result, "__pydantic_extra__")
        fields_set = object.__getattribute__(result, "__pydantic_fields_set__")
        private = object.__getattribute__(result, "__pydantic_private__")
    except Exception:
        return False
    if (
        type(raw_values) is not dict
        or extra is not None
        or type(fields_set) is not set
        or private is not None
        or any(
            type(name) is not str or name not in contract.field_types
            for name in set.__iter__(fields_set)
        )
    ):
        return False
    if set(dict.keys(raw_values)) != set(contract.field_types):
        return False
    return all(
        type(dict.__getitem__(raw_values, name)) in allowed_types
        for name, allowed_types in contract.field_types.items()
    )


def model_facing_mapping(result: Any) -> dict[str, Any] | None:
    """Return the safely mutable top-level mapping sent to the model."""
    if type(result) is dict:
        if any(type(key) is not str for key in result):
            return None
        if any(
            isinstance(value, str) and type(value) is not str
            for value in result.values()
        ):
            return None
        return dict(result)
    if type(result) not in _MODEL_CONTRACTS:
        return None
    contract = _model_contract(result)
    if contract is None or not _model_instance_is_safe(result, contract):
        return None

    try:
        raw_values = object.__getattribute__(result, "__dict__")
        serialized = contract.serializer.to_python(
            result,
            mode="json",
            warnings="error",
        )
        if type(serialized) is not dict:
            return None
        if set(serialized) != set(contract.field_types):
            return None
        for field_name, serialized_value in dict.items(serialized):
            raw_value = dict.__getitem__(raw_values, field_name)
            if type(serialized_value) is str and (
                type(raw_value) is not str or raw_value != serialized_value
            ):
                return None
        return serialized
    except Exception as exc:
        logger.debug(
            "Cannot inspect built-in Pydantic result %s (%s)",
            type(result).__name__,
            type(exc).__name__,
        )
        return None


def string_fields(mapping: dict[str, Any]) -> list[tuple[str, str, int]]:
    return [
        (key, value, byte_size(value))
        for key, value in mapping.items()
        if type(value) is str
    ]


def string_total(mapping: dict[str, Any]) -> int:
    return sum(size for _, _, size in string_fields(mapping))


def string_snapshot(result: Any) -> dict[str, str] | None:
    """Read current top-level strings without invoking model serialization."""
    if type(result) is dict:
        if any(type(key) is not str for key in result):
            return None
        if any(
            isinstance(value, str) and type(value) is not str
            for value in result.values()
        ):
            return None
        return {key: value for key, value in result.items() if type(value) is str}
    if type(result) not in _MODEL_CONTRACTS or not _model_instance_is_safe(result):
        return None
    raw_values = object.__getattribute__(result, "__dict__")
    return {key: value for key, value in dict.items(raw_values) if type(value) is str}


def model_validation_spec(result: Any) -> ModelValidationSpec | None:
    if type(result) not in _MODEL_CONTRACTS or not _model_instance_is_safe(result):
        return None
    raw_values = object.__getattribute__(result, "__dict__")
    return ModelValidationSpec(type(result), dict.copy(raw_values))


def validate_model_replacements(
    spec: ModelValidationSpec,
    replacements: dict[str, str],
    cap: int,
) -> bool:
    """Validate a complete isolated candidate without touching the live result."""
    contract = _MODEL_CONTRACTS.get(spec.model_type)
    if contract is None:
        return False
    try:
        candidate_input = spec.values.copy()
        candidate_input.update(replacements)
        candidate = contract.validator.validate_python(candidate_input)
        candidate_mapping = model_facing_mapping(candidate)
        return (
            candidate_mapping is not None
            and string_total(candidate_mapping) <= cap
            and all(
                candidate_mapping.get(field_name) == replacement
                for field_name, replacement in replacements.items()
            )
        )
    except Exception as exc:
        logger.warning(
            "Cannot validate built-in Pydantic spill result %s (%s)",
            spec.model_type.__name__,
            type(exc).__name__,
        )
        return False


def result_accepts_fields(result: Any, field_names: tuple[str, ...]) -> bool:
    if type(result) is dict:
        return all(type(name) is str for name in field_names)
    if type(result) not in _MODEL_CONTRACTS:
        return False
    contract = _model_contract(result)
    if contract is None or not _model_instance_is_safe(result, contract):
        return False
    return all(
        name in contract.field_objects and not contract.field_objects[name].frozen
        for name in field_names
    )


def commit_replacements(
    result: Any,
    replacements: dict[str, str],
    expected_strings: dict[str, str],
    *,
    model_validated: bool = False,
) -> bool:
    """Atomically commit current dict or prevalidated built-in model fields."""
    if string_snapshot(result) != expected_strings:
        return False
    if type(result) is dict:
        original = result.copy()
        try:
            dict.update(result, replacements)
        except Exception:
            dict.clear(result)
            dict.update(result, original)
            return False
        return True
    if (
        not model_validated
        or type(result) not in _MODEL_CONTRACTS
        or not result_accepts_fields(result, tuple(replacements))
        or string_snapshot(result) != expected_strings
    ):
        return False

    original_dict = object.__getattribute__(result, "__dict__")
    committed_dict = original_dict.copy()
    committed_dict.update(replacements)
    try:
        object.__setattr__(result, "__dict__", committed_dict)
    except Exception as exc:
        object.__setattr__(result, "__dict__", original_dict)
        logger.warning(
            "Cannot commit built-in Pydantic spill result %s (%s)",
            type(result).__name__,
            type(exc).__name__,
        )
        return False
    return True


__all__ = [
    "ModelValidationSpec",
    "byte_size",
    "commit_replacements",
    "model_facing_mapping",
    "model_validation_spec",
    "result_accepts_fields",
    "string_fields",
    "string_snapshot",
    "string_total",
    "validate_model_replacements",
]
