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
import re
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from facetmedia import check, doctor, env, example, installed, licenses, schema, skill, update
from facetmedia.contract import CONTRACT_VERSION, FacetError
from facetmedia.home import Home
from facetmedia.registry import load_active_registry, snapshot_identity
from facetmedia.registry_types import (
    FACETS,
    Capability,
    FacetLabel,
    Field,
    Model,
    Package,
    Registry,
    capability_models,
    is_multiple,
    weight,
)

__all__ = ["announce_list", "describe_data", "list_data", "skill_data"]


def list_data(available: bool, facet: str | None = None) -> dict[str, Any]:
    """The ``facet list`` payload: the snapshot in use and its packages.

    Only installed packages are listed unless ``available`` asks for every
    package in the registry snapshot, compatible or not.  ``facet`` keeps
    one facet's packages.
    """
    if facet is not None and facet not in FACETS:
        raise FacetError(
            "invalid_input",
            f"unknown facet: {facet}",
            details={"known": list(FACETS)},
        )
    active = load_active_registry()
    home = Home.peek_env()
    recorded = installed.entries(home)
    rows = [
        _package_row(package, home, recorded)
        for package in sorted(active.packages.values(), key=lambda item: item.name)
    ]
    if not available:
        rows = [row for row in rows if row["installed"] is True]
    if facet is not None:
        rows = [row for row in rows if row["facet"] == facet]
    data: dict[str, Any] = {"registry": snapshot_identity(), "packages": rows}
    if available and active.facets:
        data["facets"] = {name: _facet_row(active.facets[name]) for name in sorted(active.facets)}
    if not available:
        data["available"] = _available_summary(active)
    return data


def _facet_row(facet: FacetLabel) -> dict[str, Any]:
    """One facet's declared label and its localized labels."""
    return {
        "label": facet.label,
        "i18n": {lang: {"label": text} for lang, text in sorted(facet.i18n.items())},
    }


def announce_list(data: dict[str, Any], available: bool, facet: str | None) -> None:
    """Print ``facet list``'s text output, grouped by facet."""
    printed = False
    for group in FACETS:
        rows = [package for package in _list_rows(data) if package.get("facet") == group]
        if not rows:
            continue
        printed = True
        print(f"{group}:")
        for package in sorted(rows, key=lambda row: str(row.get("name", ""))):
            print(_list_line(package))
            capabilities = package.get("capabilities")
            for capability in capabilities if isinstance(capabilities, list) else []:
                if not isinstance(capability, dict):
                    continue
                print(
                    f"    {capability.get('id')}  {capability.get('kind')}  "
                    f"{capability.get('summary')}"
                )
    if not printed and not available:
        print(_no_packages_line(facet))


def _list_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    packages = data.get("packages")
    if not isinstance(packages, list):
        return []
    return [package for package in packages if isinstance(package, dict)]


def _list_line(package: dict[str, Any]) -> str:
    """One package's line: name, weight, snapshot version, install and update."""
    line = f"  {package.get('name')}  {package.get('weight')}  {package.get('version')}"
    installed_version = package.get("installed_version")
    if isinstance(installed_version, str):
        line += f"  installed {installed_version}"
    if package.get("update_available") is True:
        line += f"  update: {installed_version} → {package.get('version')}"
    return line


def _no_packages_line(facet: str | None) -> str:
    if facet is None:
        return "no packages installed; `facet list --available` shows the registry's"
    return (
        f"no {facet} packages installed; "
        f"`facet list --available --facet {facet}` shows the registry's"
    )


def _available_summary(active: Registry) -> dict[str, int]:
    """Whole-snapshot counts for the installed-set ``data.available``."""
    return {
        "packages": len(active.packages),
        "capabilities": sum(len(package.capabilities) for package in active.packages.values()),
        "facets": len({package.facet for package in active.packages.values()}),
    }


def _package_row(
    package: Package, home: Home, recorded: dict[str, dict[str, object]]
) -> dict[str, Any]:
    is_installed = env.is_installed(package, home)
    env_dir = home.env_dir(package.name, package.version)
    installed_version = _installed_version(package, is_installed, recorded)
    return {
        "name": package.name,
        "version": package.version,
        "tool_name": package.tool_name,
        "provider": package.provider,
        "facet": package.facet,
        "weight": weight(package),
        "vram_gb": package.requirements.vram_gb,
        "compatible": package.compatible,
        "installed": is_installed,
        "installed_version": installed_version,
        "update_available": _update_available(installed_version, package.version),
        "env_bytes": doctor.tree_bytes(env_dir) if is_installed else 0,
        "models_bytes": sum(
            model.bytes for model in package.models if env.model_present(model, home)
        ),
        "capabilities": [
            {"id": cap.id, "summary": cap.summary, "kind": cap.kind}
            for cap in sorted(package.capabilities, key=lambda item: item.id)
        ],
    }


