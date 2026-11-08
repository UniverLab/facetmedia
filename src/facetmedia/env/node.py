"""The ``node`` engine's staged entry: ``<env>/package`` and its marker.

A node package has no python environment, but it still runs its own entry
script: ``facet pull`` copies the whole package directory to
``<env>/package/`` and writes ``.complete`` holding the sha256 of the source
entry file's bytes.  A later pull restages from scratch when that marker no
longer matches, so a registry edit is picked up and an interrupted copy is
redone; the staged tree is never merged into.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from facetmedia.home import Home, atomic_write_text
from facetmedia.registry_types import Package


def _marker(env_dir: Path) -> Path:
    """The staging marker inside ``env_dir``, read lazily to avoid a cycle."""
    from facetmedia.env import COMPLETE  # noqa: PLC0415

    return env_dir / COMPLETE


def _entry_sha256(package: Package) -> str:
    """The sha256 of the source entry file's bytes."""
    return hashlib.sha256((package.dir / package.entry).read_bytes()).hexdigest()


def node_staged_current(package: Package, home: Home) -> bool:
    """Whether ``<env>/package/<entry>`` exists and its marker matches.

    The marker holds the source entry's sha256; a missing marker, a staged
    file that is gone, or a source entry that cannot be read all read as
    not current, so the next pull restages.
    """
    env_dir = home.env_dir(package.name, package.version)
    try:
        marker = _marker(env_dir).read_text(encoding="utf-8").strip()
        return marker == _entry_sha256(package) and (env_dir / "package" / package.entry).is_file()
    except OSError:
        return False


def stage_node(package: Package, home: Home, env_dir: Path) -> None:
    """Copy the whole package directory into ``<env>/package``, then mark it.

    The copy replaces any previous staging: the destination is wiped first so
    a removed helper file does not survive, and the marker is written last.
    The digest is taken before anything is touched, so an unreadable source
    entry fails the pull without destroying a working staging.
    """
    digest = _entry_sha256(package)
    target = env_dir / "package"
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    shutil.copytree(package.dir, target)
    atomic_write_text(_marker(env_dir), digest, home.tmp_dir)
