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
    list_fields: dict[str, _ModelContract]
    model_config: dict[str, Any]
    model_config_values: dict[str, Any]
    decorators: object
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


def _field_is_plain(model_field: Any) -> bool:
    return (
        model_field.alias is None
        and model_field.serialization_alias is None
        and model_field.validation_alias is None
        and model_field.exclude in (None, False)
        and getattr(model_field, "exclude_if", None) is None
        and not model_field.metadata
    )


def _field_runtime_types(model_field: Any) -> tuple[type, ...] | None:
    runtime_types = _annotation_runtime_types(model_field.annotation)
    return (
        runtime_types
        if runtime_types is not None and _field_is_plain(model_field)
        else None
    )


def _optional_list_item_model(model_field: Any) -> type[BaseModel] | None:
    if not _field_is_plain(model_field) or model_field.is_required():
        return None
    annotation = model_field.annotation
    if get_origin(annotation) in (Union, UnionType):
        members = tuple(item for item in get_args(annotation) if item is not NoneType)
        if len(members) != 1 or len(members) == len(get_args(annotation)):
            return None
        annotation = members[0]
    if get_origin(annotation) is not list:
        return None
    item_args = get_args(annotation)
    if len(item_args) != 1:
        return None
    item_type = item_args[0]
    if type(item_type) is not type(BaseModel) or not issubclass(item_type, BaseModel):
        return None
    return item_type


