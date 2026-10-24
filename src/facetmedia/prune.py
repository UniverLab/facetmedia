"""``facet rm`` and ``facet gc``: giving the disk back.

Environments are persistent by default — re-downloading 2–4 GB of torch or
onnx for every run costs more than keeping them — so removing one is always an
explicit act.  ``facet rm`` removes one package's environments (and, on
request, the models only it lists); ``facet gc`` sweeps what nothing uses any
more: stale or unfinished environments, environments of packages the registry
no longer has, models no package lists, registry snapshots and download
scratch, the directories failed runs kept, old logs, and uv's unused cache
entries.  Models survive both unless ``facet rm --models`` is asked, because
downloading them again is the expensive part.

Sizes are measured before anything is deleted, with walks that follow no
symlink, so a link to `/` can never make a plain directory look enormous.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final

import uv

from facetmedia import env as env_module
from facetmedia.contract import FacetError
from facetmedia.home import Home, atomic_write_json, read_json, uv_env
from facetmedia.registry import override_dir, snapshot_commit
from facetmedia.registry_types import Model, Package, Registry

__all__ = [
    "GcItem",
    "dir_bytes",
    "gc_sweep",
    "model_dirnames",
    "record_leftover",
    "rm_env",
    "rm_package",
]

#: Log directories older than this many days are swept.
LOG_DAYS: Final[int] = 30
#: The state file ``run`` appends kept temporary directories to.
LEFTOVERS: Final[str] = "leftovers.json"
UV_PRUNE_TIMEOUT_S: Final[float] = 600.0
#: Why an item is garbage; one fixed string per family, part of the contract.
INCOMPLETE_ENV: Final[str] = "incomplete-env"
STALE_VERSION: Final[str] = "stale-version"
ORPHAN_PACKAGE: Final[str] = "orphan-package"
ORPHAN_MODEL: Final[str] = "orphan-model"
PARTIAL_SNAPSHOT: Final[str] = "partial-snapshot"
OLD_SNAPSHOT: Final[str] = "old-snapshot"
TMP: Final[str] = "tmp"
LEFTOVER: Final[str] = "leftover"
OLD_LOG: Final[str] = "old-log"
UV_CACHE: Final[str] = "uv-cache"


@dataclass(frozen=True)
class GcItem:
    """One path ``facet gc`` removes, with its measured size and its reason."""

    path: Path
    bytes: int
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {"path": str(self.path), "bytes": self.bytes, "reason": self.reason}


# --- measuring and deleting, the two primitives every verb here shares.


def dir_bytes(path: Path) -> int:
    """Bytes under ``path``: a file's size, or a walk that follows no symlink."""
    try:
        info = path.lstat()
    except OSError:
        return 0
    if not stat.S_ISDIR(info.st_mode):
        return 0 if stat.S_ISLNK(info.st_mode) else info.st_size
    total = 0
    try:
        entries = list(os.scandir(path))
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                total += dir_bytes(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return total


def _children(path: Path) -> list[Path]:
    """The entries of ``path``, sorted; ``[]`` when it does not exist."""
    try:
        return sorted(Path(entry.path) for entry in os.scandir(path))
    except OSError:
        return []


def _delete(path: Path) -> None:
    """Remove a file, a link, or a whole tree; a missing path is not an error."""
    try:
        if path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
            return
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        return


def model_dirnames(models: Iterable[Model]) -> set[str]:
    """The ``models/direct`` entry each model owns: its id, or its path's root.

    A ``url`` model with a ``path`` lives under ``models/direct/<path>``, so
    its storage is the first path component; every other model is a directory
    named after its id.
    """
    names: set[str] = set()
    for model in models:
        names.add(model.id)
        if model.path:
            names.add(model.path.split("/")[0])
    return names


# --- state: the kept temporary directories failed runs leave for gc.


def record_leftover(home: Home, path: Path) -> None:
    """Append ``path``'s absolute name to ``state/leftovers.json``, atomically."""
    state = home.state_file(LEFTOVERS)
    listed: list[str] = _leftover_paths(state)
    absolute = str(path.resolve())
    if absolute in listed:
        return
    atomic_write_json(state, [*listed, absolute], home.tmp_dir)


def _leftover_paths(state: Path) -> list[str]:
    """The absolute paths ``state/leftovers.json`` lists; ``[]`` when absent."""
    listed: object = read_json(state, [])
    if not isinstance(listed, list):
        return []
    return [item for item in listed if isinstance(item, str) and Path(item).is_absolute()]


# --- facet rm.


def rm_env(home: Home, name: str, version: str) -> None:
    """Delete one environment and its package directory when it becomes empty."""
    _delete(home.env_dir(name, version))
    _rmdir_if_empty(home.envs_dir / name)


def rm_package(
    home: Home,
    name: str,
    with_models: bool = False,
    registry: Registry | None = None,
) -> dict[str, object]:
    """Delete every environment of ``name``; ``with_models`` adds its own models.

    A package that is not installed is a no-op: removing twice is safe.  With
    ``with_models`` a model survives when another *installed* package lists it,
    so removing one tool never breaks the next one.
    """
    removed: list[str] = []
    freed = 0
    for version_dir in _children(home.envs_dir / name):
        freed += dir_bytes(version_dir)
        removed.append(str(version_dir))
        _delete(version_dir)
    _rmdir_if_empty(home.envs_dir / name)
    if with_models and registry is not None:
        package = registry.package(name)
        if package is not None:
            for model_dir in _removable_models(home, package, registry):
                freed += dir_bytes(model_dir)
                removed.append(str(model_dir))
                _delete(model_dir)
    return {"removed": removed, "freed_bytes": freed}


def _rmdir_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        return


def _removable_models(home: Home, package: Package, registry: Registry) -> list[Path]:
    """This package's model directories that no other installed package lists."""
    shared = _installed_model_dirnames(home, registry, exclude=package.name)
    found: list[Path] = []
    for name in sorted(model_dirnames(package.models) - shared):
        target = home.model_dir(name)
        if target.exists() or target.is_symlink():
            found.append(target)
    return found


def _installed_model_dirnames(home: Home, registry: Registry, exclude: str) -> set[str]:
    """The model directories every package installed under ``envs/`` but ``exclude`` lists."""
    names: set[str] = set()
    for package_dir in _children(home.envs_dir):
        if package_dir.name == exclude or not _children(package_dir):
            continue
        package = registry.package(package_dir.name)
        if package is not None:
            names |= model_dirnames(package.models)
    return names


# --- facet gc.


def gc_sweep(home: Home, registry: Registry, dry_run: bool = False) -> dict[str, object]:
    """List — and, unless ``dry_run``, delete — everything nothing uses any more."""
    items = [
        *_env_items(home, registry),
        *_model_items(home, registry),
        *_snapshot_items(home),
        *_tmp_items(home),
        *_leftover_items(home),
        *_log_items(home),
    ]
    if dry_run:
        return _dry_run_result(home, items)
    return _sweep(home, items)


def _dry_run_result(home: Home, items: list[GcItem]) -> dict[str, object]:
    """What a real sweep would remove; nothing is touched."""
    planned = [*items, GcItem(home.uv_cache_dir, dir_bytes(home.uv_cache_dir), UV_CACHE)]
    return {
        "items": [item.as_dict() for item in planned],
        "freed_bytes": sum(item.bytes for item in planned),
        "dry_run": True,
    }


def _sweep(home: Home, items: list[GcItem]) -> dict[str, object]:
    """Delete every measured item, clear the leftovers list, prune uv's cache."""
    for item in items:
        _delete(item.path)
    _clear_leftovers(home)
    before = dir_bytes(home.uv_cache_dir)
    _uv_cache_prune(home)
    after = dir_bytes(home.uv_cache_dir)
    uv_item = GcItem(home.uv_cache_dir, max(0, before - after), UV_CACHE)
    swept = [*items, uv_item]
    return {
        "items": [item.as_dict() for item in swept],
        "freed_bytes": sum(item.bytes for item in swept),
        "dry_run": False,
    }


def _clear_leftovers(home: Home) -> None:
    """Empty ``state/leftovers.json`` once its directories are gone."""
    state = home.state_file(LEFTOVERS)
    if state.is_file():
        atomic_write_json(state, [], home.tmp_dir)


def _env_items(home: Home, registry: Registry) -> list[GcItem]:
    """Environment versions that are stale, unfinished, or of a gone package."""
    items: list[GcItem] = []
    for package_dir in _children(home.envs_dir):
        package = registry.package(package_dir.name)
        for version_dir in _children(package_dir):
            reason = _env_reason(package, version_dir)
            if reason is not None:
                items.append(GcItem(version_dir, dir_bytes(version_dir), reason))
    return items


def _env_reason(package: Package | None, version_dir: Path) -> str | None:
    """Why one ``envs/<package>/<version>`` directory is garbage, or ``None``."""
    if package is None:
        return ORPHAN_PACKAGE
    if version_dir.name != package.version:
        return STALE_VERSION
    if not (version_dir / env_module.COMPLETE).is_file():
        return INCOMPLETE_ENV
    return None


def _model_items(home: Home, registry: Registry) -> list[GcItem]:
    """``models/direct`` entries no package of the snapshot in use lists."""
    known: set[str] = set()
    for package in registry.packages.values():
        known |= model_dirnames(package.models)
    return [
        GcItem(entry, dir_bytes(entry), ORPHAN_MODEL)
        for entry in _children(home.models_dir / "direct")
        if entry.name not in known
    ]


def _snapshot_items(home: Home) -> list[GcItem]:
    """Snapshots other than the one in use, plus unfinished ``.partial`` ones.

    With ``FACET_REGISTRY`` pointing at a checkout there is no snapshot in use:
    every directory under ``registry/`` is then someone's own business.
    """
    if override_dir() is not None:
        return []
    in_use = snapshot_commit()
    items: list[GcItem] = []
    for entry in _children(home.root / "registry"):
        if entry.name.endswith(".partial"):
            items.append(GcItem(entry, dir_bytes(entry), PARTIAL_SNAPSHOT))
        elif entry.name != in_use:
            items.append(GcItem(entry, dir_bytes(entry), OLD_SNAPSHOT))
    return items


def _tmp_items(home: Home) -> list[GcItem]:
    """Everything in ``tmp/``: partial downloads and abandoned scratch."""
    return [GcItem(entry, dir_bytes(entry), TMP) for entry in _children(home.tmp_dir)]


def _leftover_items(home: Home) -> list[GcItem]:
    """The kept ``.facet-tmp`` directories ``state/leftovers.json`` lists.

    gc never searches the disk for these: only what a failed run recorded.
    """
    state = home.state_file(LEFTOVERS)
    paths = [Path(item) for item in _leftover_paths(state)]
    return [GcItem(path, dir_bytes(path), LEFTOVER) for path in paths if path.exists()]


def _log_items(home: Home, today: date | None = None) -> list[GcItem]:
    """Log directories older than ``LOG_DAYS``; unparseable names are left alone."""
    cutoff = (today or datetime.now(UTC).date()) - timedelta(days=LOG_DAYS)
    items: list[GcItem] = []
    for entry in _children(home.logs_dir):
        day = _parse_day(entry.name)
        if day is not None and day < cutoff:
            items.append(GcItem(entry, dir_bytes(entry), OLD_LOG))
    return items


def _parse_day(name: str) -> date | None:
    try:
        return datetime.strptime(name, "%Y-%m-%d").replace(tzinfo=UTC).date()
    except ValueError:
        return None


def _uv_cache_prune(home: Home) -> None:
    """Run ``uv cache prune`` with facet's ``UV_CACHE_DIR``."""
    argv = [uv.find_uv_bin(), "cache", "prune"]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            env=uv_env(home),
            timeout=UV_PRUNE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FacetError("download_failed", f"uv cache prune: {exc}") from exc
    if result.returncode != 0:
        raise FacetError(
            "download_failed",
            f"uv cache prune failed with exit code {result.returncode}",
            details={
                "exit": result.returncode,
                "output": (result.stderr or result.stdout)[-2000:],
            },
        )
    sys.stderr.write(result.stdout)
