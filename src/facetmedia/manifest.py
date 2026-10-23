"""Read and validate a registry's index and package manifests.

``RegistryLoader`` collects every problem instead of stopping at the first one,
so a registry author sees all the broken lines at once.  ``facetmedia.registry``
drives it.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from facetmedia.registry_types import (
    CAPABILITY_KINDS,
    DEFAULT_INVOCATION_TEMPLATE,
    FIELD_TYPES,
    MISSING,
    Capability,
    Field,
    License,
    Model,
    Package,
    Problem,
    Registry,
    Requirements,
)

_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9-]*\Z")
_MODEL_ID_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}\Z")
_REVISION_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{40}\Z")
_PYTHON_RE: Final[re.Pattern[str]] = re.compile(r"\d+\.\d+(\.\d+)?\Z")


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


def _is_relative_path(value: str) -> bool:
    """True for a non-empty relative path that stays inside its directory."""

    if not value:
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts


_PACKAGE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "name",
        "version",
        "requires_facet",
        "protocol",
        "provider",
        "tool_name",
        "description",
        "engine",
        "transport",
        "requirements",
        "licenses",
        "models",
        "capabilities",
    }
)
_ENGINE_KEYS: Final[frozenset[str]] = frozenset({"type", "python"})
_TRANSPORT_KEYS: Final[frozenset[str]] = frozenset({"type", "entry", "invocation_template"})
_REQUIREMENT_KEYS: Final[frozenset[str]] = frozenset(
    {"gpu", "ram_gb", "disk_gb", "exclusive", "platforms", "host_libs", "host_hint"}
)
_LICENSE_KEYS: Final[frozenset[str]] = frozenset({"subject", "spdx"})
_MODEL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "spdx",
        "commercial_use",
        "requires_acceptance",
        "terms_url",
        "url",
        "sha256",
        "bytes",
        "path",
        "hf_repo",
        "revision",
    }
)
_CAPABILITY_KEYS: Final[frozenset[str]] = frozenset(
    {"summary", "kind", "inputs", "verify", "preview"}
)
_VERIFY_KEYS: Final[frozenset[str]] = frozenset({"thresholds"})
_PREVIEW_KEYS: Final[frozenset[str]] = frozenset({"viewer"})
_FIELD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "type",
        "required",
        "default",
        "description",
        "min",
        "max",
        "pattern",
        "values",
        "min_items",
        "items",
    }
)
_FIELD_OPTION_TYPES: Final[Mapping[str, frozenset[str]]] = {
    "values": frozenset({"enum"}),
    "min": frozenset({"integer", "number"}),
    "max": frozenset({"integer", "number"}),
    "pattern": frozenset({"string", "enum"}),
    "min_items": frozenset({"array"}),
}


class RegistryLoader:
    """Collects every problem instead of stopping at the first one."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.problems: list[Problem] = []
        self.model_ids: dict[str, str] = {}

    def add(self, file: str, key: str, problem: str) -> None:
        found = Problem(file, key, problem)
        if found not in self.problems:
            self.problems.append(found)

    def table(
        self, parent: Mapping[str, Any], key: str, file: str, prefix: str, *, required: bool
    ) -> dict[str, Any] | None:
        value = parent.get(key)
        if value is None:
            if required:
                self.add(file, f"{prefix}{key}", "required")
            return None
        if not isinstance(value, dict):
            self.add(file, f"{prefix}{key}", "type")
            return None
        return value

    def string(
        self, parent: Mapping[str, Any], key: str, file: str, prefix: str, *, required: bool
    ) -> str | None:
        value = parent.get(key)
        if value is None:
            if required:
                self.add(file, f"{prefix}{key}", "required")
            return None if required else ""
        if not isinstance(value, str):
            self.add(file, f"{prefix}{key}", "type")
            return None
        if required and not value:
            self.add(file, f"{prefix}{key}", "value")
        return value

    def integer(
        self, parent: Mapping[str, Any], key: str, file: str, prefix: str, *, required: bool
    ) -> int | None:
        value = parent.get(key)
        if value is None:
            if required:
                self.add(file, f"{prefix}{key}", "required")
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            self.add(file, f"{prefix}{key}", "type")
            return None
        return value

    def number(self, parent: Mapping[str, Any], key: str, file: str, prefix: str) -> float | None:
        value = parent.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int | float):
            self.add(file, f"{prefix}{key}", "type")
            return None
        return float(value)

    def boolean(
        self, parent: Mapping[str, Any], key: str, file: str, prefix: str, *, default: bool
    ) -> bool:
        value = parent.get(key)
        if value is None:
            return default
        if not isinstance(value, bool):
            self.add(file, f"{prefix}{key}", "type")
            return default
        return value

    def string_list(
        self, parent: Mapping[str, Any], key: str, file: str, prefix: str
    ) -> tuple[str, ...]:
        value = parent.get(key)
        if value is None:
            return ()
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            self.add(file, f"{prefix}{key}", "type")
            return ()
        return tuple(value)

    def unknown(
        self, table: Mapping[str, Any], allowed: frozenset[str], file: str, prefix: str
    ) -> None:
        for key in sorted(table):
            if key not in allowed:
                self.add(file, f"{prefix}{key}", "unknown")

    def read_toml(self, file: str) -> dict[str, Any] | None:
        path = self.root / file
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except FileNotFoundError:
            self.add(file, "", "missing")
            return None
        except (OSError, tomllib.TOMLDecodeError):
            self.add(file, "", "invalid_toml")
            return None
        if not isinstance(data, dict):
            self.add(file, "", "type")
            return None
        return data

    def load(self) -> Registry | None:
        index = self.read_toml("index.toml")
        if index is None:
            return None
        version = self.index_version(index)
        packages: dict[str, Package] = {}
        for name in self.index_packages(index):
            file = f"packages/{name}/package.toml"
            data = self.read_toml(file)
            if data is None:
                continue
            package = self.package(name, file, data)
            if package is not None:
                packages[name] = package
        return Registry(self.root, version, packages)

    def index_version(self, index: Mapping[str, Any]) -> int:
        self.unknown(index, frozenset({"version", "packages"}), "index.toml", "")
        version = self.integer(index, "version", "index.toml", "", required=True)
        if version is None:
            return 0
        if version != 1:
            self.add("index.toml", "version", "value")
        return version

    def index_packages(self, index: Mapping[str, Any]) -> tuple[str, ...]:
        raw = index.get("packages")
        if raw is None:
            self.add("index.toml", "packages", "required")
            return ()
        if not isinstance(raw, list):
            self.add("index.toml", "packages", "type")
            return ()
        if not raw:
            self.add("index.toml", "packages", "min_items")
            return ()
        names: list[str] = []
        for position, item in enumerate(raw):
            prefix = f"packages[{position}]."
            if not isinstance(item, dict):
                self.add("index.toml", f"{prefix}name", "type")
                continue
            self.unknown(item, frozenset({"name"}), "index.toml", prefix)
            name = self.string(item, "name", "index.toml", prefix, required=True)
            if name is None:
                continue
            if not _NAME_RE.fullmatch(name):
                self.add("index.toml", f"{prefix}name", "value")
                continue
            if name in names:
                self.add("index.toml", f"{prefix}name", "duplicate")
                continue
            names.append(name)
        return tuple(names)

    def package(self, name: str, file: str, data: dict[str, Any]) -> Package | None:
        before = len(self.problems)
        self.unknown(data, _PACKAGE_KEYS, file, "")
        manifest_name = self.string(data, "name", file, "", required=True)
        if manifest_name is not None and manifest_name != name:
            self.add(file, "name", "value")
        version = self.manifest_version(data, file)
        requires_facet = self.requires_facet(data, file)
        protocol = self.integer(data, "protocol", file, "", required=True)
        provider = self.string(data, "provider", file, "", required=True)
        tool_name = self.string(data, "tool_name", file, "", required=True)
        description = self.string(data, "description", file, "", required=True)
        engine_type, python = self.engine(data, file)
        transport_type, entry, template = self.transport(data, file)
        requirements = self.requirements(data, file)
        licenses = self.licenses(data.get("licenses"), file)
        models = self.models(data.get("models"), file)
        capabilities = self.capabilities(data.get("capabilities"), name, file)
        if len(self.problems) != before:
            return None
        if (
            version is None
            or requires_facet is None
            or protocol is None
            or provider is None
            or tool_name is None
            or description is None
        ):
            return None
        return Package(
            name=name,
            version=version,
            requires_facet=requires_facet,
            protocol=protocol,
            provider=provider,
            tool_name=tool_name,
            description=description,
            engine_type=engine_type,
            python=python,
            transport_type=transport_type,
            entry=entry,
            invocation_template=template,
            requirements=requirements,
            directory=self.root / "packages" / name,
            licenses=licenses,
            models=models,
            capabilities=capabilities,
        )

    def manifest_version(self, data: Mapping[str, Any], file: str) -> str | None:
        version = self.string(data, "version", file, "", required=True)
        if version is None:
            return None
        try:
            Version(version)
        except InvalidVersion:
            self.add(file, "version", "value")
            return None
        return version

    def requires_facet(self, data: Mapping[str, Any], file: str) -> str | None:
        specifier = self.string(data, "requires_facet", file, "", required=True)
        if specifier is None:
            return None
        try:
            SpecifierSet(specifier)
        except InvalidSpecifier:
            self.add(file, "requires_facet", "value")
            return None
        return specifier

    def engine(self, data: Mapping[str, Any], file: str) -> tuple[str, str]:
        table = self.table(data, "engine", file, "", required=True)
        if table is None:
            return "", ""
        self.unknown(table, _ENGINE_KEYS, file, "engine.")
        engine_type = self.string(table, "type", file, "engine.", required=True)
        if engine_type is not None and engine_type != "python-env":
            self.add(file, "engine.type", "value")
        python = self.string(table, "python", file, "engine.", required=True)
        if python and not _PYTHON_RE.fullmatch(python):
            self.add(file, "engine.python", "value")
        return engine_type or "", python or ""

    def transport(self, data: Mapping[str, Any], file: str) -> tuple[str, str, str]:
        table = self.table(data, "transport", file, "", required=True)
        if table is None:
            return "", "", DEFAULT_INVOCATION_TEMPLATE
        self.unknown(table, _TRANSPORT_KEYS, file, "transport.")
        transport_type = self.string(table, "type", file, "transport.", required=True)
        if transport_type is not None and transport_type != "cli":
            self.add(file, "transport.type", "value")
        raw_entry = self.string(table, "entry", file, "transport.", required=True)
        entry = raw_entry or ""
        if raw_entry is not None and not _is_relative_path(entry):
            self.add(file, "transport.entry", "value")
        template = self.string(table, "invocation_template", file, "transport.", required=False)
        if isinstance(template, str) and "invocation_template" in table and not template:
            self.add(file, "transport.invocation_template", "value")
        return transport_type or "", entry, template or DEFAULT_INVOCATION_TEMPLATE

    def requirements(self, data: Mapping[str, Any], file: str) -> Requirements:
        table = self.table(data, "requirements", file, "", required=True)
        if table is None:
            return Requirements()
        self.unknown(table, _REQUIREMENT_KEYS, file, "requirements.")
        return Requirements(
            gpu=self.boolean(table, "gpu", file, "requirements.", default=False),
            ram_gb=self.number(table, "ram_gb", file, "requirements.") or 0.0,
            disk_gb=self.number(table, "disk_gb", file, "requirements.") or 0.0,
            exclusive=self.boolean(table, "exclusive", file, "requirements.", default=True),
            platforms=self.string_list(table, "platforms", file, "requirements.") or ("linux",),
            host_libs=self.string_list(table, "host_libs", file, "requirements."),
            host_hint=self.string(table, "host_hint", file, "requirements.", required=False) or "",
        )

    def licenses(self, raw: object, file: str) -> tuple[License, ...]:
        if raw is None:
            return ()
        if not isinstance(raw, list):
            self.add(file, "licenses", "type")
            return ()
        licenses: list[License] = []
        for position, item in enumerate(raw):
            prefix = f"licenses[{position}]."
            if not isinstance(item, dict):
                self.add(file, f"{prefix}subject", "type")
                continue
            self.unknown(item, _LICENSE_KEYS, file, prefix)
            subject = self.string(item, "subject", file, prefix, required=True)
            spdx = self.string(item, "spdx", file, prefix, required=True)
            if subject is not None and spdx is not None:
                licenses.append(License(subject, spdx))
        return tuple(licenses)

    def models(self, raw: object, file: str) -> tuple[Model, ...]:
        if raw is None:
            return ()
        if not isinstance(raw, list):
            self.add(file, "models", "type")
            return ()
        models: list[Model] = []
        for position, item in enumerate(raw):
            prefix = f"models[{position}]."
            if not isinstance(item, dict):
                self.add(file, f"{prefix}id", "type")
                continue
            model = self.model(item, file, prefix)
            if model is not None:
                models.append(model)
        return tuple(models)

    def model(self, item: Mapping[str, Any], file: str, prefix: str) -> Model | None:
        before = len(self.problems)
        self.unknown(item, _MODEL_KEYS, file, prefix)
        model_id = self.string(item, "id", file, prefix, required=True)
        if model_id is not None:
            if not _MODEL_ID_RE.fullmatch(model_id):
                self.add(file, f"{prefix}id", "value")
            elif model_id in self.model_ids:
                self.add(file, f"{prefix}id", "duplicate")
            else:
                self.model_ids[model_id] = file
        spdx = self.string(item, "spdx", file, prefix, required=True)
        commercial = self.boolean(item, "commercial_use", file, prefix, default=False)
        acceptance = self.boolean(item, "requires_acceptance", file, prefix, default=False)
        terms_url = self.string(item, "terms_url", file, prefix, required=False) or ""
        size = self.integer(item, "bytes", file, prefix, required=True)
        if size is not None and size <= 0:
            self.add(file, f"{prefix}bytes", "value")
        direct, hf = self.model_source(item, file, prefix)
        if len(self.problems) != before:
            return None
        if model_id is None or spdx is None or size is None:
            return None
        return Model(
            id=model_id,
            spdx=spdx,
            commercial_use=commercial,
            requires_acceptance=acceptance,
            bytes=size,
            terms_url=terms_url,
            **direct,
            **hf,
        )

    def model_source(
        self, item: Mapping[str, Any], file: str, prefix: str
    ) -> tuple[dict[str, str], dict[str, str]]:
        has_url = item.get("url") is not None
        has_hf = item.get("hf_repo") is not None
        if has_url == has_hf:
            self.add(file, f"{prefix}url", "required" if not has_url else "value")
            return {}, {}
        if has_hf:
            return {}, self.hf_source(item, file, prefix)
        return self.url_source(item, file, prefix), {}

    def url_source(self, item: Mapping[str, Any], file: str, prefix: str) -> dict[str, str]:
        url = self.string(item, "url", file, prefix, required=True) or ""
        if url and not url.startswith(("https://", "file://")):
            self.add(file, f"{prefix}url", "value")
        sha256 = self.string(item, "sha256", file, prefix, required=True) or ""
        if sha256 and not _SHA256_RE.fullmatch(sha256):
            self.add(file, f"{prefix}sha256", "value")
        path = self.string(item, "path", file, prefix, required=True) or ""
        if path and (Path(path).is_absolute() or ".." in Path(path).parts):
            self.add(file, f"{prefix}path", "value")
        return {"url": url, "sha256": sha256, "path": path}

    def hf_source(self, item: Mapping[str, Any], file: str, prefix: str) -> dict[str, str]:
        repo = self.string(item, "hf_repo", file, prefix, required=True) or ""
        revision = self.string(item, "revision", file, prefix, required=True) or ""
        if revision and not _REVISION_RE.fullmatch(revision):
            self.add(file, f"{prefix}revision", "value")
        return {"hf_repo": repo, "revision": revision}

    def capabilities(self, raw: object, package: str, file: str) -> tuple[Capability, ...]:
        if raw is None:
            return ()
        if not isinstance(raw, dict):
            self.add(file, "capabilities", "type")
            return ()
        capabilities: list[Capability] = []
        for name in sorted(raw):
            prefix = f"capabilities.{name}."
            item = raw[name]
            if not _NAME_RE.fullmatch(name):
                self.add(file, prefix.rstrip("."), "value")
                continue
            if not isinstance(item, dict):
                self.add(file, prefix.rstrip("."), "type")
                continue
            capability = self.capability(name, package, file, prefix, item)
            if capability is not None:
                capabilities.append(capability)
        return tuple(capabilities)

    def capability(
        self,
        name: str,
        package: str,
        file: str,
        prefix: str,
        item: Mapping[str, Any],
    ) -> Capability | None:
        before = len(self.problems)
        self.unknown(item, _CAPABILITY_KEYS, file, prefix)
        summary = self.string(item, "summary", file, prefix, required=True)
        kind = self.string(item, "kind", file, prefix, required=True)
        if kind is not None and kind not in CAPABILITY_KINDS:
            self.add(file, f"{prefix}kind", "value")
        inputs = self.fields(item.get("inputs"), file, f"{prefix}inputs.", allow_array=True)
        thresholds = self.verify(item.get("verify"), file, prefix)
        viewer = self.preview(item.get("preview"), file, prefix)
        if len(self.problems) != before or summary is None or kind is None:
            return None
        return Capability(
            id=f"{package}.{name}",
            package=package,
            summary=summary,
            kind=kind,
            inputs=inputs,
            thresholds=thresholds,
            viewer=viewer,
        )

    def verify(self, raw: object, file: str, prefix: str) -> Mapping[str, float]:
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            self.add(file, f"{prefix}verify", "type")
            return {}
        self.unknown(raw, _VERIFY_KEYS, file, f"{prefix}verify.")
        table = self.table(raw, "thresholds", file, f"{prefix}verify.", required=False)
        if table is None:
            return {}
        thresholds: dict[str, float] = {}
        for key in sorted(table):
            value = self.number(table, key, file, f"{prefix}verify.thresholds.")
            if value is not None:
                thresholds[key] = value
        return thresholds

    def preview(self, raw: object, file: str, prefix: str) -> str:
        if raw is None:
            return "generic"
        if not isinstance(raw, dict):
            self.add(file, f"{prefix}preview", "type")
            return "generic"
        self.unknown(raw, _PREVIEW_KEYS, file, f"{prefix}preview.")
        viewer = self.string(raw, "viewer", file, f"{prefix}preview.", required=False)
        if viewer is not None and not _NAME_RE.fullmatch(viewer):
            self.add(file, f"{prefix}preview.viewer", "value")
            return "generic"
        return viewer or "generic"

    def fields(
        self, raw: object, file: str, prefix: str, *, allow_array: bool
    ) -> tuple[Field, ...]:
        if raw is None:
            return ()
        if not isinstance(raw, dict):
            self.add(file, prefix.rstrip("."), "type")
            return ()
        fields: list[Field] = []
        for name in sorted(raw):
            spec = raw[name]
            if not _NAME_RE.fullmatch(name):
                self.add(file, f"{prefix}{name}", "value")
                continue
            if not isinstance(spec, dict):
                self.add(file, f"{prefix}{name}", "type")
                continue
            field = self.field(name, spec, file, prefix, allow_array=allow_array)
            if field is not None:
                fields.append(field)
        return tuple(fields)

    def field(
        self,
        name: str,
        spec: Mapping[str, Any],
        file: str,
        prefix: str,
        *,
        allow_array: bool,
    ) -> Field | None:
        before = len(self.problems)
        key = f"{prefix}{name}"
        self.unknown(spec, _FIELD_KEYS, file, f"{key}.")
        field_type = self.string(spec, "type", file, f"{key}.", required=True)
        if field_type is not None and field_type not in FIELD_TYPES:
            self.add(file, f"{key}.type", "value")
        if field_type == "array" and not allow_array:
            self.add(file, f"{key}.type", "value")
        self.check_field_options(field_type, spec, file, key)
        required = self.boolean(spec, "required", file, f"{key}.", default=False)
        description = self.string(spec, "description", file, f"{key}.", required=False) or ""
        values = self.string_list(spec, "values", file, f"{key}.")
        if field_type == "enum" and not values:
            self.add(file, f"{key}.values", "required")
        default = spec.get("default", MISSING)
        if default is not MISSING:
            self.check_default(field_type or "", default, values, file, f"{key}.default")
        minimum = self.number(spec, "min", file, f"{key}.")
        maximum = self.number(spec, "max", file, f"{key}.")
        pattern = self.string(spec, "pattern", file, f"{key}.", required=False)
        if pattern:
            try:
                re.compile(pattern)
            except re.error:
                self.add(file, f"{key}.pattern", "value")
        min_items = self.integer(spec, "min_items", file, f"{key}.", required=False)
        items = self.fields(spec.get("items"), file, f"{key}.items.", allow_array=False)
        if field_type == "array" and not items:
            self.add(file, f"{key}.items", "required")
        if field_type != "array" and spec.get("items") is not None:
            self.add(file, f"{key}.items", "value")
        if len(self.problems) != before or field_type is None:
            return None
        return Field(
            name=name,
            type=field_type,
            required=required,
            default=default,
            description=description,
            min=minimum,
            max=maximum,
            pattern=pattern or None,
            values=values,
            min_items=min_items,
            items=items,
        )

    def check_field_options(
        self,
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
                self.add(file, f"{key}.{option}", "value")

    def check_default(
        self,
        field_type: str,
        default: object,
        values: tuple[str, ...],
        file: str,
        key: str,
    ) -> None:
        if field_type == "array":
            if not isinstance(default, list):
                self.add(file, key, "type")
        elif not _default_type_matches(field_type, default):
            self.add(file, key, "type")
        elif field_type == "enum" and default not in values:
            self.add(file, key, "value")
