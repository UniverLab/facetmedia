"""Encrypted secrets vault: ``<home>/secrets/vault.enc``.

One XSalsa20-Poly1305 box holds every package's credentials. The key is
derived from the user name, host name, passphrase (empty when none is set)
and facet's own domain separator, so the file is bound to this account on
this machine and unreadable when copied elsewhere. Writes are atomic and the
file is kept owner-only (0600) inside an owner-only directory (0700).
"""

from __future__ import annotations

import contextlib
import getpass
import hashlib
import json
import os
import socket
import tempfile
from pathlib import Path
from typing import Any, Final

from nacl import secret as nacl_secret
from nacl.exceptions import CryptoError

#: The path of the vault file under the facet home.
VAULT: Final[str] = "secrets/vault.enc"
#: The domain separator mixed into the key derivation.
DOMAIN: Final[bytes] = b"facetmedia-secrets-v1"


def vault_path(home_root: Path) -> Path:
    """The vault file under ``home_root``."""
    return home_root / VAULT


def derive_key(passphrase: str) -> bytes:
    """Blake2b-256 of user, host, passphrase and the domain separator."""
    user = getpass.getuser()
    host = socket.gethostname()
    material = "|".join((user, host, passphrase)).encode() + b"|" + DOMAIN
    return hashlib.blake2b(material, digest_size=32).digest()


def encrypt(data: bytes, passphrase: str) -> bytes:
    """Encrypt ``data`` for this account and machine."""
    box = nacl_secret.SecretBox(derive_key(passphrase))
    return bytes(box.encrypt(data))


def decrypt(blob: bytes, passphrase: str) -> bytes:
    """Decrypt ``blob`` or raise ``CryptoError`` when it does not open."""
    box = nacl_secret.SecretBox(derive_key(passphrase))
    return bytes(box.decrypt(blob))


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Write bytes atomically: crash-safe, never a half-written vault."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    os.chmod(path, 0o600)


def save(home_root: Path, store: dict[str, dict[str, str]], passphrase: str) -> Path:
    """Encrypt and write the whole store atomically."""
    path = vault_path(home_root)
    payload = json.dumps(store, indent=2, sort_keys=True).encode("utf-8")
    _atomic_write_bytes(path, encrypt(payload, passphrase))
    return path


def try_load(home_root: Path, passphrase: str) -> dict[str, dict[str, str]] | None:
    """Decrypt the vault with ``passphrase``, or ``None`` if it does not open."""
    path = vault_path(home_root)
    try:
        blob = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    try:
        data = decrypt(blob, passphrase)
        obj: Any = json.loads(data.decode("utf-8"))
    except (CryptoError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    store: dict[str, dict[str, str]] = {}
    for pkg, values in obj.items():
        if not isinstance(pkg, str) or not isinstance(values, dict):
            return None
        cleaned: dict[str, str] = {}
        for key, value in values.items():
            if isinstance(key, str) and isinstance(value, str) and value:
                cleaned[key] = value
        store[pkg] = cleaned
    return store
