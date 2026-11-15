"""Which version of each package facet installed, kept in ``state/installed.json``.

A package's environment directory names a version, but the ``host-app`` and
``binary`` engines have no such directory.  A successful pull therefore writes
one entry per package, whatever its engine, so the installed version survives
both a registry update and an engine without an environment.

Entries are written through the same atomic rename as every other state file.
Nothing here touches the network or the registry snapshot.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from facetmedia.home import Home, atomic_write_json, read_json

__all__ = [
    "STATE_FILE",
    "entries",
    "forget",
    "record",
    "version_of",
]

STATE_FILE: Final[str] = "installed.json"
REGISTRY_STATE: Final[str] = "registry.json"


def entries(home: Home) -> dict[str, dict[str, object]]:
    """The installed-version entries, keyed by package name.

    A file that is not a table, or an entry that is not one, is ignored: the
    callers fall back to the snapshot's own answer.
    """
    state: object = read_json(home.state_file(STATE_FILE), {})
    if not isinstance(state, dict):
        return {}
    found: dict[str, dict[str, object]] = {}
    for name, entry in state.items():
        if isinstance(name, str) and isinstance(entry, dict):
            found[name] = entry
    return found


def version_of(home: Home, name: str) -> str | None:
    """The version ``state/installed.json`` records for ``name``, or ``None``."""
    version = entries(home).get(name, {}).get("version")
    return version if isinstance(version, str) and version else None


def record(home: Home, name: str, version: str) -> None:
    """Write ``name``'s entry as installed at ``version``, atomically."""
    state = entries(home)
    state[name] = {
        "version": version,
        "registry_commit": _registry_commit(home),
        "installed_at": _now(),
    }
    atomic_write_json(home.state_file(STATE_FILE), state, home.tmp_dir)


def forget(home: Home, name: str) -> None:
    """Drop ``name``'s entry; a package with no entry is left untouched."""
    state = entries(home)
    if name not in state:
        return
    del state[name]
    atomic_write_json(home.state_file(STATE_FILE), state, home.tmp_dir)


def _registry_commit(home: Home) -> str:
    """The snapshot commit ``facet update`` pinned, or ``""`` without one."""
    state: object = read_json(home.state_file(REGISTRY_STATE), {})
    if not isinstance(state, dict):
        return ""
    commit = state.get("commit")
    return commit if isinstance(commit, str) else ""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
