"""The credential vault: ``<home>/secrets/vault.enc``.

A package declares the credentials it uses in its manifest (``[[secrets]]``);
a person stores each value with ``facet secret set`` or produces it with
``facet auth``, and facet keeps every package's values in one encrypted vault
under the facet home. The vault is outside every environment, piece and
record, so ``facet remove`` and ``facet clean`` never touch it — only
``facet secret rm`` and ``facet secret passphrase`` do. A value leaves the
vault in exactly two directions: the owning package's runner environment, and
the mask applied to that runner's output.
"""

from __future__ import annotations

import getpass
import os
import re
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Final

from facetmedia.contract import FacetError
from facetmedia.home import Home, read_json
from facetmedia.registry_types import Package, Secret
from facetmedia.vault import save, try_load, vault_path

__all__ = [
    "AUTH_VERB_RE",
    "MASK",
    "PASSPHRASE_ENV",
    "STORE_DIR",
    "auth_hint",
    "change_passphrase",
    "env_for",
    "env_name",
    "has_auth_verb",
    "mask",
    "mask_values",
    "missing_required",
    "remove",
    "require_ready",
    "rows",
    "secret_names",
    "set_value",
    "store_file",
    "vault_file",
]

#: The directory under the facet home that holds the vault and legacy stores.
STORE_DIR: Final[str] = "secrets"
#: The environment variable that carries the vault passphrase without a terminal.
PASSPHRASE_ENV: Final[str] = "FACET_SECRETS_PASSPHRASE"
#: The exact text every stored value is replaced with before output is kept.
MASK: Final[str] = "***"
#: The runner environment variable prefix; the name follows in upper case.
PREFIX: Final[str] = "FACET_SECRET_"
#: The runner verb a package may provide to produce secrets interactively.
AUTH_VERB_RE: Final[re.Pattern[str]] = re.compile(r"""@runner\.verb\(\s*['"]auth['"]\s*\)""")

_HINT_SET_AGAIN = "set the secrets again with `facet secret set` or `facet auth`"
_HINT_PASSPHRASE = f"set the passphrase in {PASSPHRASE_ENV}, or run at a terminal to be prompted"


def vault_file(home: Home) -> Path:
    """The encrypted vault file."""
    return vault_path(home.root)


def store_file(home: Home, package_name: str) -> Path:
    """Legacy per-package store path (used only to scan for migration)."""
    return home.root / STORE_DIR / f"{package_name}.json"


def env_name(secret_name: str) -> str:
    """The runner's variable for a declared secret: ``FACET_SECRET_<UPPER>``."""
    return f"{PREFIX}{secret_name.upper().replace('-', '_')}"


def _vault_error(message: str, hint: str = _HINT_SET_AGAIN) -> FacetError:
    return FacetError("machine_cannot", message, hint=hint)


def _read_legacy_package(path: Path) -> dict[str, str]:
    stored: object = read_json(path, {})
    if not isinstance(stored, dict):
        return {}
    return {str(k): v for k, v in stored.items() if isinstance(v, str) and v}


def _legacy_paths(home: Home) -> list[Path]:
    """Every leftover per-package plain file, in a stable order."""
    secrets_dir = home.root / STORE_DIR
    if not secrets_dir.is_dir():
        return []
    return sorted(secrets_dir.glob("*.json"))


def _migrate_legacy(home: Home) -> dict[str, dict[str, str]]:
    """Merge every legacy per-package file into the vault, then remove them.

    The vault is written first so an interrupted migration loses nothing: a
    legacy file that remains is merged again on the next open. Every scanned
    file is removed, even one holding no values, so no plaintext remains.
    """
    paths = _legacy_paths(home)
    if not paths:
        return {}
    merged: dict[str, dict[str, str]] = {}
    for path in paths:
        values = _read_legacy_package(path)
        if values:
            merged.setdefault(path.stem, {}).update(values)
    if merged:
        save(home.root, merged, "")
    for path in paths:
        path.unlink(missing_ok=True)
    return merged


def _save_store(home: Home, store: dict[str, dict[str, str]], passphrase: str) -> Path:
    # Keep the store compact: drop empty package entries.
    compact = {pkg: vals for pkg, vals in store.items() if vals}
    return save(home.root, compact, passphrase)


def _drain_legacy(
    home: Home, store: dict[str, dict[str, str]], passphrase: str
) -> dict[str, dict[str, str]]:
    """Merge leftover plain files into an already-open store, then remove them.

    An interrupted migration writes the vault before deleting the plain
    files; the next open would otherwise see the vault and leave the
    plaintext behind forever. Re-saving uses the passphrase that opened
    the vault, so the key never changes under the caller.
    """
    paths = _legacy_paths(home)
    if not paths:
        return store
    merged_any = False
    for path in paths:
        values = _read_legacy_package(path)
        if values:
            store.setdefault(path.stem, {}).update(values)
            merged_any = True
    if merged_any:
        _save_store(home, store, passphrase)
    for path in paths:
        path.unlink(missing_ok=True)
    return store


