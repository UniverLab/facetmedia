"""Read and validate a package's ``[[secrets]]`` declarations.

``facetmedia.manifest`` keeps the package grammar; the secret grammar lives
here so the manifest module stays inside the lab's per-file size cap, the
same split ``facetmedia.manifest.model`` makes for ``[[models]]``.  The
loader passed in is :class:`facetmedia.manifest.RegistryLoader`, which
collects every problem instead of stopping at the first one.

A declaration names a credential, never a value: the description is what
``facet secret list`` prints, and ``required`` is what makes a run refuse
before the runner starts when the value is not stored.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from facetmedia.registry_types import Secret

if TYPE_CHECKING:
    from facetmedia.manifest import RegistryLoader

_SECRET_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9-]*\Z")
_SECRET_KEYS: Final[frozenset[str]] = frozenset(
    {"name", "description", "required", "obtain", "link", "produced_by"}
)
#: The only ``produced_by`` value: the package's ``auth`` verb mints it.
PRODUCED_BY_AUTH: Final[str] = "auth"
#: The longest ``obtain`` hint a person can read at a glance.
OBTAIN_LIMIT: Final[int] = 300
#: The longest ``link`` address a terminal can make clickable.
LINK_LIMIT: Final[int] = 200


def read_secrets(loader: RegistryLoader, raw: object, file: str) -> tuple[Secret, ...]:
    """Every ``[[secrets]]`` entry of one package manifest, in declared order."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        loader.add(file, "secrets", "type")
        return ()
    secrets: list[Secret] = []
    seen: set[str] = set()
    for position, item in enumerate(raw):
        prefix = f"secrets[{position}]."
        if not isinstance(item, dict):
            loader.add(file, f"{prefix}name", "type")
            continue
        secret = _read_secret(loader, item, file, prefix, seen)
        if secret is not None:
            secrets.append(secret)
    return tuple(secrets)


def _read_secret(
    loader: RegistryLoader, item: Mapping[str, Any], file: str, prefix: str, seen: set[str]
) -> Secret | None:
    before = len(loader.problems)
    loader.unknown(item, _SECRET_KEYS, file, prefix)
    name = loader.string(item, "name", file, prefix, required=True)
    if name is not None:
        if not _SECRET_NAME_RE.fullmatch(name):
            loader.add(file, f"{prefix}name", "value")
        elif name in seen:
            loader.add(file, f"{prefix}name", "duplicate")
        else:
            seen.add(name)
    description = loader.string(item, "description", file, prefix, required=True)
    required = loader.boolean(item, "required", file, prefix, default=False)
    obtain = _read_obtain(loader, item, file, prefix)
    link = _read_link(loader, item, file, prefix)
    produced_by = _read_produced_by(loader, item, file, prefix)
    if len(loader.problems) != before or name is None or description is None:
        return None
    return Secret(
        name=name,
        description=description,
        required=required,
        obtain=obtain,
        link=link,
        produced_by=produced_by,
    )


def _read_obtain(
    loader: RegistryLoader, item: Mapping[str, Any], file: str, prefix: str
) -> str | None:
    """Where the person gets the value: absent, or one short line."""
    if "obtain" not in item:
        return None
    value = item["obtain"]
    if (
        not isinstance(value, str)
        or not value
        or len(value) > OBTAIN_LIMIT
        or "\n" in value
        or "\r" in value
    ):
        loader.add(file, f"{prefix}obtain", "value")
        return None
    return value


def _read_link(
    loader: RegistryLoader, item: Mapping[str, Any], file: str, prefix: str
) -> str | None:
    """The exact page to open for the value: absent, or one short ``https`` address."""
    if "link" not in item:
        return None
    value = item["link"]
    if (
        not isinstance(value, str)
        or not value.startswith("https://")
        or len(value) > LINK_LIMIT
        or any(char.isspace() for char in value)
    ):
        loader.add(file, f"{prefix}link", "value")
        return None
    return value


def _read_produced_by(
    loader: RegistryLoader, item: Mapping[str, Any], file: str, prefix: str
) -> str | None:
    """What mints the value: absent, or exactly ``"auth"``."""
    if "produced_by" not in item:
        return None
    if item["produced_by"] != PRODUCED_BY_AUTH:
        loader.add(file, f"{prefix}produced_by", "value")
        return None
    return PRODUCED_BY_AUTH
