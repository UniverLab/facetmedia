"""The update check, the installed-version record and the notice.

Every HTTP call is answered by a fake ``urlopen``; nothing here touches the
network.  The suite's autouse fixture turns ``FACET_NO_UPDATE_CHECK`` on, so a
test that exercises the check deletes it first.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from facetmedia import installed, introspect, prune, update, updates
from facetmedia.cli import main
from facetmedia.home import Home
from facetmedia.registry import load_registry

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
OTHER_COMMIT = "fedcba9876543210fedcba9876543210fedcba98"
REF = "feature/v0.1"
RAW_BASE = "http://raw.test/registry"
ECHO_AVAILABLE = 'name = "echo"\nversion = "0.2.0"\n'
NOTICE = "updates: echo 0.1.0 → 0.2.0 — run facet update, then facet pull --package <name>\n"


class _Answer:
    """The context-managed response ``urlopen`` returns in these tests."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _Answer:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))


def _home() -> Home:
    return Home.from_env()


def _write(home: Home, name: str, payload: object) -> None:
    path = home.state_file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _read(home: Home, name: str) -> Any:
    return json.loads(home.state_file(name).read_text(encoding="utf-8"))


def _stamp(hours_ago: float = 0.0) -> str:
    moment = datetime.now(UTC) - timedelta(hours=hours_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _fake_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
    bodies: dict[str, str] | None = None,
) -> None:
    """Answer ``urlopen`` by URL substring, recording every request."""
    answers = bodies or {"commits/": COMMIT, "packages/echo/package.toml": ECHO_AVAILABLE}

    def urlopen(request: urllib.request.Request, timeout: float | None = None) -> _Answer:
        calls.append(request.full_url)
        for needle, body in answers.items():
            if needle in request.full_url:
                return _Answer(body.encode("utf-8"))
        raise OSError(f"unexpected url: {request.full_url}")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)


