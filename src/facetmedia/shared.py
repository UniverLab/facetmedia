"""Shared registry modules: the ``lib/<name>.py`` files a package declares.

A package's manifest lists the helpers it uses in ``uses = [...]``.  ``pull``
copies each one into the environment as ``facet_lib/<name>.py`` so a runner
imports ``from facet_lib import <name>``; the copy is versioned by the
registry snapshot, not by a release of its own.

The environment remembers what it copied in ``.registry``, next to
``.complete``.  The fingerprint is the pinned snapshot commit, or — with
``FACET_REGISTRY`` set — a hash of the used modules and the package manifest,
so editing the checkout makes the next pull rebuild.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Final

from facetmedia.registry import override_dir, snapshot_commit
from facetmedia.registry_types import Package

__all__ = [
    "MARKER",
    "PACKAGE_DIR",
    "check",
    "copy_into",
    "fingerprint",
    "matches",
    "module_path",
]

#: The marker file, written last next to ``.complete``.
MARKER: Final[str] = ".registry"
#: The package name the modules are importable under inside the environment.
PACKAGE_DIR: Final[str] = "facet_lib"
LIB_DIR: Final[str] = "lib"
INIT_FILE: Final[str] = "__init__.py"
MANIFEST_FILE: Final[str] = "package.toml"


def registry_root(package: Package) -> Path:
    """The registry checkout a package belongs to (``lib/`` lives here)."""
    return package.directory.parent.parent


def module_path(package: Package, name: str) -> Path:
    """The ``lib/<name>.py`` file a package declares."""
    return registry_root(package) / LIB_DIR / f"{name}.py"


def fingerprint(package: Package) -> str:
    """What an environment's ``.registry`` marker must hold, ``""`` without uses."""
    if not package.uses:
        return ""
    commit = None if override_dir() is not None else snapshot_commit()
    if commit is not None:
        return commit
    digest = hashlib.sha256()
    for name in sorted(package.uses):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(module_path(package, name).read_bytes())
        digest.update(b"\0")
    digest.update((package.directory / MANIFEST_FILE).read_bytes())
    return f"override:{digest.hexdigest()}"


def matches(package: Package, env_dir: Path) -> bool:
    """Whether the environment's ``.registry`` marker holds this fingerprint."""
    try:
        stored = (env_dir / MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return stored == fingerprint(package)


def check(package: Package, env_dir: Path) -> tuple[bool, str, str]:
    """The doctor's outcome for the copied modules: ``ok``, ``detail``, ``fix``."""
    if not package.uses:
        return True, "no shared modules declared", ""
    if not env_dir.is_dir():
        return True, "no environment to be stale", ""
    if matches(package, env_dir):
        return True, f"{len(package.uses)} shared module(s) present", ""
    return False, f"{env_dir}: the shared modules are stale", f"facet pull {package.name}"


def copy_into(package: Package, site_packages: Path) -> None:
    """Copy each used module into ``facet_lib`` under ``site_packages``."""
    target = site_packages / PACKAGE_DIR
    target.mkdir(parents=True, exist_ok=True)
    (target / INIT_FILE).write_text("", encoding="utf-8")
    for name in package.uses:
        shutil.copy2(module_path(package, name), target / f"{name}.py")
