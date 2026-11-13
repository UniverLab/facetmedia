"""Tell the person when an installed package has a newer version.

The registry is pinned, so nothing here changes what is installed: it resolves
the ref ``facet update`` pinned, compares the versions published at that ref
with the ones on disk, and keeps the answer in ``state/update-check.json``.
Every verb that uses facet runs it at most once every 24 hours, on a 3-second
budget, and every failure is logged and dropped.

``FACET_NO_UPDATE_CHECK=1`` disables both the check and the notice;
``FACET_GITHUB_RAW`` replaces the raw content base, for the tests.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import tomllib
import urllib.request
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from typing import Final

from packaging.version import InvalidVersion, Version

from facetmedia import env as env_module
from facetmedia import installed as installed_module
from facetmedia import update
from facetmedia.home import Home, atomic_write_json, read_json
from facetmedia.registry import load_active_registry

__all__ = [
    "CHECK_VERBS",
    "STATE_FILE",
    "begin",
    "disabled",
    "maybe_check",
    "notice_line",
    "on_tty",
    "pending_notices",
    "print_notice",
]

logger: Final[logging.Logger] = logging.getLogger("facetmedia")

STATE_FILE: Final[str] = "update-check.json"
REGISTRY_STATE: Final[str] = "registry.json"
#: The verbs that look for updates while they are being used.
CHECK_VERBS: Final[frozenset[str]] = frozenset(
    {"pull", "run", "check", "preview", "list", "doctor"}
)
RAW_BASE: Final[str] = "https://raw.githubusercontent.com/UniverLab/facetmedia-registry"
WINDOW_S: Final[float] = 24 * 3600.0
FAILED_WINDOW_S: Final[float] = 3600.0
DEADLINE_S: Final[float] = 3.0
WORKERS: Final[int] = 8
TIMESTAMP: Final[str] = "%Y-%m-%dT%H:%M:%SZ"
NOTICE_TAIL: Final[str] = " — run facet update, then facet pull --package <name>"


def disabled() -> bool:
    """Whether ``FACET_NO_UPDATE_CHECK=1`` turned the check and the notice off."""
    return os.environ.get("FACET_NO_UPDATE_CHECK") == "1"


def on_tty() -> bool:
    """Whether a person is looking at stderr, where the notice goes."""
    return sys.stderr.isatty()


def maybe_check(home: Home) -> None:
    """Refresh ``state/update-check.json`` when it is due; never raise.

    The caller's output and exit code are unaffected: a network or parse
    failure is logged and, when the ref itself could not be resolved, the
    check is retried after an hour instead of a day.
    """
    if disabled():
        return
    try:
        _maybe_check(home)
    except Exception:
        logger.exception("update check failed")


def begin(home: Home) -> list[dict[str, object]]:
    """The check a verb runs while it is used, plus the notices it may show."""
    if disabled():
        return []
    maybe_check(home)
    try:
        return pending_notices(home)
    except Exception:
        logger.exception("update notices failed")
        return []


def print_notice(notices: Sequence[Mapping[str, object]], as_json: bool) -> None:
    """One stderr line after the verb's output, at a terminal without ``--json``."""
    if notices and not as_json and on_tty():
        print(notice_line(notices), file=sys.stderr)


def _maybe_check(home: Home) -> None:
    if not _due(_stored(home)):
        return
    state = _registry_state(home)
    ref = state.get("ref")
    if not isinstance(ref, str) or not ref or update.is_commit(ref):
        return
    deadline = time.monotonic() + DEADLINE_S
    try:
        commit = update.resolve_ref(ref, timeout=_remaining(deadline))
    except Exception as exc:
        logger.info("update check: %s: %s", ref, exc)
        _write(home, {"checked_at": _now(), "failed": True})
        return
    if commit == state.get("commit"):
        _write(home, {"checked_at": _now(), "ref": ref, "commit": commit, "updates": []})
        return
    updates = _fetch_updates(commit, _installed_versions(home), deadline)
    _write(
        home,
        {"checked_at": _now(), "ref": ref, "commit": commit, "updates": updates},
    )


def pending_notices(home: Home) -> list[dict[str, object]]:
    """The updates still pending: stored ones the installed version has not reached."""
    stored = _stored(home)
    if not isinstance(stored, dict) or stored.get("failed") is True:
        return []
    rows = stored.get("updates")
    if not isinstance(rows, list):
        return []
    current = _installed_versions(home)
    notices = [
        notice
        for row in rows
        if isinstance(row, dict) and (notice := _notice(row, current)) is not None
    ]
    return sorted(notices, key=lambda notice: str(notice["package"]))


