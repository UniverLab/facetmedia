"""The asset record: one ``<asset>.facet.json`` beside every asset.

Every asset facet makes gets a record next to it: a content-hash id, what the
asset is, where it came from (capability, package lock, tool, models,
parameters, seed, input assets), what its check measured, and under which
licences it was made. verveSpace and the canopy pipeline cite this record
instead of recomputing anything, and a non-commercial restriction travels
with the asset.

The record is facet's. The leaf JSON and the space's ``manifest.json`` belong
to verveSpace and are never mixed into it.

Version 1 keys are never renamed once released. Paths in a record are relative
to the record's directory: no absolute path appears anywhere in one.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from facetmedia import registry
from facetmedia.contract import FacetError
from facetmedia.home import atomic_write_json

__all__ = [
    "CHUNK_BYTES",
    "RECORD_SUFFIX",
    "RECORD_VERSION",
    "STATUSES",
    "RecordSpec",
    "asset_id",
    "build_record",
    "params_digest",
    "read_record",
    "record_path",
    "registry_provenance",
    "verify_integrity",
    "write_record",
]

RECORD_VERSION: Final[int] = 1
RECORD_SUFFIX: Final[str] = ".facet.json"
CHUNK_BYTES: Final[int] = 1024 * 1024
STATUSES: Final[frozenset[str]] = frozenset({"passed", "failed", "skipped"})
#: The fields a licence acceptance keeps; nothing identifying beyond the login.
ACCEPTED_KEYS: Final[tuple[str, ...]] = ("model", "spdx", "by", "at", "method")
CREATED_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class RecordSpec:
    """What the caller knows about an asset; the bytes come from the file.

    ``facet`` is explicit rather than read from ``facetmedia.__version__`` so
    a stored golden record stays stable across releases. ``registry`` is
    ``None`` for the registry in use, or an explicit ``{"commit", "path"}``.
    """

    name: str
    kind: str
    mime: str
    facet: str
    capability: str
    package: Mapping[str, str]
    tool: Mapping[str, str]
    models: tuple[Mapping[str, object], ...] = ()
    params: Mapping[str, object] = field(default_factory=dict)
    properties: Mapping[str, object] = field(default_factory=dict)
    seed: int | None = None
    inputs: tuple[Path | str, ...] = ()
    registry: Mapping[str, str | None] | None = None
    verification: Mapping[str, object] | None = None
    license: Mapping[str, object] = field(default_factory=dict)


def asset_id(path: Path | str) -> str:
    """``sha256:<hex>`` of the asset's bytes, streamed in one pass."""
    return f"sha256:{_require_facts(Path(path))[0]}"


def record_path(path: Path | str) -> Path:
    """The record beside ``path``: ``intro.wav`` -> ``intro.wav.facet.json``."""
    target = Path(path)
    if target.name.endswith(RECORD_SUFFIX):
        return target
    return Path(str(target) + RECORD_SUFFIX)


def params_digest(params: Mapping[str, object]) -> str:
    """sha256 of the canonical JSON of ``params``, after defaults and paths."""
    canonical = json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def registry_provenance() -> dict[str, str | None]:
    """The registry a run read: a pinned commit, or the ``FACET_REGISTRY`` dir.

    A development run names the directory so it is never mistaken for a
    pinned one. The override is read through :func:`registry.override_dir`,
    the one place that validates it.
    """
    override = registry.override_dir()
    if override is not None:
        return {"commit": None, "path": str(override)}
    return {"commit": registry.snapshot_commit(), "path": None}


def build_record(
    asset_path: Path | str,
    spec: RecordSpec,
    created: str | None = None,
) -> dict[str, object]:
    """The version-1 record for the asset at ``asset_path``."""
    asset = Path(asset_path)
    base = record_path(asset).parent
    digest, size = _require_facts(asset)
    params = _relative_params(spec.params, base)
    return {
        "record": RECORD_VERSION,
        "id": f"sha256:{digest}",
        "name": spec.name,
        "path": asset.name,
        "kind": spec.kind,
        "mime": spec.mime,
        "bytes": size,
        "created": created if created is not None else _now_z(),
        "properties": dict(spec.properties),
        "provenance": _provenance(spec, params, base),
        "verification": _verification(spec.verification),
        "license": _license_section(spec),
    }


def write_record(asset_path: Path | str, record: Mapping[str, object]) -> Path:
    """Write ``record`` atomically beside the asset; never touches the asset."""
    target = record_path(asset_path)
    atomic_write_json(target, record, target.parent)
    return target


def read_record(path: Path | str) -> dict[str, object]:
    """Read the record for ``path``, given either the asset or its record."""
    target = record_path(path)
    record = _load_record(target)
    if record is None:
        raise FacetError(
            "invalid_input",
            f"{target}: no facet record",
            details={"path": str(target)},
        )
    return record


def verify_integrity(path: Path | str) -> bool:
    """Whether the record's ``id`` still matches the asset beside it.

    A missing record, a corrupt one or a missing asset answers ``False``: the
    question is whether the record still describes this file, not why it does
    not.
    """
    given = Path(path)
    try:
        record = _load_record(record_path(given))
    except FacetError:
        return False
    if record is None:
        return False
    facts = _facts(_asset_for(given))
    if facts is None:
        return False
    return record.get("id") == f"sha256:{facts[0]}"