def _build_model_contract(
    model_type: type[BaseModel],
    *,
    allow_config: bool = False,
    allow_lists: bool = True,
) -> _ModelContract | None:
    if type(model_type) is not type(BaseModel):
        return None
    if (
        model_type.__setattr__ is not _BASE_SETATTR
        or model_type.__getattribute__ is not _BASE_GETATTRIBUTE
        or model_type.model_dump is not _BASE_MODEL_DUMP
        or model_type.model_validate.__func__ is not _BASE_MODEL_VALIDATE
        or type(model_type.model_config) is not dict
        or (bool(model_type.model_config) and not allow_config)
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
    list_fields: dict[str, _ModelContract] = {}
    for name, model_field in model_type.model_fields.items():
        if type(name) is not str:
            return None
        runtime_types = _field_runtime_types(model_field)
        if runtime_types is None:
            if not allow_lists:
                return None
            item_type = _optional_list_item_model(model_field)
            if item_type is None:
                return None
            item_contract = _build_model_contract(
                item_type,
                allow_config=True,
                allow_lists=False,
            )
            if item_contract is None:
                return None
            runtime_types = (list, NoneType)
            list_fields[name] = item_contract
        field_types[name] = runtime_types

    return _ModelContract(
        model_type=model_type,
        serializer=model_type.__pydantic_serializer__,
        validator=model_type.__pydantic_validator__,
        field_types=field_types,
        field_objects=dict(model_type.model_fields),
        list_fields=list_fields,
        model_config=model_type.model_config,
        model_config_values=dict.copy(model_type.model_config),
        decorators=decorators,
        getattribute_method=model_type.__getattribute__,
        setattr_method=model_type.__setattr__,
        model_dump_method=model_type.model_dump,
        model_validate_method=model_type.model_validate.__func__,
    )


def _load_model_contracts() -> tuple[_ModelContract, ...]:
    contracts: list[_ModelContract] = []
    for model_type in _candidate_builtin_models():
        contract = _build_model_contract(model_type)
        if contract is not None:
            contracts.append(contract)
    return tuple(contracts)


_MODEL_CONTRACTS = _load_model_contracts()


def _contract_for_type(model_type: type) -> _ModelContract | None:
    return next(
        (
            contract
            for contract in _MODEL_CONTRACTS
            if model_type is contract.model_type
        ),
        None,
    )


def _safe_exact_string_keys(mapping: dict) -> tuple[str, ...] | None:
    keys: list[str] = []
    for key in dict.__iter__(mapping):
        if type(key) is not str:
            return None
        keys.append(key)
    return tuple(keys)


def _same_field_names(names: tuple[str, ...], contract: _ModelContract) -> bool:
    return len(names) == len(contract.field_types) and all(
        name in contract.field_types for name in names
    )


def _same_model_config(contract: _ModelContract) -> bool:
    current = contract.model_type.model_config
    if type(current) is not dict or current is not contract.model_config:
        return False
    names = _safe_exact_string_keys(current)
    if names is None or len(names) != len(contract.model_config_values):
        return False
    for name in names:
        if name not in contract.model_config_values:
            return False
        value = dict.__getitem__(current, name)
        expected = dict.__getitem__(contract.model_config_values, name)
        if type(value) is not type(expected):
            return False
        if type(value) in _SAFE_SCALAR_TYPES + (NoneType,) and value != expected:
            return False
        if (
            type(value) not in _SAFE_SCALAR_TYPES + (NoneType,)
            and value is not expected
        ):
            return False
    return True


def _field_matches_contract(
    name: str,
    model_field: Any,
    contract: _ModelContract,
) -> bool:
    nested = contract.list_fields.get(name)
    if nested is None:
        return _field_runtime_types(model_field) == contract.field_types[name]
    return _optional_list_item_model(
        model_field
    ) is nested.model_type and _model_type_matches_contract(nested)


def _model_type_matches_contract(contract: _ModelContract) -> bool:
    model_type = contract.model_type
    try:
        model_fields = model_type.model_fields
        if type(model_fields) is not dict:
            return False
        field_names = _safe_exact_string_keys(model_fields)
        if field_names is None:
            return False
        decorators = model_type.__pydantic_decorators__
        return not (
            not _same_model_config(contract)
            or model_type.__getattribute__ is not contract.getattribute_method
            or model_type.__setattr__ is not contract.setattr_method
            or decorators is not contract.decorators
            or model_type.__pydantic_serializer__ is not contract.serializer
            or model_type.__pydantic_validator__ is not contract.validator
            or model_type.model_dump is not contract.model_dump_method
            or model_type.model_validate.__func__ is not contract.model_validate_method
            or not _same_field_names(field_names, contract)
            or any(
                dict.__getitem__(model_fields, name) is not field
                for name, field in contract.field_objects.items()
            )
            or any(
                not _field_matches_contract(name, field, contract)
                for name, field in contract.field_objects.items()
            )
            or any(
                getattr(contract.decorators, group)
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
        )
    except Exception:
        return False


def _model_contract(result: BaseModel) -> _ModelContract | None:
    contract = _contract_for_type(type(result))
    return (
        contract
        if contract is not None and _model_type_matches_contract(contract)
        else None
    )


def _field_value_is_safe(
    name: str,
    value: Any,
    contract: _ModelContract,
) -> bool:
    nested = contract.list_fields.get(name)
    if nested is None:
        return type(value) in contract.field_types[name]
    if value is None:
        return True
    return type(value) is list and all(
        type(item) is nested.model_type
        and _model_type_matches_contract(nested)
        and _model_instance_is_safe(item, nested)
        for item in list.__iter__(value)
    )


def _model_instance_is_safe(
    result: BaseModel,
    contract: _ModelContract | None = None,
) -> bool:
    if contract is None:
        contract = _model_contract(result)
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
    raw_names = _safe_exact_string_keys(raw_values)
    if raw_names is None or not _same_field_names(raw_names, contract):
        return False
    return all(
        _field_value_is_safe(
            name,
            dict.__getitem__(raw_values, name),
            contract,
        )
        for name in contract.field_types
    )


def _contains_string_subclass(mapping: dict[Any, Any]) -> bool:
    for value in dict.values(mapping):
        value_type = type(value)
        if value_type is not str and issubclass(value_type, str):
            return True
    return False


def model_facing_mapping(result: Any) -> dict[str, Any] | None:
    """Return the safely mutable top-level mapping sent to the model."""
    if type(result) is dict:
        if any(type(key) is not str for key in result):
            return None
        if _contains_string_subclass(result):
            return None
        return dict(result)
    contract = _contract_for_type(type(result))
    if contract is None:
        return None
    contract = _model_contract(result)
    if contract is None or not _model_instance_is_safe(result, contract):
        return None

    try:
        raw_values = object.__getattribute__(result, "__dict__")
        return {
            name: dict.__getitem__(raw_values, name)
            for name in contract.field_types
            if name not in contract.list_fields
        }
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
        if _contains_string_subclass(result):
            return None
        return {key: value for key, value in result.items() if type(value) is str}
    contract = _model_contract(result)
    if contract is None or not _model_instance_is_safe(result, contract):
        return None
    raw_values = object.__getattribute__(result, "__dict__")
    return {key: value for key, value in dict.items(raw_values) if type(value) is str}


def model_validation_spec(result: Any) -> ModelValidationSpec | None:
    contract = _model_contract(result)
    if contract is None or not _model_instance_is_safe(result, contract):
        return None
    raw_values = object.__getattribute__(result, "__dict__")
    values = {
        name: dict.__getitem__(raw_values, name)
        for name in contract.field_types
        if name not in contract.list_fields
    }
    return ModelValidationSpec(type(result), values)


def validate_model_replacements(
    spec: ModelValidationSpec,
    replacements: dict[str, str],
    cap: int,
) -> bool:
    """Validate a complete isolated candidate without touching the live result."""
    contract = _contract_for_type(spec.model_type)
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
    contract = _contract_for_type(type(result))
    if contract is None:
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
        or _contract_for_type(type(result)) is None
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
