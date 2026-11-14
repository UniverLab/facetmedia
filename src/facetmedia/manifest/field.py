"""Read and validate a capability input field's options.

``facetmedia.manifest`` keeps the package grammar; the field grammar lives
here so the manifest module stays inside the lab's per-file size cap, the
same split ``facetmedia.manifest.model`` makes for ``[[models]]``.  The
loader passed in is :class:`facetmedia.manifest.RegistryLoader`, which
collects every problem instead of stopping at the first one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from facetmedia.registry_types import is_multiple

if TYPE_CHECKING:
    from facetmedia.manifest import RegistryLoader

_FIELD_OPTION_TYPES: Final[Mapping[str, frozenset[str]]] = {
    "values": frozenset({"enum"}),
    "min": frozenset({"integer", "number"}),
    "max": frozenset({"integer", "number"}),
    "multiple_of": frozenset({"integer", "number"}),
    "pattern": frozenset({"string", "enum"}),
    "min_items": frozenset({"array"}),
}


def _default_type_matches(field_type: str, default: object) -> bool:
    """True when a scalar default has the type its field declares."""

    if field_type in {"string", "path"}:
        return isinstance(default, str)
    if field_type == "integer":
        return isinstance(default, int) and not isinstance(default, bool)
    if field_type == "number":
        return isinstance(default, int | float) and not isinstance(default, bool)
    if field_type == "boolean":
        return isinstance(default, bool)
    return True


def check_field_options(
    loader: RegistryLoader,
    field_type: str | None,
    spec: Mapping[str, Any],
    file: str,
    key: str,
) -> None:
    """Reject options that do not apply to the field's type.

    ``pattern`` is a regular expression: applying it to an integer would
    crash validation, so a manifest cannot attach it to a non-text field.
    """

    if field_type is None:
        return
    for option, allowed in _FIELD_OPTION_TYPES.items():
        if spec.get(option) is not None and field_type not in allowed:
            loader.add(file, f"{key}.{option}", "value")


def read_multiple_of(
    loader: RegistryLoader,
    spec: Mapping[str, Any],
    file: str,
    prefix: str,
    field_type: str | None,
) -> int | float | None:
    """The field's ``multiple_of`` divisor, or ``None`` when not declared."""

    raw = spec.get("multiple_of")
    if raw is None or field_type not in {"integer", "number"}:
        return None
    if field_type == "integer":
        well_typed = isinstance(raw, int) and not isinstance(raw, bool)
    else:
        well_typed = isinstance(raw, int | float) and not isinstance(raw, bool)
    if not well_typed:
        loader.add(file, f"{prefix}multiple_of", "type")
    elif raw <= 0:
        loader.add(file, f"{prefix}multiple_of", "value")
    else:
        divisor: int | float = raw
        return divisor
    return None


def check_default(
    loader: RegistryLoader,
    field_type: str,
    default: object,
    values: tuple[str, ...],
    file: str,
    key: str,
    multiple_of: int | float | None = None,
) -> None:
    if field_type == "array":
        if not isinstance(default, list):
            loader.add(file, key, "type")
    elif not _default_type_matches(field_type, default):
        loader.add(file, key, "type")
    elif _default_has_wrong_value(field_type, default, values, multiple_of):
        loader.add(file, key, "value")


def _default_has_wrong_value(
    field_type: str,
    default: object,
    values: tuple[str, ...],
    multiple_of: int | float | None,
) -> bool:
    """True when an enum default is unlisted or a numeric default is no multiple."""

    if field_type == "enum" and default not in values:
        return True
    return (
        multiple_of is not None
        and field_type in {"integer", "number"}
        and isinstance(default, int | float)
        and not isinstance(default, bool)
        and not is_multiple(default, multiple_of)
    )
