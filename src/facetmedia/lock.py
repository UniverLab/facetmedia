"""The facet lock: one ``flock`` under ``<home>/state/lock`` serializing heavy work.

Heavy work (installing, removing, running an ``exclusive`` package) holds the
lock exclusively; light runs hold it shared, so they run side by side but
nobody deletes an environment under them. The kernel releases a ``flock``
when its process dies, so a crash never leaves the lock stuck.

The lock file is opened ``O_CREAT | O_RDWR`` and never truncated by a waiter.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import sys
import time
from collections.abc import Iterator
from typing import Literal

from facetmedia.contract import FacetError
from facetmedia.home import Home, atomic_write_json, read_json

LockMode = Literal["exclusive", "shared"]

#: How long a waiter sleeps between two non-blocking attempts.
POLL_S = 0.5


@contextlib.contextmanager
def hold(
    mode: LockMode,
    purpose: str,
    wait: bool = True,
    timeout_s: float = 3600.0,
) -> Iterator[None]:
    """Hold ``<home>/state/lock`` for the duration of the ``with`` block.

    ``exclusive`` serializes heavy work; ``shared`` lets light runs overlap.
    With ``wait=False`` a busy lock fails at once with ``lock_busy``; otherwise
    the caller waits at most ``timeout_s`` seconds, polling every ``POLL_S``.
    """
    home = Home.from_env()
    fd = os.open(home.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        _acquire(fd, home, mode, purpose, wait, timeout_s)
        acquired = True
        yield
    finally:
        if acquired:
            _release(fd, home, mode)
        else:
            os.close(fd)


def _acquire(
    fd: int,
    home: Home,
    mode: LockMode,
    purpose: str,
    wait: bool,
    timeout_s: float,
) -> None:
    """Poll ``flock`` until it is taken, the caller gives up, or time runs out."""
    operation = fcntl.LOCK_EX if mode == "exclusive" else fcntl.LOCK_SH
    deadline = time.monotonic() + timeout_s
    announced = False
    while True:
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if not wait or time.monotonic() >= deadline:
                raise _busy_error(home) from None
            if not announced:
                _announce_wait(home)
                announced = True
            time.sleep(POLL_S)
    if mode == "exclusive":
        _write_holder(home, purpose)


def _release(fd: int, home: Home, mode: LockMode) -> None:
    """Drop the lock: an exclusive holder removes its ``lock.json`` first."""
    try:
        if mode == "exclusive":
            with contextlib.suppress(OSError):
                os.unlink(home.lock_json)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_holder(home: Home, purpose: str) -> None:
    """Record who holds the lock exclusively, atomically."""
    atomic_write_json(
        home.lock_json,
        {
            "mode": "exclusive",
            "pid": os.getpid(),
            "purpose": purpose,
            "since": _now(),
        },
    )


def _holder_lines(home: Home) -> tuple[str | None, str | None]:
    """``(live holder text, stale note)`` from ``lock.json``; each may be ``None``."""
    try:
        info = read_json(home.lock_json, None)
    except FacetError as exc:
        return None, f"stale info: lock.json is unreadable ({exc.message})"
    if not isinstance(info, dict):
        return None, None
    pid = info.get("pid")
    if type(pid) is int and _pid_alive(pid):
        since = info.get("since", "unknown time")
        purpose = info.get("purpose", "unknown purpose")
        return f"pid {pid} since {since}: {purpose}", None
    return None, f"stale info: lock.json records pid {pid}, which is not alive"


def _announce_wait(home: Home) -> None:
    """Print the one waiting line — or the stale-info report — to stderr."""
    holder, stale = _holder_lines(home)
    if holder is not None:
        print(f"waiting for the facet lock, held by {holder}", file=sys.stderr)
    elif stale is not None:
        print(f"waiting for the facet lock ({stale}, the flock is the truth)", file=sys.stderr)
    else:
        print("waiting for the facet lock, held by another process", file=sys.stderr)


def _busy_error(home: Home) -> FacetError:
    """The ``lock_busy`` failure, naming the holder when lock.json knows it."""
    holder, stale = _holder_lines(home)
    if holder is not None:
        return FacetError(
            "lock_busy",
            f"the facet lock is busy, held by {holder}",
            details={"holder": holder},
        )
    note = stale if stale is not None else "held by another process"
    return FacetError("lock_busy", f"the facet lock is busy, {note}")


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` names a living process; signal 0 probes without sending."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _now() -> str:
    """A UTC timestamp for ``lock.json``'s ``since`` field."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
