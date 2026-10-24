"""``facet list`` and ``facet describe``: discovery without a lock.

An agent learns what facet can do by asking it, not by importing a server:
``facet list`` names the packages and their capabilities, ``facet describe``
gives one capability's input schema, verification thresholds, requirements,
licences and download size.  Both read the registry snapshot and the home's
install state only; neither takes the facet lock nor downloads anything.
"""

from __future__ import annotations

import json
import math
from typing import Any

from facetmedia import check, env, licenses, schema
from facetmedia.contract import CONTRACT_VERSION
from facetmedia.home import Home
from facetmedia.registry import load_active_registry, snapshot_identity
from facetmedia.registry_types import Capability, Field, Model, Package

__all__ = ["describe_data", "list_data"]


def list_data(available: bool) -> dict[str, Any]:
    """The ``facet list`` payload: the snapshot in use and its packages.

    Only installed packages are listed unless ``available`` asks for every
    package in the registry snapshot, compatible or not.
    """
    active = load_active_registry()
    home = Home.peek_env()
    rows = [
        _package_row(package, home)
        for package in sorted(active.packages.values(), key=lambda item: item.name)
    ]
    if not available:
        rows = [row for row in rows if row["installed"] is True]
    return {"registry": snapshot_identity(), "packages": rows}


def _package_row(package: Package, home: Home) -> dict[str, Any]:
    installed = env.is_installed(package, home)
    env_dir = home.env_dir(package.name, package.version)
    return {
        "name": package.name,
        "version": package.version,
        "tool_name": package.tool_name,
        "provider": package.provider,
        "compatible": package.compatible,
        "installed": installed,
        "env_bytes": check.tree_bytes(env_dir) if installed else 0,
        "models_bytes": sum(
            model.bytes for model in package.models if env.model_present(model, home)
        ),
        "capabilities": [
            {"id": cap.id, "summary": cap.summary, "kind": cap.kind}
            for cap in sorted(package.capabilities, key=lambda item: item.id)
        ],
    }


def describe_data(capability_id: str) -> dict[str, Any]:
    """The ``facet describe`` payload for one capability id.

    The contract version appears here as well as in the envelope because
    clients cache ``describe`` output on its own.
    """
    capability, package = check.resolve(capability_id)
    home = Home.peek_env()
    installed = env.is_installed(package, home)
    accepted = licenses.accepted_models(package.models)
    return {
        "contract": CONTRACT_VERSION,
        "capability": capability.id,
        "summary": capability.summary,
        "kind": capability.kind,
        "package": {
            "name": package.name,
            "version": package.version,
            "tool_name": package.tool_name,
            "provider": package.provider,
        },
        "inputs": schema.to_json_schema(capability),
        "piece_example": _piece_example(capability),
        "verify": {"thresholds": dict(sorted(capability.thresholds.items()))},
        "preview": {"viewer": capability.viewer},
        "requirements": _requirements(package),
        "licenses": {
            "tools": [{"subject": lic.subject, "spdx": lic.spdx} for lic in package.licenses],
            "models": [_model_row(model, accepted) for model in package.models],
        },
        "download_bytes": _download_bytes(package, home, installed),
        "installed": installed,
    }


def _requirements(package: Package) -> dict[str, Any]:
    requirements = package.requirements
    return {
        "gpu": requirements.gpu,
        "ram_gb": requirements.ram_gb,
        "disk_gb": requirements.disk_gb,
        "exclusive": requirements.exclusive,
        "platforms": list(requirements.platforms),
    }


def _model_row(model: Model, accepted: dict[str, bool]) -> dict[str, Any]:
    return {
        "id": model.id,
        "spdx": model.spdx,
        "commercial_use": model.commercial_use,
        "requires_acceptance": model.requires_acceptance,
        "accepted": accepted.get(model.id, False),
    }


def _download_bytes(package: Package, home: Home, installed: bool) -> int:
    """Bytes of models still missing; 0 when the package is installed."""
    if installed:
        return 0
    return sum(model.bytes for model in package.models if not env.model_present(model, home))


# --- the minimal valid piece `describe` shows.


def _piece_example(capability: Capability) -> str:
    """A minimal valid piece in TOML, from required fields and defaults.

    Every field that is required or carries a default gets a value the
    capability's schema accepts: the first enum value, the numeric minimum,
    and so on.  Arrays of objects become one ``[[inputs.name]]`` block, kept
    after the scalar keys so the TOML table stays unambiguous.
    """
    lines = ["[inputs]"]
    arrays: list[Field] = []
    for field in capability.inputs:
        if not (field.required or field.has_default):
            continue
        if field.type == "array" and field.items:
            arrays.append(field)
            continue
        lines.append(f"{field.name} = {_toml_value(_example_value(field))}")
    for field in arrays:
        lines.extend(_array_example_lines(field))
    return "\n".join(lines) + "\n"


def _array_example_lines(field: Field) -> list[str]:
    """One ``[[inputs.<field>]]`` block with the item fields filled in."""
    lines = [f"[[inputs.{field.name}]]"]
    for item in field.items:
        if item.required or item.has_default:
            lines.append(f"{item.name} = {_toml_value(_example_value(item))}")
    return lines


def _example_value(field: Field) -> Any:
    """One schema-valid value for ``field``: enum first, minimum, false."""
    if field.type == "enum" and field.values:
        return field.values[0]
    if field.type == "integer":
        return math.ceil(field.min) if field.min is not None else 0
    if field.type == "number":
        return field.min if field.min is not None else 0
    if field.type == "boolean":
        return False
    if field.type == "array":
        return []
    return "text"


def _toml_value(value: Any) -> str:
    """One TOML scalar rendering: strings escaped, booleans lowercase."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return str(value)
