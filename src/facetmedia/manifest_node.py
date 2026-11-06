"""Read and validate a package's ``[engine.node]`` table.

``facetmedia.manifest`` keeps the package grammar; the node grammar lives
here so the manifest module stays inside the lab's per-file size cap, the
same split ``facetmedia.manifest_server`` makes for ``[engine.server]``.
The loader passed in is :class:`facetmedia.manifest.RegistryLoader`, which
collects every problem instead of stopping at the first one.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

from facetmedia.registry_types import Node, NodePackage

if TYPE_CHECKING:
    from facetmedia.manifest import RegistryLoader

NODE_KEYS: Final[frozenset[str]] = frozenset({"runtime", "runtime_sha256", "packages"})
PACKAGE_KEYS: Final[frozenset[str]] = frozenset({"name", "version", "tarball", "integrity"})
NPM_REGISTRY_HOST: Final[str] = "registry.npmjs.org"
_INTEGRITY_ALGOS: Final[frozenset[str]] = frozenset({"sha256", "sha384", "sha512"})
#: An npm name: bare, or scoped as ``@scope/name``; each segment starts alnum.
_NAME_RE: Final[re.Pattern[str]] = re.compile(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*\Z")
_RUNTIME_RE: Final[re.Pattern[str]] = re.compile(r"\d+\.\d+\.\d+\Z")
_SEMVER_RE: Final[re.Pattern[str]] = re.compile(
    r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\Z"
)
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}\Z")


def _semver(value: str) -> bool:
    return bool(_SEMVER_RE.fullmatch(value))


def _integrity(value: str) -> bool:
    """True for an SRI hash: ``<algo>-<base64>``, algo one of sha256/384/512."""
    algo, sep, digest = value.partition("-")
    if not sep or algo not in _INTEGRITY_ALGOS or not digest:
        return False
    try:
        base64.b64decode(digest, validate=True)
    except (binascii.Error, ValueError):
        return False
    return True


def _npm_tarball(value: str) -> bool:
    return urlsplit(value).hostname == NPM_REGISTRY_HOST


def read_node(
    loader: RegistryLoader,
    engine_table: Mapping[str, Any],
    file: str,
    engine_type: str,
    transport_type: str,
) -> Node | None:
    """The required ``[engine.node]`` table for ``type = "node"`` packages."""
    if engine_table.get("node") is None:
        if engine_type == "node":
            loader.add(file, "engine.node", "required")
        return None
    before = len(loader.problems)
    table = loader.table(engine_table, "node", file, "engine.", required=False)
    if table is None:
        return None
    if (engine_type, transport_type) != ("node", "cli"):
        loader.add(file, "engine.node", "requires node with transport cli")
    loader.unknown(table, NODE_KEYS, file, "engine.node.")
    runtime = loader.string(table, "runtime", file, "engine.node.", required=True) or ""
    if runtime and not _RUNTIME_RE.fullmatch(runtime):
        loader.add(file, "engine.node.runtime", "value")
    sha256 = loader.string(table, "runtime_sha256", file, "engine.node.", required=True) or ""
    if sha256 and not _SHA256_RE.fullmatch(sha256):
        loader.add(file, "engine.node.runtime_sha256", "value")
    packages = _packages(loader, table, file)
    if len(loader.problems) != before:
        return None
    return Node(runtime=runtime, runtime_sha256=sha256, packages=packages)


def _packages(
    loader: RegistryLoader, table: Mapping[str, Any], file: str
) -> tuple[NodePackage, ...]:
    """The ``[[engine.node.packages]]`` entries; the first is the entry package."""
    raw = table.get("packages")
    if raw is None:
        loader.add(file, "engine.node.packages", "required")
        return ()
    if not isinstance(raw, list):
        loader.add(file, "engine.node.packages", "type")
        return ()
    if not raw:
        loader.add(file, "engine.node.packages", "value")
        return ()
    found: list[NodePackage] = []
    for position, item in enumerate(raw):
        prefix = f"engine.node.packages[{position}]."
        if not isinstance(item, dict):
            loader.add(file, f"{prefix}name", "type")
            continue
        spec = _package(loader, item, file, prefix)
        if spec is not None:
            found.append(spec)
    return tuple(found)


def _package(
    loader: RegistryLoader, item: Mapping[str, Any], file: str, prefix: str
) -> NodePackage | None:
    """One package entry; every bad field lands on its own dotted key."""
    before = len(loader.problems)
    loader.unknown(item, PACKAGE_KEYS, file, prefix)
    name = loader.string(item, "name", file, prefix, required=True) or ""
    if name and not _NAME_RE.fullmatch(name):
        loader.add(file, f"{prefix}name", "value")
    version = loader.string(item, "version", file, prefix, required=True) or ""
    if version and not _semver(version):
        loader.add(file, f"{prefix}version", "value")
    tarball = loader.string(item, "tarball", file, prefix, required=True) or ""
    if tarball and not _npm_tarball(tarball):
        loader.add(file, f"{prefix}tarball", "value")
    integrity = loader.string(item, "integrity", file, prefix, required=True) or ""
    if integrity and not _integrity(integrity):
        loader.add(file, f"{prefix}integrity", "value")
    if len(loader.problems) != before:
        return None
    return NodePackage(name=name, version=version, tarball=tarball, integrity=integrity)
