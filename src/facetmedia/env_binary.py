"""The ``binary`` engine: one verified zip extraction per home.

A binary package is a single static executable — no python environment, no
server, no host install.  ``facet pull`` downloads the declared zip once
into ``<home>/bin/<name>/<version>/``, verifies its sha256, extracts it,
sets the executable bit on ``bin`` and writes a ``.facet-verified`` marker
holding the archive hash; a verified extraction is never re-downloaded.

``facetmedia.env`` dispatches here for ``package.binary`` packages; the
runner re-probes ``<bin> <version_arg>`` before every call, so a missing
extraction or a wrong version is ``machine_cannot``, never a silent run of
the wrong binary.
"""

from __future__ import annotations

import os
import secrets
import shlex
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Final

from facetmedia.contract import FacetError
from facetmedia.env_download import (
    _download_with_retries,
    _part_path,
    _stream_to_part,
)
from facetmedia.home import Home, atomic_write_text
from facetmedia.registry_types import Binary, Package

#: The marker a verified binary extraction carries: the archive's sha256.
BINARY_MARKER: Final[str] = ".facet-verified"
#: How long the ``<bin> <version_arg>`` probe may take.
BIN_PROBE_TIMEOUT_S: Final[float] = 10.0


def binary_dir(binary: Binary, home: Home) -> Path:
    """``<home>/bin/<name>/<version>``, the extraction root."""
    return home.bin_dir / binary.name / binary.version


def binary_path(binary: Binary, home: Home) -> Path:
    """The resolved executable: ``<home>/bin/<name>/<version>/<bin>``."""
    return binary_dir(binary, home) / binary.bin


def binary_present(binary: Binary, home: Home) -> bool:
    """Whether the extraction exists and its marker holds the declared sha256."""
    try:
        marker = (binary_dir(binary, home) / BINARY_MARKER).read_text(encoding="utf-8")
    except OSError:
        return False
    return marker.strip() == binary.sha256


def engine_present(package: Package, home: Home) -> bool | None:
    """The binary engine's presence, or ``None`` for every other engine."""
    if package.binary is None:
        return None
    return binary_present(package.binary, home)


def binary_problem(package: Package, home: Home) -> str | None:
    """Why the binary engine is not usable, or ``None`` when it is.

    A missing or unverified extraction is pull-fixable; a verified binary
    that cannot run or whose ``version_arg`` output misses the declared
    version is not — no pull changes that.
    """
    binary = package.binary
    if binary is None:
        return None
    target = binary_dir(binary, home)
    if not binary_present(binary, home):
        return f"{target}: the extraction is not verified"
    resolved = binary_path(binary, home)
    return _version_problem(binary, resolved)


def _version_problem(binary: Binary, resolved: Path) -> str | None:
    """Run ``<bin> <version_arg>``; the declared version must appear in it."""
    argv = [str(resolved), *shlex.split(binary.version_arg)]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=BIN_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return f"{resolved}: the managed binary cannot run on this host"
    if result.returncode != 0:
        return f"{resolved}: the managed binary cannot run on this host"
    output = (result.stdout + result.stderr).strip()
    if binary.version not in output:
        return (
            f"{resolved}: {binary.version_arg} output {output!r} does not contain {binary.version}"
        )
    return None


def fetch_binary(binary: Binary, home: Home) -> int:
    """Download, verify and extract the declared zip; returns the bytes fetched."""
    if binary_present(binary, home):
        return 0
    label = f"{binary.name}-{binary.version}"
    part = _part_path(home, label)
    digest, size = _download_with_retries(part, lambda: _stream_to_part(binary.url, part))
    if digest != binary.sha256:
        part.unlink(missing_ok=True)
        raise FacetError(
            "download_failed",
            f"{label}: sha256 mismatch: expected {binary.sha256}, got {digest}",
            details={
                "binary": label,
                "expected": binary.sha256,
                "actual": digest,
            },
        )
    target = binary_dir(binary, home)
    staging = home.tmp_dir / f"{label}.{secrets.token_hex(4)}"
    try:
        _extract_zip(part, staging, binary, label)
    finally:
        part.unlink(missing_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    os.replace(staging, target)
    resolved = target / binary.bin
    os.chmod(resolved, 0o755)
    atomic_write_text(target / BINARY_MARKER, binary.sha256, home.tmp_dir)
    return size


def _extract_zip(archive: Path, staging: Path, binary: Binary, label: str) -> None:
    """Unpack the verified zip into ``staging``; a failure leaves nothing."""
    staging.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(staging)
    except zipfile.BadZipFile as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise FacetError("download_failed", f"{label}: not a zip archive: {exc}") from exc
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if not (staging / binary.bin).exists():
        shutil.rmtree(staging, ignore_errors=True)
        raise FacetError(
            "download_failed",
            f"{label}: the archive has no {binary.bin}",
            details={"binary": label, "bin": binary.bin},
        )


def pull_report(
    package: Package,
    home: Home,
    env_dir: Path,
    started: float,
    verify_models: bool,
) -> dict[str, object]:
    """The ``binary`` engine's pull: verified extraction, no python environment."""
    from facetmedia import env as env_module  # noqa: PLC0415

    binary = package.binary
    assert binary is not None
    up_to_date = binary_present(binary, home)
    downloaded = fetch_binary(binary, home)
    records, model_bytes = env_module._fetch_models(package, home, verify_models)
    downloaded += model_bytes
    return {
        "package": package.name,
        "version": package.version,
        "env": str(env_dir),
        "python": package.python,
        "already_installed": up_to_date,
        "downloaded_bytes": downloaded,
        "seconds": round(time.monotonic() - started, 3),
        "models": records,
    }


def plan_lines(package: Package, home: Home) -> list[str]:
    """The pull plan's binary lines, before the model lines ``env`` adds."""
    binary = package.binary
    assert binary is not None
    state = "" if binary_present(binary, home) else " to download"
    return [f"binary {binary.name} {binary.version}{state}"]
