"""Facet home directory layout and crash-safe state files.

Everything facet installs or remembers lives under one directory,
``~/.facet`` (``FACET_HOME`` overrides it), and nothing goes to the system.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from facetmedia.contract import FacetError

_MNT_RE = re.compile(r"^/mnt/[a-z]/")

_T = TypeVar("_T")


def require_posix() -> None:
    """Refuse to run on native Windows; WSL must be used instead."""
    if sys.platform == "win32":
        raise FacetError(
            "machine_cannot",
            "native Windows is not supported in v0.1; use WSL",
        )


class Home:
    """Paths under a facet home directory, created on first use."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    @classmethod
    def from_env(cls) -> Home:
        raw = os.environ.get("FACET_HOME") or str(Path.home() / ".facet")
        home = cls(Path(raw))
        home.ensure()
        return home

    def ensure(self) -> Home:
        root_abs = os.path.abspath(str(self._root))
        if _MNT_RE.match(root_abs + "/"):
            raise FacetError(
                "machine_cannot",
                f"{root_abs}: Windows drives are not supported",
                hint="keep FACET_HOME on the Linux filesystem",
            )
        self._root = Path(root_abs)
        created = not self._root.exists()
        self._root.mkdir(parents=True, exist_ok=True)
        if created:
            os.chmod(self._root, 0o700)
        for directory in (
            self._root / "registry",
            self._root / "state",
            self._root / "envs",
            self._root / "models" / "direct",
            self._root / "models" / "hf",
            self._root / "python",
            self._root / "cache" / "uv",
            self._root / "logs",
            self._root / "tmp",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        same_filesystem_check(self)
        return self

    def registry_dir(self, commit: str) -> Path:
        return self._root / "registry" / commit

    def state_file(self, name: str) -> Path:
        return self._root / "state" / name

    def env_dir(self, package: str, version: str) -> Path:
        return self._root / "envs" / package / version

    def model_dir(self, model_id: str) -> Path:
        return self._root / "models" / "direct" / model_id

    @property
    def envs_dir(self) -> Path:
        return self._root / "envs"

    @property
    def models_dir(self) -> Path:
        return self._root / "models"

    @property
    def hf_dir(self) -> Path:
        return self._root / "models" / "hf"

    @property
    def python_dir(self) -> Path:
        return self._root / "python"

    @property
    def uv_cache_dir(self) -> Path:
        return self._root / "cache" / "uv"

    @property
    def licenses_file(self) -> Path:
        return self._root / "state" / "licenses.json"

    @property
    def lock_file(self) -> Path:
        return self._root / "state" / "lock"

    @property
    def lock_json(self) -> Path:
        return self._root / "state" / "lock.json"

    @property
    def logs_dir(self) -> Path:
        return self._root / "logs"

    @property
    def tmp_dir(self) -> Path:
        return self._root / "tmp"


def same_filesystem_check(home: Home) -> None:
    """Verify the home's working directories share one filesystem."""
    anchor = os.stat(home.envs_dir).st_dev
    for directory in (home.models_dir, home.uv_cache_dir, home.tmp_dir):
        if os.stat(directory).st_dev != anchor:
            raise FacetError(
                "machine_cannot",
                f"{home.root}: FACET_HOME must be on a single filesystem",
            )


def uv_env(home: Home) -> dict[str, str]:
    """The environment every uv call gets."""
    env = dict(os.environ)
    env["UV_CACHE_DIR"] = str(home.uv_cache_dir)
    env["UV_PYTHON_INSTALL_DIR"] = str(home.python_dir)
    env["UV_PYTHON_PREFERENCE"] = "only-managed"
    env["UV_NO_CONFIG"] = "1"
    env["HF_HOME"] = str(home.hf_dir)
    env["HF_HUB_DISABLE_TELEMETRY"] = "1"
    return env


def atomic_write_json(path: Path | str, obj: Any, tmp_dir: Path | str | None = None) -> None:
    """Write JSON so a crash mid-write leaves the previous version intact."""
    target = Path(path)
    scratch = Path(tmp_dir) if tmp_dir is not None else Home.from_env().tmp_dir
    scratch.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(obj, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=str(scratch))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def read_json(path: Path | str, default: _T) -> _T | Any:
    """Read a JSON state file, returning ``default`` when it is missing."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except OSError as exc:
        raise FacetError("internal", f"{path}: cannot read state file") from exc
    try:
        return json.loads(text)
    except ValueError as exc:
        raise FacetError("internal", f"{path}: corrupt state file") from exc


def new_run_id() -> str:
    """A UTC timestamp plus 6 random hex characters."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def log_file(run_id: str) -> Path:
    """The log path for ``run_id``; creates nothing."""
    try:
        day = f"{run_id[0:4]}-{run_id[4:6]}-{run_id[6:8]}"
        datetime.strptime(day, "%Y-%m-%d")
    except (ValueError, IndexError):
        day = datetime.now(UTC).strftime("%Y-%m-%d")
    return Home.from_env().logs_dir / day / f"{run_id}.log"