def _installed_version(
    package: Package, is_installed: bool, recorded: dict[str, dict[str, object]]
) -> str | None:
    """The recorded version, else the snapshot's when installed, else none."""
    version = recorded.get(package.name, {}).get("version")
    if isinstance(version, str) and version:
        return version
    return package.version if is_installed else None


def _update_available(installed_version: str | None, snapshot_version: str) -> bool:
    """Whether the snapshot's version is newer than the installed one."""
    if installed_version is None:
        return False
    try:
        return Version(installed_version) < Version(snapshot_version)
    except InvalidVersion:
        return False


def describe_data(capability_id: str) -> dict[str, Any]:
    """The ``facet describe`` payload for one capability id.

    The contract version appears here as well as in the envelope because
    clients cache ``describe`` output on its own.
    """
    capability, package = check.resolve(capability_id)
    home = Home.peek_env()
    installed = env.is_installed(package, home)
    accepted = licenses.accepted_models(package.models)
    piece, example_path = _describe_piece(capability, package)
    return {
        "contract": CONTRACT_VERSION,
        "capability": capability.id,
        "summary": capability.summary,
        "i18n": {lang: {"summary": text} for lang, text in sorted(capability.i18n.items())},
        "kind": capability.kind,
        "facet": package.facet,
        "weight": weight(package),
        "skill": f"facet skill {package.name}",
        "package": {
            "name": package.name,
            "version": package.version,
            "tool_name": package.tool_name,
            "provider": package.provider,
        },
        "inputs": schema.to_json_schema(capability),
        "piece_example": piece,
        "example_path": example_path,
        "verify": {"thresholds": dict(sorted(capability.thresholds.items()))},
        "verify_uses_server": capability.verify_uses_server,
        "preview": {"viewer": capability.viewer},
        "requirements": _requirements(package),
        "licenses": {
            "tools": [{"subject": lic.subject, "spdx": lic.spdx} for lic in package.licenses],
            "models": [
                _model_row(model, accepted) for model in capability_models(package, capability)
            ],
        },
        "download_bytes": _download_bytes(package, home, installed),
        "installed": installed,
    }