def _notice(row: Mapping[str, object], current: Mapping[str, str]) -> dict[str, object] | None:
    name = row.get("package")
    available = row.get("available")
    if not isinstance(name, str) or not isinstance(available, str):
        return None
    installed = current.get(name)
    if installed is None or not _newer(available, installed):
        return None
    return {"kind": "update", "package": name, "installed": installed, "available": available}


def notice_line(notices: Sequence[Mapping[str, object]]) -> str:
    """The one stderr line naming every pending update."""
    parts = [
        f"{notice['package']} {notice['installed']} → {notice['available']}" for notice in notices
    ]
    return "updates: " + ", ".join(parts) + NOTICE_TAIL


def _stored(home: Home) -> object:
    return read_json(home.state_file(STATE_FILE), None)


def _due(stored: object) -> bool:
    """Whether a check may run now, given the last one and whether it failed."""
    if not isinstance(stored, dict):
        return True
    checked_at = _parse_time(stored.get("checked_at"))
    if checked_at is None:
        return True
    window = FAILED_WINDOW_S if stored.get("failed") is True else WINDOW_S
    return time.time() - checked_at >= window


def _parse_time(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.strptime(value, TIMESTAMP).replace(tzinfo=UTC)
    except ValueError:
        return None
    return stamp.timestamp()


def _registry_state(home: Home) -> dict[str, object]:
    state: object = read_json(home.state_file(REGISTRY_STATE), {})
    return state if isinstance(state, dict) else {}


def _installed_versions(home: Home) -> dict[str, str]:
    """Every package's installed version, by the three-step rule.

    A recorded entry wins; without one the snapshot's version answers when the
    package is installed; otherwise there is no installed version.
    """
    recorded = installed_module.entries(home)
    try:
        active = load_active_registry()
    except Exception as exc:
        logger.info("update check: the registry is unavailable: %s", exc)
        return {}
    versions: dict[str, str] = {}
    for package in active.packages.values():
        entry = recorded.get(package.name, {})
        recorded_version = entry.get("version")
        if isinstance(recorded_version, str) and recorded_version:
            versions[package.name] = recorded_version
        elif env_module.is_installed(package, home):
            versions[package.name] = package.version
    return versions


def _fetch_updates(
    commit: str, versions: Mapping[str, str], deadline: float
) -> list[dict[str, str]]:
    """The packages with a newer version at ``commit``, within the deadline."""
    base = _raw_base()
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    try:
        futures: dict[Future[dict[str, str] | None], str] = {
            pool.submit(_one_update, base, commit, name, version, deadline): name
            for name, version in versions.items()
        }
        done, _pending = wait(list(futures), timeout=_remaining(deadline))
        found = [_finished(future) for future in done]
    finally:
        pool.shutdown(wait=False)
    return sorted((item for item in found if item is not None), key=lambda item: item["package"])


def _finished(future: Future[dict[str, str] | None]) -> dict[str, str] | None:
    """One worker's answer, or ``None`` when it failed or answered too late."""
    try:
        return future.result()
    except Exception as exc:
        logger.info("update check: %s", exc)
        return None


def _one_update(
    base: str, commit: str, name: str, installed: str, deadline: float
) -> dict[str, str] | None:
    url = f"{base}/{commit}/packages/{name}/package.toml"
    text = _fetch_text(url, deadline)
    manifest = tomllib.loads(text)
    available = manifest.get("version")
    if not isinstance(available, str) or not _newer(available, installed):
        return None
    return {"package": name, "installed": installed, "available": available}


def _fetch_text(url: str, deadline: float) -> str:
    """Read ``url`` with the time left before ``deadline`` as its timeout."""
    timeout = _remaining(deadline)
    if timeout <= 0:
        raise TimeoutError(f"{url}: the update check deadline passed")
    request = urllib.request.Request(url, headers=update._headers(None))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return bytes(response.read()).decode("utf-8", "replace")


def _remaining(deadline: float) -> float:
    return deadline - time.monotonic()


def _raw_base() -> str:
    return (os.environ.get("FACET_GITHUB_RAW") or RAW_BASE).rstrip("/")


def _newer(available: str, installed: str) -> bool:
    """Whether ``available`` is strictly newer than ``installed``."""
    try:
        return Version(available) > Version(installed)
    except InvalidVersion:
        return False


def _write(home: Home, payload: dict[str, object]) -> None:
    atomic_write_json(home.state_file(STATE_FILE), payload, home.tmp_dir)


def _now() -> str:
    return datetime.now(UTC).strftime(TIMESTAMP)
