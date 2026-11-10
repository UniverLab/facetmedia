"""The whole-package fingerprint and its ``.package`` marker.

``.complete`` says the environment was built from this lock hash; it says
nothing about the rest of the package directory. A registry fix that keeps
the version, the lock and (for node) the entry file never reaches an
installed package through those markers, so the pull also records a
fingerprint over every file under the package directory in ``.package``.
A missing or differing ``.package`` reads as stale once, and the next pull
rebuilds or restages the environment from scratch.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Final

from facetmedia.registry_types import Package

if TYPE_CHECKING:
    from facetmedia.home import Home

COMPLETE: Final[str] = ".complete"
PACKAGE_MARKER: Final[str] = ".package"


def complete_matches(env_dir: Path, lock_sha256: str) -> bool:
    """Whether ``.complete`` holds this package's lock hash."""
    try:
        return (env_dir / COMPLETE).read_text(encoding="utf-8").strip() == lock_sha256
    except OSError:
        return False


def package_fingerprint(package: Package) -> str:
    """A sha256 over every file under the package directory.

    The digest covers the sorted POSIX relative paths interleaved with the
    file bytes, so a renamed, added, removed or edited file changes it.
    ``__pycache__/`` directories, ``*.pyc`` files and symlinks are skipped:
    they are build residue, never registry content.
    """
    digest = hashlib.sha256()
    for path in _sorted_files(package.dir):
        digest.update(path.relative_to(package.dir).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _sorted_files(root: Path) -> list[Path]:
    """Every hashable file under ``root``, ordered by POSIX relative path."""
    found = [path for path in root.rglob("*") if _hashable(path, root)]
    found.sort(key=lambda path: path.relative_to(root).as_posix())
    return found


def _hashable(path: Path, root: Path) -> bool:
    """Whether ``path`` carries registry content worth fingerprinting."""
    if path.is_symlink() or not path.is_file():
        return False
    if "__pycache__" in path.relative_to(root).parts:
        return False
    return path.suffix != ".pyc"


def package_matches(package: Package, env_dir: Path) -> bool:
    """Whether the environment's ``.package`` marker holds this fingerprint."""
    try:
        stored = (env_dir / PACKAGE_MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return stored == package_fingerprint(package)


def package_stale(package: Package, home: Home) -> bool:
    """Whether an installed package's files changed since it was installed.

    Only a ``python-env`` or ``node`` package with an environment that
    passes today's install checks can be stale; a missing ``.package``
    marker — every environment from before this change — reads as stale
    once. Host-app and binary packages stage no package copy, and a package
    that is not installed is never stale.
    """
    if package.host_app is not None or package.binary is not None:
        return False
    from facetmedia import env as env_module  # noqa: PLC0415

    if not env_module.is_installed(package, home):
        return False
    return not package_matches(package, home.env_dir(package.name, package.version))
