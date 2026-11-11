"""The shared browser: one verified Chrome for Testing extraction per home.

A package that renders declares the build it needs in ``[browser]``.
``facet pull`` downloads the declared Chrome for Testing archive once into
``<home>/browsers/chromium-<revision>/``, verifies its sha256, extracts it,
and writes a ``.facet-verified`` marker holding the archive hash; a verified
extraction is never re-downloaded.  The executable is
``chromium-<revision>/chrome-linux64/chrome``, Playwright's own layout, so
the package's own ``playwright`` finds it through
``PLAYWRIGHT_BROWSERS_PATH`` without downloading anything itself.

Only Linux x86-64 hosts are supported in v0.1; anything else is
``machine_cannot``.
"""

from __future__ import annotations

import os
import platform
import secrets
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Final

from facetmedia.contract import FacetError
from facetmedia.env.download import (
    _download_with_retries,
    _part_path,
    _stream_to_part,
)
from facetmedia.home import Home, atomic_write_text
from facetmedia.registry_types import Browser

#: The official Chrome for Testing CDN the ``[browser]`` archives come from.
PLAYWRIGHT_CDN: Final[str] = "https://cdn.playwright.dev"
#: The marker a verified browser extraction carries: the archive's sha256.
BROWSER_MARKER: Final[str] = ".facet-verified"


def browser_dir(browser: Browser, home: Home) -> Path:
    """``<home>/browsers/<channel>-<revision>``, Playwright's own layout."""
    return home.browsers_dir / f"{browser.channel}-{browser.revision}"


def browser_executable(browser: Browser, home: Home) -> Path:
    """The verified executable: ``chromium-<revision>/chrome-linux64/chrome``."""
    return browser_dir(browser, home) / "chrome-linux64" / "chrome"


def browser_present(browser: Browser, home: Home) -> bool:
    """Whether the extraction is verified and its executable exists."""
    try:
        marker = (browser_dir(browser, home) / BROWSER_MARKER).read_text(encoding="utf-8")
    except OSError:
        return False
    return marker.strip() == browser.sha256 and browser_executable(browser, home).is_file()


def require_host() -> None:
    """Refuse any host other than Linux x86-64 with ``machine_cannot``."""
    machine = platform.machine()
    if sys.platform != "linux" or machine not in ("x86_64", "amd64"):
        raise FacetError(
            "machine_cannot",
            f"browser builds are only available for linux x86-64; "
            f"this host is {sys.platform}/{machine}",
        )


def fetch_browser(browser: Browser, home: Home) -> None:
    """Download, verify and extract the declared browser, once per home.

    The archive lands at Playwright's ``<name>-<revision>`` layout under
    ``<home>/browsers``, so the package's own ``playwright`` finds it through
    ``PLAYWRIGHT_BROWSERS_PATH`` without downloading anything itself.
    """
    if browser_present(browser, home):
        return
    url = f"{PLAYWRIGHT_CDN}/builds/cft/{browser.version}/linux64/chrome-linux64.zip"
    part = _part_path(home, f"{browser.channel}-{browser.revision}")
    digest, _size = _download_with_retries(part, lambda: _stream_to_part(url, part))
    if digest != browser.sha256:
        part.unlink(missing_ok=True)
        raise FacetError(
            "download_failed",
            f"{browser.channel}-{browser.revision}: sha256 mismatch: "
            f"expected {browser.sha256}, got {digest}",
            details={
                "browser": f"{browser.channel}-{browser.revision}",
                "expected": browser.sha256,
                "actual": digest,
            },
        )
    staging = home.tmp_dir / f"{browser.channel}-{browser.revision}.{secrets.token_hex(4)}"
    try:
        _extract_browser(part, staging)
    finally:
        part.unlink(missing_ok=True)
    target = browser_dir(browser, home)
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    os.replace(staging, target)
    atomic_write_text(target / BROWSER_MARKER, browser.sha256, home.tmp_dir)


def _extract_browser(archive: Path, staging: Path) -> None:
    """Unpack the verified archive into ``staging``; a failure leaves nothing."""
    staging.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(staging)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
