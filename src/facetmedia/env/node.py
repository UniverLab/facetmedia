"""The ``node`` engine's staged entry: ``<env>/package`` and its markers.

A node package has no python environment, but it still runs its own entry
script: ``facet pull`` copies the whole package directory to
``<env>/package/``, writes ``.complete`` holding the sha256 of the source
entry file's bytes, and writes ``.package`` holding the fingerprint over
every file under the package directory (see
``facetmedia.env.package_marker``).  A later pull restages from scratch
when either marker no longer matches, so a same-version registry edit to a
helper file is picked up and an interrupted copy is redone; the staged tree
is never merged into.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Final

from facetmedia.home import Home, atomic_write_text
from facetmedia.registry_types import Node, NodePackage, Package

#: The marker a verified node extraction carries: the declared hash.
NODE_MARKER: Final[str] = ".facet-verified"
#: How long the ``node --version`` probe a verified runtime gets may take.
NODE_PROBE_TIMEOUT_S: Final[float] = 10.0


def _marker(env_dir: Path) -> Path:
    """The staging marker inside ``env_dir``, read lazily to avoid a cycle."""
    from facetmedia.env import COMPLETE  # noqa: PLC0415

    return env_dir / COMPLETE


def _entry_sha256(package: Package) -> str:
    """The sha256 of the source entry file's bytes."""
    return hashlib.sha256((package.dir / package.entry).read_bytes()).hexdigest()


def node_runtime_present(node: Node, home: Home) -> bool:
    """Whether the runtime extraction exists and its marker holds the sha256."""
    try:
        marker = (home.node_runtime_dir(node.runtime) / NODE_MARKER).read_text(encoding="utf-8")
    except OSError:
        return False
    return marker.strip() == node.runtime_sha256


def node_package_present(spec: NodePackage, home: Home) -> bool:
    """Whether the package extraction exists and its marker holds the integrity."""
    try:
        marker = (home.node_pkg_dir(spec.name, spec.version) / NODE_MARKER).read_text(
            encoding="utf-8"
        )
    except OSError:
        return False
    return marker.strip() == spec.integrity


def node_present(package: Package, home: Home) -> bool:
    """Whether the runtime and every declared package extraction verify."""
    node = package.node
    if node is None:
        return False
    return node_runtime_present(node, home) and all(
        node_package_present(spec, home) for spec in node.packages
    )


def node_problem(package: Package, home: Home) -> str | None:
    """Why the node engine is not usable, or ``None`` when it is.

    A missing or unverified extraction is pull-fixable; a verified runtime
    whose binary cannot run on this host is not — no pull changes that.
    """
    node = package.node
    if node is None:
        return None
    runtime_dir = home.node_runtime_dir(node.runtime)
    if not node_runtime_present(node, home):
        return f"{runtime_dir}: the extraction is not verified"
    for spec in node.packages:
        target = home.node_pkg_dir(spec.name, spec.version)
        if not node_package_present(spec, home):
            return f"{target}: the extraction is not verified"
    if not node_staged_current(package, home):
        staged = home.env_dir(package.name, package.version) / "package" / package.entry
        return f"{staged}: the staged runner is not current"
    if not _node_runs(home, node):
        return f"{runtime_dir / 'bin' / 'node'}: the managed node binary cannot run on this host"
    return None


def _node_runs(home: Home, node: Node) -> bool:
    """Whether ``bin/node --version`` exits zero; the linux-x64 probe."""
    binary = home.node_runtime_dir(node.runtime) / "bin" / "node"
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=NODE_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


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
    a removed helper file does not survive, and both markers are written
    last.  The digests are taken before anything is touched, so an unreadable
    source entry fails the pull without destroying a working staging.
    """
    from facetmedia.env.package_marker import PACKAGE_MARKER, package_fingerprint  # noqa: PLC0415

    digest = _entry_sha256(package)
    fingerprint = package_fingerprint(package)
    target = env_dir / "package"
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    shutil.copytree(package.dir, target)
    atomic_write_text(_marker(env_dir), digest, home.tmp_dir)
    atomic_write_text(env_dir / PACKAGE_MARKER, fingerprint, home.tmp_dir)
