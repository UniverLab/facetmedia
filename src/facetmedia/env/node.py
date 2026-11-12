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

import base64
import hashlib
import os
import secrets
import shutil
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Final

from facetmedia.contract import FacetError
from facetmedia.env.download import (
    CHUNK_BYTES,
    _content_length,
    _download_with_retries,
    _opened,
    _part_path,
    _stream_to_part,
)
from facetmedia.home import Home, atomic_write_text
from facetmedia.registry_types import Node, NodePackage, Package

#: The marker a verified node extraction carries: the declared hash.
NODE_MARKER: Final[str] = ".facet-verified"
#: How long the ``node --version`` probe a verified runtime gets may take.
NODE_PROBE_TIMEOUT_S: Final[float] = 10.0
#: The official Node.js distribution root the ``node`` engine downloads from.
NODE_DIST: Final[str] = "https://nodejs.org/dist"


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


# --- The downloads: one runtime tarball and one tarball per npm package.


def _fetch_runtime(
    node: Node,
    home: Home,
    on_bytes: Callable[[int], None] | None = None,
    on_total: Callable[[int], None] | None = None,
) -> int:
    """The official linux-x64 tarball, sha256-verified, extracted once."""
    if node_runtime_present(node, home):
        return 0
    url = f"{NODE_DIST}/v{node.runtime}/node-v{node.runtime}-linux-x64.tar.gz"
    part = _part_path(home, f"node-{node.runtime}")
    digest, size = _download_with_retries(
        part, lambda: _stream_to_part(url, part, on_bytes, on_total)
    )
    if digest != node.runtime_sha256:
        part.unlink(missing_ok=True)
        raise FacetError(
            "download_failed",
            f"node {node.runtime}: sha256 mismatch: expected {node.runtime_sha256}, got {digest}",
            details={
                "runtime": node.runtime,
                "expected": node.runtime_sha256,
                "actual": digest,
            },
        )
    target = home.node_runtime_dir(node.runtime)
    _install_tarball(part, target, node.runtime_sha256, home, f"node-{node.runtime}")
    return size


def _fetch_package(
    spec: NodePackage,
    home: Home,
    on_bytes: Callable[[int], None] | None = None,
    on_total: Callable[[int], None] | None = None,
) -> int:
    """One npm tarball, integrity-verified, extracted once per name+version."""
    algo, _sep, expected = spec.integrity.partition("-")
    # The part and staging paths are flat files: a scoped name's "/" cannot
    # appear in them, so it is folded to "-" (the extraction path keeps it).
    label = f"{spec.name.replace('/', '-')}-{spec.version}"
    part = _part_path(home, label)
    digest, size = _download_with_retries(
        part, lambda: _stream_with_digest(part, spec.tarball, algo, on_bytes, on_total)
    )
    if digest != expected:
        part.unlink(missing_ok=True)
        raise FacetError(
            "download_failed",
            f"{spec.name}@{spec.version}: integrity mismatch: "
            f"expected {spec.integrity}, got {algo}-{digest}",
            details={
                "package": spec.name,
                "version": spec.version,
                "expected": spec.integrity,
                "actual": f"{algo}-{digest}",
            },
        )
    target = home.node_pkg_dir(spec.name, spec.version)
    _install_tarball(part, target, spec.integrity, home, label)
    return size


def _stream_with_digest(
    part: Path,
    url: str,
    algo: str,
    on_bytes: Callable[[int], None] | None = None,
    on_total: Callable[[int], None] | None = None,
) -> tuple[str, int]:
    """Stream ``url`` into ``part`` while hashing with ``algo``.

    Returns the digest base64-encoded — the spelling npm's ``integrity``
    field uses — plus the byte count.
    """
    digest = hashlib.new(algo)
    size = 0
    with _opened(url) as source, part.open("wb") as sink:
        total = _content_length(source)
        if on_total is not None and total is not None:
            on_total(total)
        while chunk := source.read(CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
            sink.write(chunk)
            if on_bytes is not None:
                on_bytes(len(chunk))
    return base64.b64encode(digest.digest()).decode("ascii"), size


def _install_tarball(archive: Path, target: Path, marker: str, home: Home, label: str) -> None:
    """Extract a verified tarball into ``target``; the marker lands last.

    The archive's single top-level directory becomes the extraction root —
    ``node-v<ver>-linux-x64/`` for the runtime, ``package/`` for npm — so
    ``<target>/bin/node`` and ``<target>/<entry>`` resolve as documented.
    """
    staging = home.tmp_dir / f"{label}.{secrets.token_hex(4)}"
    try:
        payload = _extract_tarball(archive, staging)
    finally:
        archive.unlink(missing_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    if payload == staging:
        os.replace(staging, target)
    else:
        os.replace(payload, target)
        shutil.rmtree(staging, ignore_errors=True)
    atomic_write_text(target / NODE_MARKER, marker, home.tmp_dir)


def _extract_tarball(archive: Path, staging: Path) -> Path:
    """Unpack ``archive`` into ``staging``; returns the payload directory."""
    staging.mkdir(parents=True, exist_ok=False)
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            if hasattr(tarfile, "data_filter"):
                bundle.extractall(staging, filter="data")
            else:
                bundle.extractall(staging)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    entries = list(staging.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return staging
