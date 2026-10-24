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
import sys
from pathlib import Path

from facetmedia.contract import FacetError
from facetmedia.home import Home
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
    "override_dir",
    "registry_dir",
    "snapshot_commit",
    "snapshot_identity",
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


def override_dir() -> Path | None:
    """The ``FACET_REGISTRY`` development directory, or ``None`` when unset.

    The override is validated here and nowhere else, so the directory a run
    reads and the directory an asset record names are always the same one.
    """
    override = os.environ.get("FACET_REGISTRY")
    if not override:
        return None
    path = Path(override)
    if not path.is_absolute():
        raise FacetError(
            "invalid_input",
            "FACET_REGISTRY must be an absolute path",
            details={"FACET_REGISTRY": override},
        )
    return path


def registry_dir() -> Path:
    """The registry directory in use: ``FACET_REGISTRY``, else the snapshot.

    An empty facet home fetches the registry once, before the first use, so a
    fresh install works with one command.
    """

    override = override_dir()
    if override is not None:
        return override
    home = Home.peek_env()
    commit = _recorded_commit(home)
    if commit is None:
        commit = _first_use(home)
    return home.registry_dir(commit)


def snapshot_commit() -> str | None:
    """The commit of the snapshot ``facet update`` recorded, or ``None``.

    Asset records name this commit, so the registry a run read is never
    guessed.
    """
    return _recorded_commit(Home.peek_env())


def snapshot_identity() -> dict[str, str]:
    """Which snapshot is in use: the ``FACET_REGISTRY`` path, else commit+ref.

    ``facet list`` reports this so a client knows what it discovered against;
    with the override set the path says it, otherwise ``state/registry.json``
    names the commit ``facet update`` pinned and the ref it pinned from.
    """
    override = override_dir()
    if override is not None:
        return {"path": str(override)}
    state = _recorded_state(Home.peek_env())
    commit = state.get("commit")
    ref = state.get("ref")
    return {
        "commit": commit if isinstance(commit, str) else "",
        "ref": ref if isinstance(ref, str) else "",
    }


def _recorded_commit(home: Home) -> str | None:
    """The commit ``state/registry.json`` records, or ``None`` when absent."""
    commit = _recorded_state(home).get("commit")
    if not isinstance(commit, str) or not commit:
        return None
    return commit


def _recorded_state(home: Home) -> dict[str, object]:
    """The parsed ``state/registry.json``; ``{}`` when absent or not a table."""
    state_file = home.state_file("registry.json")
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise FacetError("internal", f"{state_file}: corrupt registry state") from exc
    return state if isinstance(state, dict) else {}


def _first_use(home: Home) -> str:
    """Fetch the registry once, then return the commit it recorded.

    No lock is taken: a caller such as ``facet update`` already holds the
    exclusive lock, and a second descriptor on the same file would deadlock.
    """
    print("fetching the registry (first use)", file=sys.stderr)
    # Imported here because facetmedia.update imports this module: a top-level
    # import would be a cycle.
    from facetmedia import update  # noqa: PLC0415

    update.update_registry(home.ensure(), update.default_ref())
    commit = _recorded_commit(home)
    if commit is None:
        raise FacetError(
            "download_failed",
            "no registry snapshot is available",
            hint="run facet update when online",
        )
    return commit


def load_active_registry() -> Registry:
    """Load the registry ``registry_dir`` selects."""

    return load_registry(registry_dir())
