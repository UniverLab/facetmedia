"""The registry's value types: packages, models, capabilities and input fields.

Everything here is frozen data.  Reading and validating a registry checkout is
``facetmedia.manifest`` and ``facetmedia.registry``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Final

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version

from facetmedia import __version__
from facetmedia.contract import FacetError

PROTOCOL_VERSION: Final[int] = 1
DEFAULT_INVOCATION_TEMPLATE: Final[str] = (
    "{{python}} -I {{entry}} {{verb}} --input {{input}} --result {{result}}"
)

CAPABILITY_KINDS: Final[frozenset[str]] = frozenset(
    {"audio", "video", "image", "cad", "presentation", "text"}
)
FIELD_TYPES: Final[frozenset[str]] = frozenset(
    {"string", "integer", "number", "boolean", "enum", "path", "array"}
)

MISSING: Final[object] = object()


@dataclass(frozen=True, order=True)
class Problem:
    """One validation problem, located by file and dotted key."""

    file: str
    key: str
    problem: str

    def as_dict(self) -> dict[str, str]:
        return {"file": self.file, "key": self.key, "problem": self.problem}


@dataclass(frozen=True)
class Field:
    """One input field of a capability, in the registry's small schema language."""

    name: str
    type: str
    required: bool = False
    default: object = MISSING
    description: str = ""
    min: float | None = None
    max: float | None = None
    pattern: str | None = None
    values: tuple[str, ...] = ()
    min_items: int | None = None
    items: tuple[Field, ...] = ()

    @property
    def has_default(self) -> bool:
        return self.default is not MISSING


@dataclass(frozen=True)
class Requirements:
    """What a package needs from the machine and the lock."""

    gpu: bool = False
    ram_gb: float = 0.0
    disk_gb: float = 0.0
    exclusive: bool = True
    platforms: tuple[str, ...] = ("linux",)
    host_libs: tuple[str, ...] = ()
    host_hint: str = ""


@dataclass(frozen=True)
class License:
    """The license of the tool or one notable dependency."""

    subject: str
    spdx: str


@dataclass(frozen=True)
class Model:
    """One downloadable model, direct URL or Hugging Face repository."""

    id: str
    spdx: str
    commercial_use: bool
    requires_acceptance: bool
    bytes: int
    terms_url: str = ""
    url: str = ""
    sha256: str = ""
    path: str = ""
    hf_repo: str = ""
    revision: str = ""

    @property
    def source(self) -> str:
        if self.url:
            return self.url
        return f"hf:{self.hf_repo}@{self.revision}"


@dataclass(frozen=True)
class Capability:
    """One thing a package can make: ``<package>.<name>``."""

    id: str
    package: str
    summary: str
    kind: str
    inputs: tuple[Field, ...] = ()
    thresholds: Mapping[str, float] = dc_field(default_factory=dict)
    viewer: str = "generic"

    def input(self, name: str) -> Field | None:
        for field in self.inputs:
            if field.name == name:
                return field
        return None


@dataclass(frozen=True)
class Package:
    """One package directory: its manifest, lock and capabilities."""

    name: str
    version: str
    requires_facet: str
    protocol: int
    provider: str
    tool_name: str
    description: str
    engine_type: str
    python: str
    transport_type: str
    entry: str
    invocation_template: str
    requirements: Requirements
    directory: Path
    licenses: tuple[License, ...] = ()
    models: tuple[Model, ...] = ()
    capabilities: tuple[Capability, ...] = ()

    @property
    def dir(self) -> Path:
        return self.directory

    @property
    def lock_path(self) -> Path:
        return self.directory / "uv.lock"

    @property
    def lock_sha256(self) -> str:
        try:
            data = self.lock_path.read_bytes()
        except OSError:
            return ""
        return hashlib.sha256(data).hexdigest()

    @property
    def compatible(self) -> bool:
        if self.protocol != PROTOCOL_VERSION:
            return False
        try:
            specifier = SpecifierSet(self.requires_facet)
            return specifier.contains(Version(__version__), prereleases=True)
        except InvalidSpecifier:
            return False

    def require_compatible(self) -> None:
        if self.compatible:
            return
        raise FacetError(
            "machine_cannot",
            f"package {self.name} cannot run with facet {__version__}",
            hint="facet update, or upgrade facet",
            details={
                "package": self.name,
                "requires_facet": self.requires_facet,
                "protocol": self.protocol,
            },
        )


@dataclass(frozen=True)
class Registry:
    """A validated registry checkout, indexed by package name."""

    root: Path
    version: int
    packages: Mapping[str, Package]

    def package(self, name: str) -> Package | None:
        return self.packages.get(name)

    def capabilities(self) -> tuple[Capability, ...]:
        found = [cap for package in self.packages.values() for cap in package.capabilities]
        return tuple(sorted(found, key=lambda cap: cap.id))

    def capability(self, cap_id: str) -> Capability:
        for cap in self.capabilities():
            if cap.id == cap_id:
                return cap
        known = [cap.id for cap in self.capabilities()]
        raise FacetError(
            "invalid_input",
            f"unknown capability: {cap_id}",
            details={"known": known},
        )
