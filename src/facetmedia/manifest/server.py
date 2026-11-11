"""Read and validate a package's ``[engine.server]`` table.

``facetmedia.manifest`` keeps the package grammar; the server grammar lives
here so the manifest module stays inside the lab's per-file size cap.  The
loader passed in is :class:`facetmedia.manifest.RegistryLoader`, which collects
every problem instead of stopping at the first one.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

from facetmedia.registry_types import Server, Source

if TYPE_CHECKING:
    from facetmedia.manifest import RegistryLoader

SERVER_KEYS: Final[frozenset[str]] = frozenset(
    {"repo", "rev", "requirements", "launch", "health_url", "timeout_s", "env"}
)
SOURCE_KEYS: Final[frozenset[str]] = frozenset({"repo", "rev"})
#: The manifest's port token, substituted with the free port facet picked.
PORT_TOKEN: Final[str] = "{{port}}"
#: The manifest's data token, substituted with the package's persistent dir.
DATA_TOKEN: Final[str] = "{{data}}"
_ENV_KEY_RE: Final[re.Pattern[str]] = re.compile(r"[A-Z_][A-Z0-9_]*\Z")
_ENV_RESERVED: Final[frozenset[str]] = frozenset({"PATH", "PORT"})
_ENV_TOKENS: Final[tuple[str, ...]] = (DATA_TOKEN, PORT_TOKEN)
_ENV_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\{\{[^}]*\}\}")
DEFAULT_TIMEOUT_S: Final[int] = 120
_REVISION_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{40}\Z")


def _relative(value: str) -> bool:
    """True for a non-empty relative path that stays inside the checkout."""
    path = Path(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts


def _repo_ok(value: str) -> bool:
    """True for an https repo URL, the only scheme facet clones."""
    return value.startswith("https://")


def _rev_ok(value: str) -> bool:
    """True for a 40-character lowercase hex commit."""
    return bool(_REVISION_RE.fullmatch(value))


_CHECKS: Final[Mapping[str, Callable[[str], bool]]] = {
    "repo": _repo_ok,
    "rev": _rev_ok,
    "requirements": _relative,
    "health_url": lambda value: bool(urlsplit(value).scheme),
}


def read_server(
    loader: RegistryLoader,
    engine_table: Mapping[str, Any],
    file: str,
    engine_type: str,
    transport_type: str,
) -> Server | None:
    """The optional ``[engine.server]`` table, or ``None`` when it is absent."""
    if engine_table.get("server") is None:
        return None
    before = len(loader.problems)
    table = loader.table(engine_table, "server", file, "engine.", required=False)
    if table is None:
        return None
    if (engine_type, transport_type) != ("python-env", "cli"):
        loader.add(file, "engine.server", "requires python-env with transport cli")
    loader.unknown(table, SERVER_KEYS, file, "engine.server.")
    values = {
        key: loader.string(table, key, file, "engine.server.", required=True) or ""
        for key in _CHECKS
    }
    for key, check in _CHECKS.items():
        if values[key] and not check(values[key]):
            loader.add(file, f"engine.server.{key}", "value")
    launch = _launch(loader, table, file)
    timeout_s = loader.integer(table, "timeout_s", file, "engine.server.", required=False)
    if timeout_s is not None and timeout_s <= 0:
        loader.add(file, "engine.server.timeout_s", "value")
    env = read_env(loader, table, file)
    if len(loader.problems) != before:
        return None
    return Server(
        repo=values["repo"],
        rev=values["rev"],
        requirements=values["requirements"],
        launch=launch,
        health_url=values["health_url"],
        timeout_s=timeout_s or DEFAULT_TIMEOUT_S,
        env=env,
    )


def read_source(
    loader: RegistryLoader,
    engine_table: Mapping[str, Any],
    file: str,
    engine_type: str,
    transport_type: str,
) -> Source | None:
    """The optional ``[engine.source]`` table, or ``None`` when it is absent."""
    if engine_table.get("source") is None:
        return None
    before = len(loader.problems)
    table = loader.table(engine_table, "source", file, "engine.", required=False)
    if table is None:
        return None
    if engine_table.get("server") is not None:
        loader.add(file, "engine.source", "a package declares either a server or a source checkout")
    if (engine_type, transport_type) != ("python-env", "cli"):
        loader.add(file, "engine.source", "requires python-env with transport cli")
    loader.unknown(table, SOURCE_KEYS, file, "engine.source.")
    repo = loader.string(table, "repo", file, "engine.source.", required=True) or ""
    rev = loader.string(table, "rev", file, "engine.source.", required=True) or ""
    if repo and not _repo_ok(repo):
        loader.add(file, "engine.source.repo", "value")
    if rev and not _rev_ok(rev):
        loader.add(file, "engine.source.rev", "value")
    if len(loader.problems) != before:
        return None
    return Source(repo=repo, rev=rev)


def read_env(
    loader: RegistryLoader, table: Mapping[str, Any], file: str
) -> tuple[tuple[str, str], ...]:
    """The optional ``env`` table: sorted ``(key, value)`` pairs, or ``()``."""
    raw = loader.table(table, "env", file, "engine.server.", required=False)
    if raw is None:
        return ()
    found: list[tuple[str, str]] = []
    for key in sorted(raw):
        item = f"engine.server.env.{key}"
        if not _ENV_KEY_RE.fullmatch(key) or key in _ENV_RESERVED or key.startswith("FACET_"):
            loader.add(file, item, "name")
            continue
        value = raw[key]
        if not isinstance(value, str):
            loader.add(file, item, "value")
            continue
        if any(token not in _ENV_TOKENS for token in _ENV_TOKEN_RE.findall(value)):
            loader.add(file, item, "token")
            continue
        found.append((key, value))
    return tuple(found)


def _launch(loader: RegistryLoader, table: Mapping[str, Any], file: str) -> tuple[str, ...]:
    """The launch argv: a non-empty list whose port token facet substitutes."""
    raw = table.get("launch")
    argv = loader.string_list(table, "launch", file, "engine.server.")
    if raw is None:
        loader.add(file, "engine.server.launch", "required")
    elif isinstance(raw, list) and not argv:
        loader.add(file, "engine.server.launch", "value")
    elif argv and not any(PORT_TOKEN in token for token in argv):
        loader.add(file, "engine.server.launch", f"value: needs {PORT_TOKEN}")
    return argv
