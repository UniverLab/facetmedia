"""The shared FFmpeg: one verified static build extraction per home.

A package that encodes video declares the build it needs in ``[ffmpeg]``.
``facet pull`` downloads the declared ``.tar.xz`` archive once into
``<home>/ffmpeg/<version>/``, verifies its sha256, extracts it stripping
its single top directory, and writes a ``.facet-verified`` marker holding
the archive hash; a verified extraction is never re-downloaded.  The
binaries are ``<version>/bin/ffmpeg`` and ``<version>/bin/ffprobe``, and
the runner gets them through ``FACET_FFMPEG``, ``FACET_FFPROBE`` and
``PATH``.

Only Linux x86-64 hosts are supported in v0.1; anything else is
``machine_cannot``.
"""

from __future__ import annotations

import os
import platform
import secrets
import shutil
import sys
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Final

from facetmedia.contract import FacetError
from facetmedia.env.download import (
    _download_with_retries,
    _part_path,
    _stream_to_part,
)
from facetmedia.home import Home, atomic_write_text
from facetmedia.registry_types import FFmpeg

#: The marker a verified FFmpeg extraction carries: the archive's sha256.
FFMPEG_MARKER: Final[str] = ".facet-verified"


def ffmpeg_dir(ffmpeg: FFmpeg, home: Home) -> Path:
    """``<home>/ffmpeg/<version>``: one extraction per declared version."""
    return home.ffmpeg_dir / ffmpeg.version


def ffmpeg_bin_dir(ffmpeg: FFmpeg, home: Home) -> Path:
    """The ``bin/`` directory holding the verified ``ffmpeg`` and ``ffprobe``."""
    return ffmpeg_dir(ffmpeg, home) / "bin"


def ffmpeg_executable(ffmpeg: FFmpeg, home: Home) -> Path:
    """The verified ``ffmpeg`` binary."""
    return ffmpeg_bin_dir(ffmpeg, home) / "ffmpeg"


def ffprobe_executable(ffmpeg: FFmpeg, home: Home) -> Path:
    """The verified ``ffprobe`` binary."""
    return ffmpeg_bin_dir(ffmpeg, home) / "ffprobe"


def ffmpeg_present(ffmpeg: FFmpeg, home: Home) -> bool:
    """Whether the extraction is verified and both binaries exist."""
    try:
        marker = (ffmpeg_dir(ffmpeg, home) / FFMPEG_MARKER).read_text(encoding="utf-8")
    except OSError:
        return False
    return (
        marker.strip() == ffmpeg.sha256
        and ffmpeg_executable(ffmpeg, home).is_file()
        and ffprobe_executable(ffmpeg, home).is_file()
    )


def require_host() -> None:
    """Refuse any host other than Linux x86-64 with ``machine_cannot``."""
    machine = platform.machine()
    if sys.platform != "linux" or machine not in ("x86_64", "amd64"):
        raise FacetError(
            "machine_cannot",
            "ffmpeg builds are only available for linux x86-64; "
            f"this host is {sys.platform}/{machine}",
        )


def fetch_ffmpeg(
    ffmpeg: FFmpeg,
    home: Home,
    on_bytes: Callable[[int], None] | None = None,
    on_total: Callable[[int], None] | None = None,
) -> int:
    """Download, verify and extract the declared FFmpeg build, once per home.

    The archive holds one top directory with ``bin/ffmpeg`` and
    ``bin/ffprobe``; the extraction strips that directory so the binaries
    land at ``<home>/ffmpeg/<version>/bin/``. Returns the bytes downloaded:
    0 when a verified extraction is present.
    """
    if ffmpeg_present(ffmpeg, home):
        return 0
    part = _part_path(home, f"ffmpeg-{ffmpeg.version}")
    digest, size = _download_with_retries(
        part, lambda: _stream_to_part(ffmpeg.url, part, on_bytes, on_total)
    )
    if digest != ffmpeg.sha256:
        part.unlink(missing_ok=True)
        raise FacetError(
            "download_failed",
            f"{ffmpeg.version}: sha256 mismatch: expected {ffmpeg.sha256}, got {digest}",
            details={
                "ffmpeg": ffmpeg.version,
                "expected": ffmpeg.sha256,
                "actual": digest,
            },
        )
    staging = home.tmp_dir / f"ffmpeg-{ffmpeg.version}.{secrets.token_hex(4)}"
    try:
        _extract_ffmpeg(ffmpeg, part, staging)
    finally:
        part.unlink(missing_ok=True)
    target = ffmpeg_dir(ffmpeg, home)
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    os.replace(staging, target)
    atomic_write_text(target / FFMPEG_MARKER, ffmpeg.sha256, home.tmp_dir)
    return size


def _extract_ffmpeg(ffmpeg: FFmpeg, archive: Path, staging: Path) -> None:
    """Unpack the verified archive into ``staging``; a failure leaves nothing."""
    unpacked = staging.parent / f"{staging.name}.{secrets.token_hex(4)}.unpacked"
    unpacked.mkdir(parents=True, exist_ok=False)
    try:
        with tarfile.open(archive, "r:xz") as bundle:
            bundle.extractall(unpacked, filter="data")
        tops = sorted(unpacked.iterdir())
        if len(tops) != 1 or not tops[0].is_dir():
            raise FacetError(
                "download_failed",
                f"{ffmpeg.version}: the archive has no single top directory",
                details={"ffmpeg": ffmpeg.version},
            )
        top = tops[0]
        if not (top / "bin" / "ffmpeg").is_file() or not (top / "bin" / "ffprobe").is_file():
            raise FacetError(
                "download_failed",
                f"{ffmpeg.version}: the archive is missing bin/ffmpeg or bin/ffprobe",
                details={"ffmpeg": ffmpeg.version},
            )
        os.replace(top, staging)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(unpacked, ignore_errors=True)