def _load_record(target: Path) -> dict[str, object] | None:
    """The record JSON at ``target``, or ``None`` when there is no file.

    A record that exists but cannot be read is never treated as absent: it
    raises ``invalid_input``, naming the file, instead of answering ``None``.
    """
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FacetError("invalid_input", f"{target}: cannot read facet record") from exc
    try:
        loaded: object = json.loads(text)
    except ValueError as exc:
        raise FacetError("invalid_input", f"{target}: corrupt facet record") from exc
    if not isinstance(loaded, dict):
        raise FacetError("invalid_input", f"{target}: not a facet record")
    return dict(loaded)


def _facts(path: Path) -> tuple[str, int] | None:
    """The file's (sha256, byte count) in one pass, or ``None`` if unreadable."""
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(CHUNK_BYTES):
                digest.update(chunk)
                size += len(chunk)
    except OSError:
        return None
    return digest.hexdigest(), size


def _require_facts(asset: Path) -> tuple[str, int]:
    facts = _facts(asset)
    if facts is None:
        raise FacetError("invalid_input", f"{asset}: cannot read asset")
    return facts


def _asset_for(given: Path) -> Path:
    """The asset a record path names; a plain asset path is already one."""
    if not given.name.endswith(RECORD_SUFFIX):
        return given
    return Path(str(given)[: -len(RECORD_SUFFIX)])


def _now_z() -> str:
    return datetime.now(UTC).strftime(CREATED_FORMAT)


def _relative(value: str, base: Path) -> str:
    """``value`` as a posix path relative to ``base``, never absolute.

    Both sides are resolved the way every path is (a relative one from the
    working directory), so the answer is correct whatever the working
    directory happens to be.
    """
    absolute = Path(os.path.abspath(value))
    return Path(os.path.relpath(absolute, base)).as_posix()


def _relative_params(params: Mapping[str, object], base: Path) -> dict[str, object]:
    return {str(key): _relative_value(value, base) for key, value in params.items()}


def _relative_value(value: object, base: Path) -> object:
    """Relativise the paths inside a parameter, leaving text such as "/imagine".

    A string is a path only when it is absolute and exists; every list and
    mapping is walked.
    """
    if isinstance(value, Mapping):
        return {str(key): _relative_value(item, base) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_relative_value(item, base) for item in value]
    if isinstance(value, str) and Path(value).is_absolute() and Path(value).exists():
        return _relative(value, base)
    return value


def _provenance(spec: RecordSpec, params: Mapping[str, object], base: Path) -> dict[str, object]:
    return {
        "facet": spec.facet,
        "registry": _registry_section(spec.registry, base),
        "capability": spec.capability,
        "package": dict(spec.package),
        "tool": dict(spec.tool),
        "models": [dict(model) for model in spec.models],
        "params": dict(params),
        "params_sha256": params_digest(params),
        "seed": spec.seed,
        "inputs": [_input_entry(item, base) for item in spec.inputs],
    }


def _input_entry(item: Path | str, base: Path) -> dict[str, str]:
    """One ``provenance.inputs`` entry: id and path name the same file.

    The input is resolved the way every path is (a relative one from the
    working directory) and then stored relative to the record's directory, so
    the id and the path can never describe different bytes.
    """
    resolved = Path(os.path.abspath(item))
    return {"id": asset_id(resolved), "path": _relative(str(resolved), base)}


def _registry_section(
    registry_in_use: Mapping[str, str | None] | None, base: Path
) -> dict[str, str | None]:
    section = dict(registry_in_use) if registry_in_use is not None else registry_provenance()
    path = section.get("path")
    if path:
        section["path"] = _relative(path, base)
    return section


def _verification(verification: Mapping[str, object] | None) -> dict[str, object]:
    given: Mapping[str, object] = verification if verification is not None else {}
    status = given.get("status", "skipped")
    if not isinstance(status, str) or status not in STATUSES:
        raise FacetError(
            "invalid_input",
            f"verification status must be one of {', '.join(sorted(STATUSES))}",
            details={"status": status},
        )
    return {
        "status": status,
        "metrics": _mapping(given.get("metrics")),
        "thresholds": _mapping(given.get("thresholds")),
        "verifier": given.get("verifier"),
    }


def _license_section(spec: RecordSpec) -> dict[str, object]:
    declared = spec.license
    tools = _mappings(declared.get("tools"))
    models = _mappings(declared.get("models"))
    accepted = [
        {key: entry[key] for key in ACCEPTED_KEYS if key in entry}
        for entry in _mappings(declared.get("accepted"))
    ]
    commercial = all(entry.get("commercial_use") is True for entry in models)
    if commercial:
        commercial = not any(_input_is_nc(item) for item in spec.inputs)
    return {
        "tools": tools,
        "models": models,
        "commercial_use": commercial,
        "accepted": accepted,
    }


def _input_is_nc(path: Path | str) -> bool:
    """Whether the input asset's own record forbids commercial use.

    A missing record, or one without a licence section, is ignored: a
    restriction is inherited only from a record that states one. A corrupt
    record is refused rather than read as permissive.
    """
    record = _load_record(record_path(path))
    if record is None:
        return False
    license = record.get("license")
    if not isinstance(license, dict):
        return False
    return license.get("commercial_use") is False


def _mapping(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _mappings(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list | tuple):
        return []
    return [_mapping(item) for item in value if isinstance(item, Mapping)]
