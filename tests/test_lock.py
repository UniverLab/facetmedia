"""Two-process tests for the facet lock (``<home>/state/lock``)."""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from facetmedia.cli import _lock_params, build_parser
from facetmedia.contract import FacetError
from facetmedia.home import Home, atomic_write_json
from facetmedia.lock import POLL_S, hold

CTX = mp.get_context("spawn")
JOIN_S = 30.0
EXIT_USAGE = 2

# One real sleep of POLL_S (0.5 s) separates every non-blocking retry.
SHORT_WAIT_S = 0.7

LOCKING_VERBS = [
    ["update"],
    ["pull"],
    ["run"],
    ["check"],
    ["rm"],
    ["gc"],
    ["license", "accept"],
]

LIGHT_VERBS = [["list"], ["describe"], ["preview"], ["license", "list"]]

HolderStart = Callable[[str, str], tuple[mp.Process, Any]]


def _noop() -> None:
    pass


def _hold_exclusive(purpose: str, ready: Any, release: Any) -> None:
    _run_holder("exclusive", purpose, ready, release)


def _hold_shared(purpose: str, ready: Any, release: Any) -> None:
    _run_holder("shared", purpose, ready, release)


def _run_holder(mode: str, purpose: str, ready: Any, release: Any) -> None:
    """Hold the lock in a child process until the parent releases it."""
    try:
        with hold(mode, purpose, wait=True, timeout_s=JOIN_S):
            ready.put(("held", os.getpid()))
            release.wait(JOIN_S)
    except FacetError as exc:
        ready.put(("busy", exc.code))


def _dead_pid() -> int:
    """A pid that has exited and been reaped, so ``os.kill`` finds it gone."""
    proc = CTX.Process(target=_noop)
    proc.start()
    pid = proc.pid
    proc.join(timeout=JOIN_S)
    assert pid is not None and not proc.is_alive()
    return pid


@pytest.fixture
def holder_factory() -> Iterator[HolderStart]:
    """Start exclusive/shared holders in real child processes; clean them up."""
    started: list[tuple[mp.Process, Any]] = []

    def start(mode: str, purpose: str) -> tuple[mp.Process, Any]:
        ready: Any = CTX.Queue()
        release: Any = CTX.Event()
        target = _hold_exclusive if mode == "exclusive" else _hold_shared
        proc = CTX.Process(target=target, args=(purpose, ready, release), daemon=True)
        proc.start()
        started.append((proc, release))
        status, pid = ready.get(timeout=JOIN_S)
        assert status == "held", f"holder process {pid} could not take the lock"
        return proc, release

    yield start

    for proc, release in started:
        if proc.is_alive():
            # Never set() an event whose child is dead: a child SIGKILLed while
            # sleeping on wait() cannot answer notify_all, and set() would hang.
            release.set()
        proc.join(timeout=JOIN_S)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=JOIN_S)


def test_exclusive_blocks_exclusive(
    holder_factory: HolderStart, capsys: pytest.CaptureFixture[str]
) -> None:
    proc, release = holder_factory("exclusive", "installing kokoro")
    info = json.loads(Home.from_env().lock_json.read_text(encoding="utf-8"))
    assert info == {
        "mode": "exclusive",
        "pid": proc.pid,
        "purpose": "installing kokoro",
        "since": info["since"],
    }
    started = time.monotonic()
    with (
        pytest.raises(FacetError) as exc_info,
        hold("exclusive", "gc", wait=True, timeout_s=SHORT_WAIT_S),
    ):
        pass
    waited = time.monotonic() - started
    assert exc_info.value.code == "lock_busy"
    assert waited >= POLL_S, "the waiter did not poll; it grabbed the lock at once"
    err = capsys.readouterr().err
    assert f"waiting for the facet lock, held by pid {proc.pid} since " in err
    assert "installing kokoro" in err
    assert err.count("waiting for the facet lock") == 1, "the waiting line prints once"
    release.set()
    with hold("exclusive", "gc", wait=True, timeout_s=JOIN_S):
        pass  # free again now that the holder released