def _checking(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the check on and give it a raw base the fake server answers for."""
    monkeypatch.delenv("FACET_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setenv("FACET_GITHUB_RAW", RAW_BASE)


def _recorded_echo(home: Home, version: str = "0.1.0") -> None:
    _write(home, "registry.json", {"commit": OTHER_COMMIT, "ref": REF})
    installed.record(home, "echo", version)


def _fake_installed(home: Home, package_name: str = "echo") -> None:
    """A complete environment marker, so ``env.is_installed`` answers yes."""
    package = load_registry(FIXTURES).package(package_name)
    assert package is not None
    env_dir = home.env_dir(package.name, package.version)
    env_dir.mkdir(parents=True)
    (env_dir / ".complete").write_text(package.lock_sha256, encoding="utf-8")


def _row(data: dict[str, Any], name: str) -> dict[str, Any]:
    rows = data["packages"]
    assert isinstance(rows, list)
    return next(row for row in rows if row["name"] == name)


# --- the installed-version record.


def test_a_removed_package_leaves_no_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    _registry(monkeypatch)
    home = _home()
    installed.record(home, "echo", "0.1.0")
    assert installed.version_of(home, "echo") == "0.1.0"

    prune.rm_package(home, "echo")

    assert installed.entries(home) == {}
    assert installed.version_of(home, "echo") is None


# --- the three-step installed-version rule, through ``facet list``.


def test_the_recorded_entry_wins_over_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registry(monkeypatch)
    home = _home()
    installed.record(home, "echo", "0.0.9")

    row = _row(introspect.list_data(available=True), "echo")

    assert row["installed_version"] == "0.0.9"
    assert row["update_available"] is True


def test_an_install_without_an_entry_uses_the_snapshot_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registry(monkeypatch)
    _fake_installed(_home())

    row = _row(introspect.list_data(available=True), "echo")

    assert row["installed_version"] == "0.1.0"
    assert row["update_available"] is False


def test_a_package_that_is_not_installed_has_no_installed_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registry(monkeypatch)

    row = _row(introspect.list_data(available=True), "echo")

    assert row["installed_version"] is None
    assert row["update_available"] is False


# --- the remote check.


def test_the_check_resolves_the_recorded_ref_and_stores_the_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registry(monkeypatch)
    _checking(monkeypatch)
    home = _home()
    _recorded_echo(home)
    calls: list[str] = []
    _fake_urlopen(monkeypatch, calls)

    updates.maybe_check(home)

    assert calls[0].endswith(f"/commits/{REF}")
    assert calls[1] == f"{RAW_BASE}/{COMMIT}/packages/echo/package.toml"
    state = _read(home, "update-check.json")
    assert state["ref"] == REF
    assert state["commit"] == COMMIT
    assert state["updates"] == [{"package": "echo", "installed": "0.1.0", "available": "0.2.0"}]


def test_a_commit_hash_ref_is_never_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    _registry(monkeypatch)
    _checking(monkeypatch)
    home = _home()
    _write(home, "registry.json", {"commit": COMMIT, "ref": COMMIT})
    installed.record(home, "echo", "0.1.0")
    calls: list[str] = []
    _fake_urlopen(monkeypatch, calls)

    updates.maybe_check(home)

    assert calls == []
    assert not home.state_file("update-check.json").exists()


@pytest.mark.parametrize(
    ("hours_ago", "failed", "due"),
    [
        (1.0, False, False),
        (25.0, False, True),
        (0.5, True, False),
        (2.0, True, True),
    ],
)
def test_the_window_is_a_day_or_an_hour_after_a_failure(
    monkeypatch: pytest.MonkeyPatch, hours_ago: float, failed: bool, due: bool
) -> None:
    _registry(monkeypatch)
    _checking(monkeypatch)
    home = _home()
    _recorded_echo(home)
    stored: dict[str, object] = {"checked_at": _stamp(hours_ago)}
    if failed:
        stored["failed"] = True
    _write(home, "update-check.json", stored)
    calls: list[str] = []
    _fake_urlopen(monkeypatch, calls)

    updates.maybe_check(home)

    assert bool(calls) is due


def test_a_package_that_misses_the_deadline_is_left_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registry(monkeypatch)
    _checking(monkeypatch)
    home = _home()
    _recorded_echo(home)
    installed.record(home, "termsy", "0.1.0")
    monkeypatch.setattr(updates, "DEADLINE_S", 0.2)
    monkeypatch.setattr(update, "resolve_ref", lambda ref, timeout: COMMIT)
    gate = threading.Event()

    def fake_fetch(url: str, deadline: float) -> str:
        if "termsy" in url:
            gate.wait(5)
            return 'name = "termsy"\nversion = "0.3.0"\n'
        return ECHO_AVAILABLE

    monkeypatch.setattr(updates, "_fetch_text", fake_fetch)

    updates.maybe_check(home)
    gate.set()

    packages = [row["package"] for row in _read(home, "update-check.json")["updates"]]
    assert packages == ["echo"]


def test_a_failure_to_resolve_stores_the_failed_flag_and_raises_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registry(monkeypatch)
    _checking(monkeypatch)
    monkeypatch.setenv("FACET_GITHUB_API", "http://127.0.0.1:1")
    home = _home()
    _recorded_echo(home)

    updates.maybe_check(home)

    state = _read(home, "update-check.json")
    assert state["failed"] is True
    assert "updates" not in state


def test_a_failed_check_does_not_change_the_verb(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry(monkeypatch)
    _checking(monkeypatch)
    monkeypatch.setenv("FACET_GITHUB_API", "http://127.0.0.1:1")
    home = _home()
    _recorded_echo(home)

    assert main(["list", "--json"]) == 0

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert "notices" not in envelope
    assert _read(home, "update-check.json")["failed"] is True


def test_a_corrupt_check_file_does_not_change_the_verb(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry(monkeypatch)
    _checking(monkeypatch)
    home = _home()
    _recorded_echo(home)
    home.state_file("update-check.json").write_text("{not json", encoding="utf-8")

    assert main(["list", "--json"]) == 0

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert "notices" not in envelope


# --- the notice.


def _stored_update(home: Home, installed_version: str = "0.1.0") -> None:
    _write(home, "registry.json", {"commit": OTHER_COMMIT, "ref": REF})
    installed.record(home, "echo", installed_version)
    _write(
        home,
        "update-check.json",
        {
            "checked_at": _stamp(),
            "ref": REF,
            "commit": COMMIT,
            "updates": [{"package": "echo", "installed": "0.1.0", "available": "0.2.0"}],
        },
    )


def test_the_notice_is_one_stderr_line_at_a_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry(monkeypatch)
    _checking(monkeypatch)
    _stored_update(_home())
    monkeypatch.setattr(updates, "on_tty", lambda: True)

    assert main(["list", "--available"]) == 0

    captured = capsys.readouterr()
    assert captured.err == NOTICE
    assert captured.out.startswith("text:\n  echo  light  0.1.0  installed 0.1.0\n")


def test_the_notice_travels_in_the_json_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry(monkeypatch)
    _checking(monkeypatch)
    _stored_update(_home())

    assert main(["list", "--json"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    envelope = json.loads(captured.out)
    assert envelope["notices"] == [
        {"kind": "update", "package": "echo", "installed": "0.1.0", "available": "0.2.0"}
    ]


def test_nothing_is_printed_without_a_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry(monkeypatch)
    _checking(monkeypatch)
    _stored_update(_home())
    monkeypatch.setattr(updates, "on_tty", lambda: False)

    assert main(["list"]) == 0

    assert capsys.readouterr().err == ""


def test_an_installed_version_that_reached_the_update_is_dropped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry(monkeypatch)
    _checking(monkeypatch)
    _stored_update(_home(), installed_version="0.2.0")

    assert updates.pending_notices(_home()) == []
    assert main(["list", "--json"]) == 0
    assert "notices" not in json.loads(capsys.readouterr().out)


def test_the_variable_disables_the_check_and_the_notice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry(monkeypatch)
    _stored_update(_home())
    monkeypatch.setenv("FACET_NO_UPDATE_CHECK", "1")
    calls: list[str] = []
    _fake_urlopen(monkeypatch, calls)

    assert main(["list", "--json"]) == 0
    monkeypatch.setattr(updates, "on_tty", lambda: True)
    assert main(["list"]) == 0

    captured = capsys.readouterr()
    assert "notices" not in json.loads(captured.out.splitlines()[0])
    assert captured.err == ""
    assert calls == []


def test_notice_line_names_every_update() -> None:
    notices = [
        {"package": "comfy", "installed": "0.1.0", "available": "0.2.0"},
        {"package": "moss", "installed": "0.1.0", "available": "0.1.1"},
    ]

    assert updates.notice_line(notices) == (
        "updates: comfy 0.1.0 → 0.2.0, moss 0.1.0 → 0.1.1"
        " — run facet update, then facet pull --package <name>"
    )


def test_on_tty_follows_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Terminal:
        def isatty(self) -> bool:
            return True

    assert updates.on_tty() is False
    monkeypatch.setattr(sys, "stderr", _Terminal())
    assert updates.on_tty() is True
