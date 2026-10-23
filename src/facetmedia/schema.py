"""Validate a piece against a capability's input schema.

The schema language lives in the registry manifest (``feat(registry)``); this
module turns it into validation and into JSON Schema draft 2020-12 for
``describe``.  It never imports ``jsonschema``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from facetmedia.contract import FacetError
from facetmedia.registry import Capability, Field

_INVALID: Final[object] = object()
_JSON_SCHEMA: Final[str] = "https://json-schema.org/draft/2020-12/schema"


def validate(cap: Capability, piece: Mapping[str, Any], piece_dir: Path) -> dict[str, Any]:
    """Return the piece's inputs with defaults filled and paths resolved.

    Every problem is collected; if there is at least one, raise
    ``FacetError("invalid_input")`` with ``details["errors"]``.
    """

    errors: list[dict[str, str]] = []
    values = _validate_object(cap.inputs, piece, "", Path(piece_dir), errors)
    if errors:
        raise FacetError(
            "invalid_input",
            f"invalid inputs for {cap.id}",
            details={"errors": errors},
        )
    return values


def _validate_object(
    fields: tuple[Field, ...],
    values: Mapping[str, Any],
    prefix: str,
    piece_dir: Path,
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    by_name = {field.name: field for field in fields}
    for name in values:
        if name not in by_name:
            errors.append({"field": f"{prefix}{name}", "problem": "unknown"})
    result: dict[str, Any] = {}
    for field in fields:
        path = f"{prefix}{field.name}"
        if field.name not in values:
            if field.has_default:
                result[field.name] = _default_value(field, piece_dir)
            elif field.required:
                errors.append({"field": path, "problem": "required"})
            continue
        checked = _check_field(field, values[field.name], path, piece_dir, errors)
        if checked is not _INVALID:
            result[field.name] = checked
    return result


def _check_field(
    field: Field,
    value: Any,
    path: str,
    piece_dir: Path,
    errors: list[dict[str, str]],
) -> Any:
    if field.type == "array":
        return _check_array(field, value, path, piece_dir, errors)
    checked = _check_scalar(field, value, path, piece_dir, errors)
    if checked is _INVALID:
        return _INVALID
    if field.type in {"integer", "number"} and field.min is not None and checked < field.min:
        errors.append({"field": path, "problem": "min"})
        return _INVALID
    if field.type in {"integer", "number"} and field.max is not None and checked > field.max:
        errors.append({"field": path, "problem": "max"})
        return _INVALID
    return checked


def _default_value(field: Field, piece_dir: Path) -> Any:
    if field.type == "path" and isinstance(field.default, str):
        return str((piece_dir / field.default).resolve())
    return field.default


def _check_path(value: Any, path: str, piece_dir: Path, errors: list[dict[str, str]]) -> Any:
    if not isinstance(value, str):
        errors.append({"field": path, "problem": "type"})
        return _INVALID
    resolved = (piece_dir / value).resolve()
    if not resolved.exists():
        errors.append({"field": path, "problem": "not_found"})
        return _INVALID
    return str(resolved)


def _check_scalar(
    field: Field, value: Any, path: str, piece_dir: Path, errors: list[dict[str, str]]
) -> Any:
    if field.type == "path":
        return _check_path(value, path, piece_dir, errors)
    if not _type_matches(field.type, value):
        errors.append({"field": path, "problem": "type"})
        return _INVALID
    if field.type == "enum" and value not in field.values:
        errors.append({"field": path, "problem": "enum"})
        return _INVALID
    if field.pattern is not None and re.fullmatch(field.pattern, value) is None:
        errors.append({"field": path, "problem": "pattern"})
        return _INVALID
    return value


def _type_matches(field_type: str, value: Any) -> bool:
    if field_type in {"string", "enum", "path"}:
        return isinstance(value, str)
    if field_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if field_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if field_type == "boolean":
        return isinstance(value, bool)
    return False


def _check_array(
    field: Field,
    value: Any,
    path: str,
    piece_dir: Path,
    errors: list[dict[str, str]],
) -> Any:
    if not isinstance(value, list):
        errors.append({"field": path, "problem": "type"})
        return _INVALID
    if field.min_items is not None and len(value) < field.min_items:
        errors.append({"field": path, "problem": "min_items"})
        return _INVALID
    items: list[dict[str, Any]] = []
    for position, item in enumerate(value):
        item_path = f"{path}[{position}]"
        if not isinstance(item, dict):
            errors.append({"field": item_path, "problem": "type"})
            continue
        items.append(_validate_object(field.items, item, f"{item_path}.", piece_dir, errors))
    return items


def to_json_schema(cap: Capability) -> dict[str, Any]:
    """The capability's inputs as JSON Schema draft 2020-12."""

    return {
        "$schema": _JSON_SCHEMA,
        "title": cap.id,
        "description": cap.summary,
        "type": "object",
        "properties": {field.name: _field_schema(field) for field in cap.inputs},
        "required": [field.name for field in cap.inputs if field.required],
        "additionalProperties": False,
    }


def _field_schema(field: Field) -> dict[str, Any]:
    schema = _type_schema(field)
    if field.has_default:
        schema["default"] = field.default
    if field.description:
        schema["description"] = field.description
    if field.min is not None:
        schema["minimum"] = field.min
    if field.max is not None:
        schema["maximum"] = field.max
    if field.pattern is not None:
        schema["pattern"] = field.pattern
    return schema


def _type_schema(field: Field) -> dict[str, Any]:
    if field.type == "enum":
        return {"type": "string", "enum": list(field.values)}
    if field.type == "path":
        return {"type": "string", "format": "path"}
    if field.type == "array":
        items = {
            "type": "object",
            "properties": {item.name: _field_schema(item) for item in field.items},
            "required": [item.name for item in field.items if item.required],
            "additionalProperties": False,
        }
        schema: dict[str, Any] = {"type": "array", "items": items}
        if field.min_items is not None:
            schema["minItems"] = field.min_items
        return schema
    return {"type": field.type}
