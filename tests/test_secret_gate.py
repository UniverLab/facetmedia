"""Tests for secret-row filtering in pull/run gates vs. ``facet doctor``."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from facetmedia import doctor as doctor_module
from facetmedia import plan as plan_module
from facetmedia import run as run_module
from facetmedia.cli import main
from facetmedia.home import Home
from facetmedia.registry import load_registry
from facetmedia.registry_types import Secret

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
EXIT_MACHINE_CANNOT = 5

_SECRET_ONLY_DOCTOR = """
from facet_runner_sdk import Request, Runner

runner = Runner()


@runner.verb("doctor")
def doctor(req: Request) -> dict[str, object]:
    return {
        "checks": [
            {
                "name": "access-token",
                "ok": False,
                "detail": "access-token is not stored",
                "fix": "facet auth login echo",
            }
        ]
    }


runner.main()
"""

_SECRET_AND_OTHER_DOCTOR = """
from facet_runner_sdk import Request, Runner

runner = Runner()


@runner.verb("doctor")
def doctor(req: Request) -> dict[str, object]:
    return {
        "checks": [
            {
                "name": "access-token",
                "ok": False,
                "detail": "access-token is not stored",
                "fix": "facet auth login echo",
            },
            {
                "name": "binary",
                "ok": False,
                "detail": "missing binary",
                "fix": "install binary",
            },
        ]
    }


runner.main()
"""


@pytest.fixture(scope="module")
def seed_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One home with echo installed; shared by every test in this module."""
    root = tmp_path_factory.mktemp("facet-secret-gate-seed")
    previous = {name: os.environ.get(name) for name in ("FACET_HOME", "FACET_REGISTRY")}
    os.environ["FACET_HOME"] = str(root)
    os.environ["FACET_REGISTRY"] = str(FIXTURES)
    try:
        assert main(["pull", "echo", "--json"]) == 0
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return root


@pytest.fixture
def facet_home(seed_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point FACET_HOME and FACET_REGISTRY at the seeded home."""
    monkeypatch.setenv("FACET_HOME", str(seed_home))
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))
    return seed_home


@pytest.fixture
def swap_echo_runner(facet_home: Path) -> Iterator[Callable[[str], None]]:
    """Replace the installed echo runner for one test, then restore it."""
    target = facet_home / "envs" / "echo" / "0.1.0" / "package" / "runner.py"
    original = target.read_text(encoding="utf-8")

    def swap(source: str) -> None:
        target.write_text(source, encoding="utf-8")

    yield swap
    target.write_text(original, encoding="utf-8")


def _attach_echo_secrets(monkeypatch: pytest.MonkeyPatch, *secrets_list: Secret) -> None:
    registry = load_registry(FIXTURES)
    echo = dataclasses.replace(registry.package("echo"), secrets=secrets_list)
    patched = dataclasses.replace(registry, packages={**registry.packages, "echo": echo})
    monkeypatch.setattr(plan_module, "load_active_registry", lambda: patched)
    monkeypatch.setattr(run_module, "load_active_registry", lambda: patched)
    monkeypatch.setattr(doctor_module, "load_registry", lambda _path: patched)


def test_pull_succeeds_and_prints_signin_when_only_secrets_fail(
    facet_home: Path,
    swap_echo_runner: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _attach_echo_secrets(
        monkeypatch,
        Secret(name="access-token", description="API token", required=True),
    )
    swap_echo_runner(_SECRET_ONLY_DOCTOR)

    code = main(["pull", "--verbose", "echo"])

    out = capsys.readouterr().out
    assert code == 0
    assert "echo 0.1.0" in out
    assert "already installed" in out
    assert "sign in with facet auth login echo" in out


def test_pull_still_fails_when_non_secret_doctor_row_fails(
    facet_home: Path,
    swap_echo_runner: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _attach_echo_secrets(
        monkeypatch,
        Secret(name="access-token", description="API token", required=True),
    )
    swap_echo_runner(_SECRET_AND_OTHER_DOCTOR)

    code = main(["pull", "echo", "--json"])

    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert code == EXIT_MACHINE_CANNOT
    assert payload["ok"] is False
    assert payload["error"]["code"] == "machine_cannot"
    assert "binary" in payload["error"]["message"]
    assert "access-token" not in payload["error"]["message"]


def test_doctor_still_lists_failing_secret_rows(
    facet_home: Path,
    swap_echo_runner: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _attach_echo_secrets(
        monkeypatch,
        Secret(name="access-token", description="API token", required=True),
    )
    swap_echo_runner(_SECRET_ONLY_DOCTOR)

    code = main(["doctor", "echo", "--json"])

    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert code == EXIT_MACHINE_CANNOT
    checks = payload["data"]["packages"]["echo"]["doctor"]
    secret_rows = [row for row in checks if row["name"] == "access-token"]
    assert len(secret_rows) == 1
    assert secret_rows[0]["ok"] is False
    assert "is not stored" in secret_rows[0]["detail"]


def test_planned_pull_noop_entry_prints_signin_hint(
    facet_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    echo = dataclasses.replace(
        load_registry(FIXTURES).package("echo"),
        secrets=(Secret(name="access-token", description="API token", required=True),),
    )
    args = argparse.Namespace(json=False, verify_models=False, run_id="")
    code = plan_module._install_all(
        args,
        Home.from_env(),
        [plan_module.PlanEntry(package=echo, install=True, reason="fits", noop=True)],
        None,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "sign in with facet auth login echo" in out


def test_run_still_refuses_missing_required_secret(
    facet_home: Path,
    swap_echo_runner: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _attach_echo_secrets(
        monkeypatch,
        Secret(name="access-token", description="API token", required=True),
    )
    swap_echo_runner(_SECRET_ONLY_DOCTOR)
    piece = tmp_path / "piece.toml"
    piece.write_text('[inputs]\ntext = "hi"\nrepeat = 1\n', encoding="utf-8")

    code = main(["run", "echo.text", str(piece), "--json"])

    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert code == EXIT_MACHINE_CANNOT
    assert payload["ok"] is False
    assert payload["error"]["code"] == "machine_cannot"
    assert "access-token" in payload["error"]["message"]
