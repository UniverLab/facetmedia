"""The credential store: ``<home>/secrets/<package>.json``.

A package declares the credentials it uses in its manifest (``[[secrets]]``);
a person stores each value with ``facet secret set`` or produces it with
``facet auth``, and facet keeps it here — one JSON object per package, written
atomically with mode ``0600``.  The location matters: it is outside every
environment, piece and record, so ``facet remove`` and ``facet clean`` never
touch it — only ``facet secret rm`` does — and WSL2 or plain Linux needs no
keyring.

A value leaves the store in exactly two directions: the owning package's
runner environment, and the mask applied to that runner's output.  Nothing in
this module returns a value to a caller that prints it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Final

from facetmedia.contract import FacetError
from facetmedia.home import Home, atomic_write_json, read_json
from facetmedia.registry_types import Package, Secret

__all__ = [
    "AUTH_VERB_RE",
    "MASK",
    "STORE_DIR",
    "auth_hint",
    "env_for",
    "env_name",
    "has_auth_verb",
    "mask",
    "mask_values",
    "missing_required",
    "remove",
    "require_ready",
    "rows",
    "set_value",
    "store_file",
]

#: The directory under the facet home that holds one file per package.
STORE_DIR: Final[str] = "secrets"
#: The exact text every stored value is replaced with before output is kept.
MASK: Final[str] = "***"
#: The runner environment variable prefix; the name follows in upper case.
PREFIX: Final[str] = "FACET_SECRET_"
#: The runner verb a package may provide to produce secrets interactively.
AUTH_VERB_RE: Final[re.Pattern[str]] = re.compile(r"""@runner\.verb\(\s*['"]auth['"]\s*\)""")
_STORE_MODE: Final[int] = 0o600


def store_file(home: Home, package_name: str) -> Path:
    """``<home>/secrets/<package>.json``: the file ``facet secret`` owns.

    Outside ``envs/``, ``servers/``, ``models/``, ``tmp/`` and every piece and
    record, so neither ``facet remove`` nor ``facet clean`` can reach it.
    """
    return home.root / STORE_DIR / f"{package_name}.json"


def env_name(secret_name: str) -> str:
    """The runner's variable for a declared secret: ``FACET_SECRET_<UPPER>``."""
    return f"{PREFIX}{secret_name.upper().replace('-', '_')}"


def _read(home: Home, package_name: str) -> dict[str, str]:
    """The stored name/value pairs of one package; ``{}`` when it has none."""
    stored: object = read_json(store_file(home, package_name), {})
    if not isinstance(stored, dict):
        raise FacetError("internal", f"{store_file(home, package_name)}: corrupt secret store")
    return {str(name): value for name, value in stored.items() if isinstance(value, str) and value}


def _write(home: Home, package_name: str, values: dict[str, str]) -> Path:
    """Write the store atomically, then narrow it to the owner's read and write."""
    path = store_file(home, package_name)
    if not values:
        path.unlink(missing_ok=True)
        return path
    atomic_write_json(path, values, home.tmp_dir)
    os.chmod(path, _STORE_MODE)
    return path


def _declared(package: Package, name: str) -> Secret:
    for secret in package.secrets:
        if secret.name == name:
            return secret
    raise FacetError(
        "invalid_input",
        f"{package.name} declares no secret {name}",
        details={"package": package.name, "declared": [secret.name for secret in package.secrets]},
    )


def set_value(home: Home, package: Package, name: str, value: str) -> dict[str, object]:
    """Store one declared secret; an undeclared name or an empty value is refused."""
    _declared(package, name)
    if not value:
        raise FacetError("invalid_input", f"{package.name} {name}: the value must not be empty")
    values = _read(home, package.name)
    values[name] = value
    path = _write(home, package.name, values)
    return {"package": package.name, "name": name, "set": True, "path": str(path)}


def remove(home: Home, package: Package, name: str) -> dict[str, object]:
    """Remove one declared secret; a secret that is not stored is a no-op."""
    _declared(package, name)
    values = _read(home, package.name)
    removed = values.pop(name, None) is not None
    path = _write(home, package.name, values)
    return {"package": package.name, "name": name, "removed": removed, "path": str(path)}


def rows(home: Home, packages: Iterable[Package]) -> list[dict[str, object]]:
    """One row per declared secret: its name, description, and whether it is set."""
    found: list[dict[str, object]] = []
    for package in packages:
        values = _read(home, package.name)
        found.extend(
            {
                "package": package.name,
                "name": secret.name,
                "description": secret.description,
                "required": secret.required,
                "set": secret.name in values,
            }
            for secret in package.secrets
        )
    return found


def env_for(home: Home, package: Package) -> dict[str, str]:
    """The owning package's stored values, under the names its runner reads.

    Only declared secrets that are set are exported: a package never sees a
    secret it did not declare, and never one of another package.
    """
    values = _read(home, package.name)
    return {
        env_name(secret.name): values[secret.name]
        for secret in package.secrets
        if secret.name in values
    }


def missing_required(home: Home, package: Package) -> tuple[Secret, ...]:
    """The declared secrets marked required that have no stored value."""
    values = _read(home, package.name)
    return tuple(
        secret for secret in package.secrets if secret.required and secret.name not in values
    )


def has_auth_verb(package: Package) -> bool:
    """Whether the package's entry registers an interactive ``auth`` verb."""
    try:
        text = (package.directory / package.entry).read_text(encoding="utf-8")
    except OSError:
        return False
    return AUTH_VERB_RE.search(text) is not None


def auth_hint(package: Package, name: str) -> str:
    """The exact command that produces a missing secret."""
    if has_auth_verb(package):
        return f"facet auth {package.name}"
    return f"facet secret set {package.name} {name}"


def require_ready(home: Home, package: Package) -> None:
    """Refuse a run whose required secrets are not stored, before the runner starts."""
    missing = missing_required(home, package)
    if not missing:
        return
    names = ", ".join(secret.name for secret in missing)
    hint = (
        f"facet auth {package.name}"
        if has_auth_verb(package)
        else "; ".join(auth_hint(package, secret.name) for secret in missing)
    )
    raise FacetError(
        "machine_cannot",
        f"{package.name} needs a secret that is not set: {names}",
        hint=hint,
        details={"package": package.name, "missing": [secret.name for secret in missing]},
    )


def mask_values(home: Home, package: Package) -> tuple[str, ...]:
    """The values the owning runner received, for masking what it prints."""
    return tuple(env_for(home, package).values())


def mask(text: str, values: Sequence[str]) -> str:
    """``text`` with every occurrence of every stored value replaced by the mask.

    Longest first: when one stored value contains another, replacing the short
    one first would rewrite the long one and leave its remainder in the text.
    """
    for value in sorted((value for value in values if value), key=len, reverse=True):
        text = text.replace(value, MASK)
    return text