def _requirements(package: Package) -> dict[str, Any]:
    requirements = package.requirements
    return {
        "gpu": requirements.gpu,
        "ram_gb": requirements.ram_gb,
        "vram_gb": requirements.vram_gb,
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


# --- the piece `describe` shows: the bundled example, else a synthetic one.


def _describe_piece(capability: Capability, package: Package) -> tuple[str, str | None]:
    """The capability's first bundled example, or a synthetic valid piece.

    ``facet example`` copies the first example in path order, so ``describe``
    shows that same text with its top-level ``capability`` key removed.  The
    second element is the example's path relative to the package directory,
    ``None`` when the capability ships none.
    """
    found = [item for item in example.find_examples(package) if item.capability == capability.id]
    if not found:
        return _piece_example(capability), None
    first = found[0]
    return _piece_file_text(first.path), first.path.relative_to(package.directory).as_posix()


_CAPABILITY_KEY = re.compile(r"[\"']?capability[\"']?\s*=")


def _piece_file_text(path: Path) -> str:
    """An example's own TOML, without the top-level ``capability`` key."""
    kept: list[str] = []
    in_table = False
    for line in path.read_text(encoding="utf-8-sig").splitlines(keepends=True):
        stripped = line.lstrip()
        in_table = in_table or stripped.startswith("[")
        if in_table or _CAPABILITY_KEY.match(stripped) is None:
            kept.append(line)
    return "".join(kept)


def _piece_example(capability: Capability) -> str:
    """A minimal valid piece in TOML, from required fields and defaults.

    Every field that is required or carries a default gets a value the
    capability's schema accepts: an enum's default or first value, the
    numeric minimum adjusted to ``multiple_of``, a string matching its
    ``pattern``.  Arrays of objects become one ``[[inputs.name]]`` block per
    ``min_items`` (at least one), kept after the scalar keys so the TOML
    table stays unambiguous.
    """
    lines = ["[inputs]"]
    arrays: list[Field] = []
    for field in capability.inputs:
        if not (field.required or field.has_default):
            continue
        if field.type == "array" and field.items:
            arrays.append(field)
            continue
        lines.append(_example_line(field, _example_value(field)))
    for field in arrays:
        lines.extend(_array_example_lines(field))
    return "\n".join(lines) + "\n"


def _array_example_lines(field: Field) -> list[str]:
    """One ``[[inputs.<field>]]`` block per item ``min_items`` requires."""
    lines: list[str] = []
    for _ in range(max(field.min_items or 0, 1)):
        lines.append(f"[[inputs.{field.name}]]")
        for item in field.items:
            if item.required or item.has_default:
                lines.append(_example_line(item, _example_value(item)))
    return lines


#: No value matches the field's constraints: the line becomes a comment.
_SET_THIS: Any = object()


def _example_line(field: Field, value: Any) -> str:
    """One TOML key line, or a ``# set this`` comment when nothing fits."""
    if value is _SET_THIS:
        return f"# set this: {field.name}"
    return f"{field.name} = {_toml_value(value)}"


def _example_value(field: Field) -> Any:
    """One schema-valid value for ``field``, or ``_SET_THIS`` when none fits."""
    if field.type in {"string", "enum"}:
        return _example_string(field)
    if field.type in {"integer", "number"}:
        return _example_number(field)
    if field.type == "boolean":
        return False
    if field.type == "array":
        return []
    if field.type == "path":
        # The piece's own directory: the one path that always exists.
        return "."
    return _SET_THIS


def _example_string(field: Field) -> Any:
    """A string the field's ``pattern`` accepts, else ``_SET_THIS``."""
    candidates: list[str] = []
    if field.has_default and isinstance(field.default, str):
        candidates.append(field.default)
    if field.type == "enum":
        candidates.extend(field.values)
    else:
        candidates.extend(["text", "example", field.name, "a", ""])
    for candidate in candidates:
        if _matches(field.pattern, candidate):
            return candidate
    return _SET_THIS


def _matches(pattern: str | None, value: str) -> bool:
    """True when ``value`` satisfies ``pattern``, or when there is none."""
    if pattern is None:
        return True
    return re.fullmatch(pattern, value) is not None


def _example_number(field: Field) -> float | int:
    """A number the field accepts: its default, or a boundary it allows.

    ``multiple_of`` wins over the boundary: the value rounds up to the next
    multiple, or down to the largest one at or below ``max``.
    """
    base = _number_base(field)
    multiple = field.multiple_of
    if multiple is None or is_multiple(base, multiple):
        return _cast_number(field, base)
    return _cast_number(field, _next_multiple(base, multiple, field.max))


def _number_base(field: Field) -> float:
    """The default, else the minimum, else the maximum below zero, else 0."""
    default = field.default
    if field.has_default and isinstance(default, int | float) and not isinstance(default, bool):
        return default
    if field.min is not None:
        return math.ceil(field.min) if field.type == "integer" else field.min
    if field.max is not None and field.max < 0:
        return math.floor(field.max) if field.type == "integer" else field.max
    return 0


def _next_multiple(base: float, multiple: float, maximum: float | None) -> float:
    """``base`` rounded up to a multiple, or down when that passes ``maximum``."""
    step = Decimal(str(multiple))
    rounded = (Decimal(str(base)) / step).to_integral_value(rounding=ROUND_CEILING) * step
    if maximum is not None and rounded > Decimal(str(maximum)):
        rounded = (Decimal(str(maximum)) / step).to_integral_value(rounding=ROUND_FLOOR) * step
    return float(rounded)


def _cast_number(field: Field, value: float) -> float | int:
    """An integer field gets ``int`` so the TOML stays an integer literal."""
    return int(value) if field.type == "integer" else value


def _toml_value(value: Any) -> str:
    """One TOML scalar rendering: strings escaped, booleans lowercase."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def skill_data(
    target: str | None, reference: str | None, *, with_doctor: bool, run_id: str
) -> dict[str, Any]:
    """The ``facet skill`` payload: the skill text plus the machine section.

    A light verb: no lock.  ``target`` names a package or a capability
    id; ``None`` is the core skill, which prints without a registry.
    """
    if target is None:
        return _core_skill_data(reference)
    package, capability_id = _resolve_skill_target(target)
    return _package_skill_data(
        package, capability_id, reference, with_doctor=with_doctor, run_id=run_id
    )


def _resolve_skill_target(target: str) -> tuple[Package, str | None]:
    """The package and optional capability id for a skill target."""
    active = load_active_registry()
    package = active.package(target)
    if package is not None:
        return package, None
    try:
        capability, owner = check.resolve(target)
    except FacetError:
        known = sorted([*active.packages, *(cap.id for cap in active.capabilities())])
        raise FacetError(
            "invalid_input",
            f"unknown package or capability: {target}",
            details={"known": known},
        ) from None
    return owner, capability.id


def _core_skill_data(reference: str | None) -> dict[str, Any]:
    """The core skill plus ``## Packages in this registry``."""
    if reference is not None:
        raise FacetError(
            "invalid_input",
            f"unknown reference: {reference}",
            details={"known": []},
        )
    text = skill.core_skill_text()
    try:
        active = load_active_registry()
        identity = snapshot_identity()
    except FacetError as exc:
        return _core_skill_no_registry(text, exc)
    if identity.get("commit"):
        registry_line = f"Registry commit {identity['commit']} (ref {identity.get('ref')})."
    else:
        registry_line = f"Registry {identity.get('path', '')}."
    parts = [text.rstrip("\n"), "", "## Packages in this registry", "", registry_line, ""]
    facets: dict[str, list[dict[str, Any]]] = {}
    for facet in FACETS:
        rows = sorted(
            (item for item in active.packages.values() if item.facet == facet),
            key=lambda item: item.name,
        )
        if not rows:
            continue
        parts.append(f"### {facet}")
        facets[facet] = []
        for package in rows:
            description = _skill_description_of(package)
            capabilities = sorted(cap.id for cap in package.capabilities)
            line = f"- {package.name} ({weight(package)}): {', '.join(capabilities)}"
            if description:
                line += f" — {description}"
            parts.append(line)
            facets[facet].append(
                {
                    "name": package.name,
                    "weight": weight(package),
                    "capabilities": capabilities,
                    "description": description,
                }
            )
        parts.append("")
    return {
        "scope": "core",
        "name": "facet",
        "facet": None,
        "weight": None,
        "description": skill.skill_description(text),
        "body": "\n".join(parts).rstrip("\n") + "\n",
        "references": [],
        "state": {"registry": identity, "facets": facets, "registry_error": None},
    }


def _registry_advice(error: FacetError) -> str:
    """One line of advice for a registry that could not be loaded.

    A snapshot that fails validation can never load by re-running update with
    the same ref: the ref resolves the same commit and install leaves nothing
    behind.  Any other load error is not deterministic, so retrying can work.
    """
    if error.code == "download_failed" and "does not validate" in error.message:
        commit = error.details.get("commit", "unknown")
        problem = error.message.partition("does not validate")[2].lstrip(": ").strip()
        return (
            f"Registry snapshot {commit} does not validate: {problem or error.message}. "
            "Re-running update with the same ref fails identically (same ref resolves "
            "the same commit, and install_snapshot leaves nothing behind). "
            "Try a different --ref or $FACET_REGISTRY_REF."
        )
    ref = update.default_ref()
    return (
        f"Registry not available: {error.message}. "
        f"Run `facet update` (will try ref '{ref}'), then `facet skill` again."
    )


def _core_skill_no_registry(text: str, error: FacetError) -> dict[str, Any]:
    """The core skill plus one line of registry advice, no registry."""
    parts = [
        text.rstrip("\n"),
        "",
        "## Packages in this registry",
        "",
        _registry_advice(error),
    ]
    return {
        "scope": "core",
        "name": "facet",
        "facet": None,
        "weight": None,
        "description": skill.skill_description(text),
        "body": "\n".join(parts).rstrip("\n") + "\n",
        "references": [],
        "state": {
            "registry": None,
            "facets": {},
            "registry_error": {"code": error.code, "message": error.message},
        },
    }


def _skill_description_of(package: Package) -> str:
    """The skill's one-line description, or ``""`` when it has no skill."""
    text = skill.skill_text(package.directory)
    if text is None:
        return ""
    return skill.skill_description(text)


def _package_skill_data(
    package: Package,
    capability_id: str | None,
    reference: str | None,
    *,
    with_doctor: bool,
    run_id: str,
) -> dict[str, Any]:
    """One package's skill plus ``## On this machine``."""
    text = skill.skill_text(package.directory)
    if text is None:
        raise FacetError("internal", f"packages/{package.name}/skill/SKILL.md: missing skill")
    references = skill.skill_references(package.directory)
    if reference is not None and reference not in references:
        raise FacetError(
            "invalid_input",
            f"unknown reference: {reference}",
            details={"known": references},
        )
    if reference is not None:
        body = _reference_text(package, reference)
        _, state = _machine_section(package, capability_id, False, run_id)
    else:
        section, state = _machine_section(package, capability_id, with_doctor, run_id)
        body = text.rstrip("\n") + "\n\n" + "\n".join(section) + "\n"
    return {
        "scope": "package",
        "name": package.name,
        "facet": package.facet,
        "weight": weight(package),
        "description": skill.skill_description(text),
        "body": body,
        "references": references,
        "state": state,
    }


def _reference_text(package: Package, reference: str) -> str:
    """One reference file's content, exactly as written."""
    content = (package.directory / "skill" / "references" / f"{reference}.md").read_text(
        encoding="utf-8"
    )
    return content if content.endswith("\n") else content + "\n"


def _machine_section(
    package: Package, capability_id: str | None, with_doctor: bool, run_id: str
) -> tuple[list[str], dict[str, Any]]:
    """``## On this machine`` as lines and as data, reading nothing but files."""
    home = Home.peek_env()
    installed = env.is_installed(package, home)
    accepted = licenses.accepted_models(package.models)
    missing = _download_bytes(package, home, installed)
    named = next((cap for cap in package.capabilities if cap.id == capability_id), None)
    shown = package.models if named is None else capability_models(package, named)
    models = [_model_row(model, accepted) for model in shown]
    lines = [
        f"## On this machine ({capability_id})" if capability_id else "## On this machine",
        "",
    ]
    if installed:
        lines.append("- installed: yes.")
    else:
        lines.append(_not_installed_line(package, missing))
    for row in models:
        lines.append(_model_line(row))
    doctor_checks: list[dict[str, Any]] | None = None
    if with_doctor and installed:
        capability = capability_id or next((cap.id for cap in package.capabilities), "")
        doctor_checks = _quick_checks(package, home, run_id, capability)
        lines.extend(["", "Quick doctor:"])
        for entry in doctor_checks:
            lines.append(_check_line(entry))
    state: dict[str, Any] = {
        "installed": installed,
        "download_bytes": missing,
        "models": models,
        "doctor": doctor_checks,
    }
    return lines, state


def _not_installed_line(package: Package, missing: int) -> str:
    pull = f"`facet pull {package.name}`"
    if not package.models:
        return f"- installed: no — run {pull} (no model downloads)."
    return f"- installed: no — run {pull} ({missing} bytes)."


def _model_line(row: dict[str, Any]) -> str:
    """One model's licence state, with the fix when acceptance is missing."""
    model_id = str(row["id"])
    if not row["requires_acceptance"]:
        return f"- {model_id}: no acceptance needed."
    if row["accepted"]:
        return f"- {model_id}: accepted."
    return f"- {model_id}: needs `facet license accept {model_id}`."


def _quick_checks(
    package: Package, home: Home, run_id: str, capability: str
) -> list[dict[str, Any]]:
    """Readiness plus the quick package doctor, each ``{name, ok, detail, fix}``."""
    readiness, _ = doctor.machine_checks(package, home)
    ordered = [
        {
            "name": name,
            "ok": readiness[name]["ok"],
            "detail": readiness[name]["detail"],
            "fix": readiness[name]["fix"],
        }
        for name in doctor.CHECK_ORDER
    ]
    doctor_checks, _ = doctor.package_doctor(package, home, run_id, "quick", capability)
    return [*ordered, *doctor_checks]


def _check_line(entry: dict[str, Any]) -> str:
    """One check line, with its fix when it fails."""
    mark = "✓" if entry["ok"] is True else "✗"
    line = f"- {mark} {entry['name']}: {entry['detail']}"
    fix = entry.get("fix")
    if mark == "✗" and isinstance(fix, str) and fix:
        line += f" → fix: {fix}"
    return line
