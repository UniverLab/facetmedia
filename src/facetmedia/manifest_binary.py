"""Read and validate a package's ``[engine.binary]`` table.

``facetmedia.manifest`` keeps the package grammar; the binary grammar lives
here so the manifest module stays inside the lab's per-file size cap, the
same split ``facetmedia.manifest_node`` makes for ``[engine.node]``.  The
loader passed in is :class:`facetmedia.manifest.RegistryLoader`, which
collects every problem instead of stopping at the first one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from facetmedia.registry_types import Binary

if TYPE_CHECKING:
    from facetmedia.manifest import RegistryLoader

BINARY_KEYS: Final[frozenset[str]] = frozenset(
    {"name", "version", "url", "sha256", "bin", "version_arg"}
)
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}\Z")
#: One directory under ``<home>/bin``: never ``.``, ``..`` or a separator.
_SEGMENT_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
DEFAULT_VERSION_ARG: Final[str] = "--version"


def _inside_archive(value: str) -> bool:
    """True for a non-empty relative path that stays inside the extraction."""
    path = Path(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts


def read_binary(
    loader: RegistryLoader,
    engine_table: Mapping[str, Any],
    file: str,
    engine_type: str,
    transport_type: str,
    template: str,
) -> Binary | None:
    """The required ``[engine.binary]`` table for ``type = "binary"`` packages."""
    if engine_table.get("binary") is None:
        if engine_type == "binary":
            loader.add(file, "engine.binary", "required")
        return None
    before = len(loader.problems)
    table = loader.table(engine_table, "binary", file, "engine.", required=False)
    if table is None:
        return None
    if (engine_type, transport_type) != ("binary", "cli"):
        loader.add(file, "engine.binary", "requires binary with transport cli")
    if engine_type == "binary" and "{{binary}}" not in template:
        loader.add(file, "transport.invocation_template", "value")
    loader.unknown(table, BINARY_KEYS, file, "engine.binary.")
    name = loader.string(table, "name", file, "engine.binary.", required=True) or ""
    if name and not _SEGMENT_RE.fullmatch(name):
        loader.add(file, "engine.binary.name", "value")
    version = loader.string(table, "version", file, "engine.binary.", required=True) or ""
    if version and not _SEGMENT_RE.fullmatch(version):
        loader.add(file, "engine.binary.version", "value")
    url = loader.string(table, "url", file, "engine.binary.", required=True) or ""
    if url and not url.startswith("https://"):
        loader.add(file, "engine.binary.url", "value")
    sha256 = loader.string(table, "sha256", file, "engine.binary.", required=True) or ""
    if sha256 and not _SHA256_RE.fullmatch(sha256):
        loader.add(file, "engine.binary.sha256", "value")
    bin_path = loader.string(table, "bin", file, "engine.binary.", required=True) or ""
    if bin_path and not _inside_archive(bin_path):
        loader.add(file, "engine.binary.bin", "value")
    version_arg = (
        loader.string(table, "version_arg", file, "engine.binary.", required=False)
        or DEFAULT_VERSION_ARG
    )
    if len(loader.problems) != before:
        return None
    return Binary(
        name=name,
        version=version,
        url=url,
        sha256=sha256,
        bin=bin_path,
        version_arg=version_arg,
    )