def test_exclusive_blocks_shared(holder_factory: HolderStart) -> None:
    _proc, release = holder_factory("exclusive", "removing an environment")
    with (
        pytest.raises(FacetError) as exc_info,
        hold("shared", "run", wait=True, timeout_s=SHORT_WAIT_S),
    ):
        pass
    assert exc_info.value.code == "lock_busy"
    release.set()
    with hold("shared", "run", wait=True, timeout_s=JOIN_S):
        pass


def test_shared_allows_shared(holder_factory: HolderStart) -> None:
    _proc, _release = holder_factory("shared", "light run")
    with hold("shared", "another light run", wait=False, timeout_s=0.0):
        assert not Home.from_env().lock_json.exists(), "shared holders write no lock.json"


def test_no_wait_fails_at_once_naming_the_holder(holder_factory: HolderStart) -> None:
    proc, _release = holder_factory("exclusive", "installing kokoro")
    started = time.monotonic()
    with (
        pytest.raises(FacetError) as exc_info,
        hold("exclusive", "gc", wait=False, timeout_s=3600.0),
    ):
        pass
    assert time.monotonic() - started < 1.0, "--no-wait must not sit out the timeout"
    assert exc_info.value.code == "lock_busy"
    assert "installing kokoro" in exc_info.value.message
    assert str(proc.pid) in exc_info.value.message


def test_sigkill_of_the_holder_frees_the_lock(holder_factory: HolderStart) -> None:
    proc, _release = holder_factory("exclusive", "stuck heavy job")
    proc.kill()  # SIGKILL: the holder's cleanup never runs
    proc.join(timeout=JOIN_S)
    assert not proc.is_alive()
    with hold("exclusive", "after the crash", wait=False, timeout_s=0.0):
        pass  # the kernel dropped the dead process's flock
    assert not Home.from_env().lock_json.exists(), "the new holder cleaned up"


def test_stale_lock_json_is_reported_and_ignored(
    holder_factory: HolderStart, capsys: pytest.CaptureFixture[str]
) -> None:
    home = Home.from_env()
    stale = {
        "mode": "exclusive",
        "pid": _dead_pid(),
        "purpose": "ghost job",
        "since": "2020-01-01T00:00:00Z",
    }
    atomic_write_json(home.lock_json, stale)
    # The flock itself is the truth: a free lock with a stale file does not block.
    with hold("exclusive", "real work", wait=False, timeout_s=0.0):
        pass
    assert not home.lock_json.exists()
    # While a lock really is held, the stale file is reported, not trusted.
    atomic_write_json(home.lock_json, stale)
    _proc, release = holder_factory("shared", "light run")
    with (
        pytest.raises(FacetError) as exc_info,
        hold("exclusive", "blocked", wait=True, timeout_s=SHORT_WAIT_S),
    ):
        pass
    assert exc_info.value.code == "lock_busy"
    err = capsys.readouterr().err
    assert "waiting for the facet lock" in err
    assert "stale info" in err
    assert str(stale["pid"]) in err
    release.set()


@pytest.mark.parametrize("argv", LOCKING_VERBS, ids=" ".join)
def test_locking_verbs_take_the_lock_flags(argv: list[str]) -> None:
    assert _lock_params(build_parser().parse_args([*argv, "--no-wait"])) == (False, 3600.0)
    assert _lock_params(build_parser().parse_args([*argv, "--wait-timeout", "5"])) == (True, 5.0)


def test_lock_flags_default_to_waiting_an_hour() -> None:
    assert _lock_params(build_parser().parse_args(["pull"])) == (True, 3600.0)


@pytest.mark.parametrize("argv", LIGHT_VERBS, ids=" ".join)
def test_light_verbs_reject_the_lock_flags(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args([*argv, "--no-wait"])
    assert exit_info.value.code == EXIT_USAGE
