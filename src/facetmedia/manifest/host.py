"""Read and validate a package's ``[engine.host_app]`` table.

``facetmedia.manifest`` keeps the package grammar; the host-app grammar lives
here so the manifest module stays inside the lab's per-file size cap, the same
split ``facetmedia.manifest.node`` makes for ``[engine.node]``.  The loader
passed in is :class:`facetmedia.manifest.RegistryLoader`, which collects every
problem instead of stopping at the first one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from facetmedia.registry_types import HostApp

if TYPE_CHECKING:
    from facetmedia.manifest import RegistryLoader

HOST_APP_KEYS: Final[frozenset[str]] = frozenset({"app", "min_version", "config_key", "env"})
#: The only host applications v0.1 knows, and the lab's env var for each.
HOST_APPS: Final[Mapping[str, str]] = {"blender": "BLENDER_EXE", "freecad": "FREECAD_EXE"}
#: A minimum version is dotted numeric: two components (FreeCAD) or three.
_VERSION_RE: Final[re.Pattern[str]] = re.compile(r"\d+\.\d+(\.\d+)?\Z")
#: A dotted configuration key, e.g. ``blender.path``.
_CONFIG_KEY_RE: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+\Z")
_ENV_RE: Final[re.Pattern[str]] = re.compile(r"[A-Z][A-Z0-9_]*\Z")


def read_host_app(
    loader: RegistryLoader,
    engine_table: Mapping[str, Any],
    file: str,
    engine_type: str,
    transport_type: str,
) -> HostApp | None:
    """The required ``[engine.host_app]`` table for ``type = "host-app"`` packages."""
    if engine_table.get("host_app") is None:
        if engine_type == "host-app":
            loader.add(file, "engine.host_app", "required")
        return None
    before = len(loader.problems)
    table = loader.table(engine_table, "host_app", file, "engine.", required=False)
    if table is None:
        return None
    if (engine_type, transport_type) != ("host-app", "cli"):
        loader.add(file, "engine.host_app", "requires host-app with transport cli")
    loader.unknown(table, HOST_APP_KEYS, file, "engine.host_app.")
    app = loader.string(table, "app", file, "engine.host_app.", required=True) or ""
    if app and app not in HOST_APPS:
        loader.add(file, "engine.host_app.app", "value")
    minimum = loader.string(table, "min_version", file, "engine.host_app.", required=True) or ""
    if minimum and not _VERSION_RE.fullmatch(minimum):
        loader.add(file, "engine.host_app.min_version", "value")
    config_key = _config_key(loader, table, file, app)
    env_name = _env(loader, table, file, app)
    if len(loader.problems) != before:
        return None
    return HostApp(app=app, min_version=minimum, config_key=config_key, env=env_name)


def _config_key(loader: RegistryLoader, table: Mapping[str, Any], file: str, app: str) -> str:
    """``config_key``: ``<app>.path`` unless the manifest names its own."""
    value = loader.string(table, "config_key", file, "engine.host_app.", required=False) or ""
    if not value:
        return f"{app}.path"
    if not _CONFIG_KEY_RE.fullmatch(value):
        loader.add(file, "engine.host_app.config_key", "value")
    return value


def _env(loader: RegistryLoader, table: Mapping[str, Any], file: str, app: str) -> str:
    """``env``: the lab's existing variable name unless the manifest overrides it."""
    value = loader.string(table, "env", file, "engine.host_app.", required=False) or ""
    if not value:
        return HOST_APPS.get(app, "")
    if not _ENV_RE.fullmatch(value):
        loader.add(file, "engine.host_app.env", "value")
    return value