def _unlock(home: Home, *, allow_prompt: bool = True) -> tuple[dict[str, dict[str, str]], str]:
    """Return the decrypted store and the passphrase that opened it.

    Tries the empty key, then the environment, then a terminal prompt. A vault
    that cannot be opened is never overwritten.
    """
    vpath = vault_file(home)
    if not vpath.exists():
        return _migrate_legacy(home), ""

    store = try_load(home.root, "")
    if store is not None:
        return _drain_legacy(home, store, ""), ""

    env_pass = os.environ.get(PASSPHRASE_ENV)
    if env_pass is not None:
        store = try_load(home.root, env_pass)
        if store is not None:
            return _drain_legacy(home, store, env_pass), env_pass
        raise _vault_error(f"{vpath}: the secrets vault could not be opened with {PASSPHRASE_ENV}")

    if allow_prompt and sys.stdin.isatty():
        prompted = getpass.getpass("secrets passphrase: ")
        store = try_load(home.root, prompted)
        if store is not None:
            return _drain_legacy(home, store, prompted), prompted
        raise _vault_error(f"{vpath}: the secrets vault could not be opened")

    # Not decryptable without a passphrase we can obtain.
    raise _vault_error(
        f"{vpath}: the secrets vault is locked and no passphrase is available",
        hint=_HINT_PASSPHRASE,
    )


def _load_store(home: Home) -> dict[str, dict[str, str]]:
    store, _pass = _unlock(home)
    return store


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
    store, passphrase = _unlock(home)
    store.setdefault(package.name, {})[name] = value
    path = _save_store(home, store, passphrase)
    return {"package": package.name, "name": name, "set": True, "path": str(path)}


def remove(home: Home, package: Package, name: str) -> dict[str, object]:
    """Remove one declared secret; a secret that is not stored is a no-op."""
    _declared(package, name)
    store, passphrase = _unlock(home)
    values = store.get(package.name, {})
    removed = values.pop(name, None) is not None
    if removed:
        store[package.name] = values
        path = _save_store(home, store, passphrase)
    else:
        path = vault_file(home)
    return {"package": package.name, "name": name, "removed": removed, "path": str(path)}


def rows(home: Home, packages: Iterable[Package]) -> list[dict[str, object]]:
    """One row per declared secret: its name, description, and whether it is set.

    ``obtain``, ``link`` and ``produced_by`` travel beside the description;
    a value never does.
    """
    store = _load_store(home)
    found: list[dict[str, object]] = []
    for package in packages:
        values = store.get(package.name, {})
        found.extend(
            {
                "package": package.name,
                "name": secret.name,
                "description": secret.description,
                "required": secret.required,
                "set": secret.name in values,
                "obtain": secret.obtain,
                "link": secret.link,
                "produced_by": secret.produced_by,
            }
            for secret in package.secrets
        )
    return found


def env_for(home: Home, package: Package) -> dict[str, str]:
    """The owning package's stored values, under the names its runner reads."""
    store = _load_store(home)
    values = store.get(package.name, {})
    return {
        env_name(secret.name): values[secret.name]
        for secret in package.secrets
        if secret.name in values
    }


def secret_names(package: Package) -> frozenset[str]:
    """Every declared secret name for ``package``, required or not."""
    return frozenset(secret.name for secret in package.secrets)


def missing_required(home: Home, package: Package) -> tuple[Secret, ...]:
    """The declared secrets marked required that have no stored value."""
    store = _load_store(home)
    values = store.get(package.name, {})
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
    if not package.secrets:
        return
    try:
        missing = missing_required(home, package)
    except FacetError as exc:
        # A locked/missing vault is a missing secret from the runner's point of view.
        raise FacetError(
            "machine_cannot",
            f"{package.name} needs a secret that is not set",
            hint=exc.hint or _HINT_PASSPHRASE,
            details={"package": package.name},
        ) from exc
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
    """``text`` with every stored value replaced by the fixed mask."""
    for value in sorted((value for value in values if value), key=len, reverse=True):
        text = text.replace(value, MASK)
    return text


def change_passphrase(home: Home, new_passphrase: str) -> dict[str, object]:
    """Re-encrypt the vault with ``new_passphrase`` (empty removes protection)."""
    store, _current = _unlock(home)
    path = _save_store(home, store, new_passphrase)
    return {
        "path": str(path),
        "protected": bool(new_passphrase),
        "passphrase": "set" if new_passphrase else "removed",
    }
