"""Explicit host-application configuration: ``<home>/config.toml``.

facet never installs Blender or FreeCAD, and never guesses: a host-app
package names a dotted config key (``blender.path``), a person stores the
binary's location there, and the resolver tries the config file, then the
lab's environment variable, then ``PATH``.  A key that no host-app package in
the active registry declares is refused, so a typo fails loudly instead of
being ignored.

The known keys are exactly the ``config_key`` values the active registry's
host-app packages declare; ``facet config`` reads them from the registry
rather than keeping a second list that could drift.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from facetmedia.contract import FacetError
from facetmedia.home import Home, atomic_write_text
from facetmedia.registry_types import HostApp, Registry

__all__ = [
    "CONFIG_NAME",
    "HostAppPath",
    "HostAppProblem",
    "config_file",
    "get_value",
    "host_app_installed",
    "host_app_problem",
    "known_apps",
    "list_values",
    "resolve",
    "set_value",
]

CONFIG_NAME: Final[str] = "config.toml"
#: How long one ``<binary> --version`` probe may take.
PROBE_TIMEOUT_S: Final[float] = 10.0
_VERSION_RE: Final[re.Pattern[str]] = re.compile(r"\d+\.\d+(?:\.\d+)?")
#: The PATH names a host application may answer to, in order.
PATH_NAMES: Final[dict[str, tuple[str, ...]]] = {
    "blender": ("blender",),
    "freecad": ("freecadcmd", "freecad"),
}
#: Components a version comparison pads to: 4.0 and 4.0.0 are the same version.
VERSION_PARTS: Final[int] = 3


@dataclass(frozen=True)
class HostAppPath:
    """A resolved host binary and where the resolver found it."""

    path: str
    location: str


@dataclass(frozen=True)
class HostAppProblem:
    """Why a host application is unusable: the exact message and its fix."""

    message: str
    fix: str
    details: dict[str, object]


def config_file(home: Home) -> Path:
    """``<home>/config.toml``: the file ``facet config`` owns."""
    return home.root / CONFIG_NAME


def known_apps(registry: Registry) -> dict[str, HostApp]:
    """The config-key universe: every host-app package's declared key."""
    return {
        package.host_app.config_key: package.host_app
        for package in registry.packages.values()
        if package.host_app is not None
    }


def read_values(home: Home) -> dict[str, str]:
    """Every dotted key and string value in the config file; ``{}`` when absent."""
    path = config_file(home)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise FacetError("internal", f"{path}: cannot read the config file: {exc}") from exc
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise FacetError("invalid_input", f"{path}: not valid TOML: {exc}") from exc
    values: dict[str, str] = {}
    _flatten(data, "", values, path)
    return values


def _flatten(data: object, prefix: str, values: dict[str, str], path: Path) -> None:
    """Turn nested tables back into the dotted keys ``facet config`` speaks."""
    if not isinstance(data, dict):
        raise FacetError("invalid_input", f"{path}: the config file must hold tables")
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            _flatten(value, f"{dotted}.", values, path)
        elif isinstance(value, str):
            values[dotted] = value
        else:
            raise FacetError("invalid_input", f"{path}: {dotted} must be a string")


def _write_values(home: Home, values: dict[str, str]) -> None:
    """Write every dotted key atomically, so a crash leaves the old file intact."""
    lines = [f'{key} = "{_escape(value)}"' for key, value in sorted(values.items())]
    atomic_write_text(config_file(home), "\n".join(lines) + "\n" if lines else "", home.tmp_dir)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _require_key(key: str, apps: dict[str, HostApp]) -> HostApp:
    app = apps.get(key)
    if app is None:
        raise FacetError(
            "invalid_input",
            f"unknown config key: {key}",
            details={"known": sorted(apps)},
        )
    return app


def set_value(home: Home, registry: Registry, key: str, value: str) -> dict[str, object]:
    """Store one host application path; an unknown key is ``invalid_input``."""
    apps = known_apps(registry)
    _require_key(key, apps)
    if not value:
        raise FacetError("invalid_input", f"{key}: the value must not be empty")
    values = read_values(home)
    values[key] = value
    _write_values(home, values)
    return {"key": key, "value": value, "path": str(config_file(home))}


def get_value(home: Home, registry: Registry, key: str) -> dict[str, object]:
    """One key's value; ``set`` says whether the file carries it."""
    _require_key(key, known_apps(registry))
    values = read_values(home)
    return {
        "key": key,
        "value": values.get(key, ""),
        "set": key in values,
        "path": str(config_file(home)),
    }


def list_values(home: Home, registry: Registry) -> dict[str, object]:
    """Every known key, its host application, and its configured value."""
    apps = known_apps(registry)
    values = read_values(home)
    return {
        "keys": [
            {
                "key": key,
                "app": apps[key].app,
                "value": values.get(key, ""),
                "set": key in values,
            }
            for key in sorted(apps)
        ],
        "path": str(config_file(home)),
    }


def resolve(home: Home, app: HostApp) -> HostAppPath | None:
    """The binary and its source: config key, then env var, then ``PATH``."""
    configured = read_values(home).get(app.config_key, "")
    if configured:
        return HostAppPath(configured, f"config {app.config_key}")
    from_env = os.environ.get(app.env, "")
    if from_env:
        return HostAppPath(from_env, f"env {app.env}")
    for name in PATH_NAMES.get(app.app, (app.app,)):
        found = shutil.which(name)
        if found:
            return HostAppPath(found, "PATH")
    return None


def host_app_installed(app: HostApp, home: Home) -> bool:
    """True exactly when the app resolves and its version meets the minimum."""
    return host_app_problem(app, home) is None


def host_app_problem(app: HostApp, home: Home) -> HostAppProblem | None:
    """``None`` when the host app is usable, else the exact failure and its fix."""
    fix = f"facet config set {app.config_key} /path/to/{app.app}"
    resolved = resolve(home, app)
    if resolved is None:
        message = (
            f"{app.app} {app.min_version}+ not found "
            f"(config {app.config_key}, env {app.env}, PATH all empty); fix: {fix}"
        )
        return HostAppProblem(message, fix, {"app": app.app, "locations": "none"})
    version = probe_version(resolved.path)
    if version is None:
        message = f"{app.app} {app.min_version}+ not found ({resolved.location}); fix: {fix}"
        return HostAppProblem(message, fix, {"app": app.app, "path": resolved.path})
    if _version_tuple(version) < _version_tuple(app.min_version):
        message = (
            f"{app.app} {version} found, needs {app.min_version}+ ({resolved.location}); fix: {fix}"
        )
        return HostAppProblem(
            message,
            fix,
            {"app": app.app, "path": resolved.path, "found": version, "needs": app.min_version},
        )
    return None


def probe_version(binary: str) -> str | None:
    """The first dotted version ``<binary> --version`` prints, or ``None``."""
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    found = _VERSION_RE.search(f"{result.stdout}\n{result.stderr}")
    return found.group(0) if found is not None else None


def _version_tuple(value: str) -> tuple[int, ...]:
    """A dotted version padded to three components, so ``4.0`` equals ``4.0.0``."""
    parts = [int(part) for part in value.split(".") if part.isdigit()]
    return tuple(parts + [0] * (VERSION_PARTS - len(parts)))[:VERSION_PARTS]
