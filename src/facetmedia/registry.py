"""Load and validate a facet registry checkout.

A registry is a directory with an ``index.toml`` and one directory per package
under ``packages/``.  Each package directory holds its ``package.toml``
manifest, the environment's ``pyproject.toml`` and ``uv.lock``, and its
runner.  Person-facing viewers live under ``viewers/``.

Nothing here touches the network: fetching the registry is ``feat(update)``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from facetmedia.contract import FacetError
from facetmedia.manifest import RegistryLoader
from facetmedia.registry_types import (
    PROTOCOL_VERSION,
    Capability,
    Field,
    License,
    Model,
    Package,
    Problem,
    Registry,
    Requirements,
)

__all__ = [
    "PROTOCOL_VERSION",
    "Capability",
    "Field",
    "License",
    "Model",
    "Package",
    "Problem",
    "Registry",
    "Requirements",
    "load_active_registry",
    "load_registry",
    "registry_dir",
    "validate_registry",
]


def _load(root: Path) -> tuple[Registry | None, list[Problem]]:
    loader = RegistryLoader(root)
    registry = loader.load()
    return registry, sorted(loader.problems)


def load_registry(path: Path) -> Registry:
    """Load a registry checkout, raising on the first problem found.

    The message names the file and the dotted key so a registry author can go
    straight to the broken line.
    """

    registry, problems = _load(Path(path))
    if problems:
        first = problems[0]
        raise FacetError(
            "internal",
            f"{first.file}: {first.key}: {first.problem}",
            details={
                "file": first.file,
                "key": first.key,
                "problem": first.problem,
            },
        )
    if registry is None:
        raise FacetError("internal", f"{path}: no index.toml")
    return registry


def validate_registry(path: Path) -> list[dict[str, str]]:
    """Return every problem in the registry at ``path``, sorted, never raising."""

    root = Path(path)
    registry, problems = _load(root)
    found = [*problems, *_package_file_problems(root, registry)]
    return [problem.as_dict() for problem in sorted(found)]


def _package_dirs(root: Path) -> list[Path]:
    packages = root / "packages"
    if not packages.is_dir():
        return []
    return sorted(path for path in packages.iterdir() if path.is_dir())


def _package_file_problems(root: Path, registry: Registry | None) -> list[Problem]:
    # Every package directory needs its environment files, even when its manifest
    # is broken: their names do not depend on the manifest. The entry and the
    # viewers are only known from a valid manifest, so those are checked for
    # loaded packages alone.
    problems = [
        Problem(f"packages/{directory.name}/{name}", "", "missing")
        for directory in _package_dirs(root)
        for name in ("pyproject.toml", "uv.lock")
        if not (directory / name).is_file()
    ]
    if registry is None:
        return problems
    viewers: set[str] = set()
    for package in registry.packages.values():
        base = f"packages/{package.name}"
        if not (package.directory / package.entry).is_file():
            problems.append(Problem(f"{base}/{package.entry}", "", "missing"))
        for capability in package.capabilities:
            if capability.viewer in viewers:
                continue
            viewers.add(capability.viewer)
            viewer_file = root / "viewers" / capability.viewer / "index.html"
            if not viewer_file.is_file():
                problems.append(Problem(f"viewers/{capability.viewer}/index.html", "", "missing"))
    return problems


def registry_dir() -> Path:
    """The registry directory in use: ``FACET_REGISTRY``, else the snapshot."""

    override = os.environ.get("FACET_REGISTRY")
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise FacetError(
                "invalid_input",
                "FACET_REGISTRY must be an absolute path",
                details={"FACET_REGISTRY": override},
            )
        return path
    home = Path(os.environ.get("FACET_HOME") or Path.home() / ".facet")
    state_file = home / "state" / "registry.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FacetError(
            "download_failed",
            "no registry snapshot is available",
            hint="run facet update when online",
        ) from exc
    except (OSError, ValueError) as exc:
        raise FacetError("internal", f"{state_file}: corrupt registry state") from exc
    commit = state.get("commit") if isinstance(state, dict) else None
    if not isinstance(commit, str) or not commit:
        raise FacetError(
            "download_failed",
            "no registry snapshot is in use",
            hint="run facet update when online",
        )
    return home / "registry" / commit


def load_active_registry() -> Registry:
    """Load the registry ``registry_dir`` selects."""

    return load_registry(registry_dir())
