"""The renewals side channel: a runner can keep a secret it refreshed.

A verb that renews an OAuth token writes the new value through the runner
SDK into a file facet created for this call.  facet stores every renewed
value the package declared after the verb ends — by success, failure or
exception — before it raises or returns anything, then deletes the file
and its directory in every case.  Nothing of the renewed value is kept on
disk outside the encrypted vault, and every surface that may carry it is
masked with the renewed values too.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path

from facetmedia import secrets
from facetmedia.contract import FacetError
from facetmedia.home import Home, log_file
from facetmedia.registry_types import Package

__all__ = [
    "RENEWALS_DIR",
    "cleanup",
    "create",
    "parse",
    "scrub",
    "store",
]

#: The directory name facet creates under the facet home's tmp area per call.
RENEWALS_DIR: str = ".facet-renewals"
_FILE_NAME: str = "renewals"


def create(home: Home, run_id: str, verb: str) -> tuple[Path, Path]:
    """An empty renewals file (0600) in its own directory (0700), outside temp."""
    directory = home.tmp_dir / f"{RENEWALS_DIR}-{run_id}-{verb}"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
        path = directory / _FILE_NAME
        path.touch()
        os.chmod(path, 0o600)
    except Exception:
        cleanup(directory)
        raise
    return directory, path


def parse(path: Path) -> dict[str, str]:
    """``{name: value}`` from ``name=value`` lines; last value wins.

    Malformed lines and names carrying a newline are ignored: a runner that
    breaks the format cannot block storing the lines it wrote correctly.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        if not name or "\n" in name or "\n" in value:
            continue
        values[name] = value
    return values


def store(home: Home, package: Package, values: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """Store declared renewed values; undeclared names are skipped, not fatal.

    Returns ``(stored, skipped)`` in name order.  A value the store refuses
    is reported as a lost renewal: the caller must delete the file and tell
    the user to run ``facet auth``.
    """
    declared = {secret.name for secret in package.secrets}
    stored: list[str] = []
    skipped: list[str] = []
    for name in sorted(values):
        if name not in declared:
            skipped.append(name)
            continue
        try:
            secrets.set_value(home, package, name, values[name])
        except FacetError as exc:
            raise _lost(exc) from exc
        except Exception as exc:
            raise FacetError(
                "machine_cannot",
                "the renewed secret was lost; run facet auth",
                hint=str(exc) if str(exc) else None,
            ) from exc
        stored.append(name)
    return stored, skipped


def _lost(error: FacetError) -> FacetError:
    """The store failure that means a renewed secret did not survive."""
    return FacetError(
        "machine_cannot",
        "the renewed secret was lost; run facet auth",
        hint=error.hint,
        details=error.details,
    )


def scrub(run_id: str, values: Mapping[str, str] | tuple[str, ...]) -> None:
    """Replace renewed values in this run's log with the fixed mask."""
    path = log_file(run_id)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    items = values.values() if isinstance(values, Mapping) else values
    scrubbed = secrets.mask(text, tuple(items))
    if scrubbed != text:
        path.write_text(scrubbed, encoding="utf-8")


def cleanup(directory: Path) -> None:
    """Remove the renewals directory and its file; never leave a leftover."""
    shutil.rmtree(directory, ignore_errors=True)
