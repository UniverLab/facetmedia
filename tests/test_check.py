"""``facet check``: re-verification of an asset against its record.

Readiness moved to ``test_doctor.py`` with the command it now belongs to.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from facetmedia import cli as cli_module
from facetmedia import record as record_module
from facetmedia.cli import main
from facetmedia.record import RecordSpec

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
EXIT_INVALID_INPUT = 2
EXIT_VERIFICATION_FAILED = 3
EXIT_NOT_INSTALLED = 7


def _registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))


def _envelope(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    """The single JSON document facet printed on stdout."""
    parsed: Any = json.loads(capsys.readouterr().out)
    assert isinstance(parsed, dict)
    return parsed


def _data(envelope: dict[str, Any]) -> dict[str, Any]:
    assert envelope["ok"] is True
    data = envelope["data"]
    assert isinstance(data, dict)
    return data


def _error(envelope: dict[str, Any]) -> dict[str, Any]:
    assert envelope["ok"] is False
    error: dict[str, Any] = envelope["error"]
    return error


@pytest.fixture(scope="module")
def seed_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One home with echo installed; shared by every test in this module."""
    root = tmp_path_factory.mktemp("facet-home-seed")
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
def facet_home(seed_home: Path, monkeypatch: pytest.MonkeyPatch, _facet_home_tmp: None) -> Path:
    """The shared home, after the per-test temp home has been set and replaced."""
    monkeypatch.setenv("FACET_HOME", str(seed_home))
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))
    return seed_home


def _piece(directory: Path) -> Path:
    piece = directory / "piece.toml"
    piece.write_text('[inputs]\ntext = "hello"\nrepeat = 2\n', encoding="utf-8")
    return piece


def _write_echo_record(asset: Path, capability: str = "echo.text") -> None:
    spec = RecordSpec(
        name="echo",
        kind="text",
        mime="text/plain",
        facet="0.1.0b1",
        capability=capability,
        package={"name": "echo", "version": "0.1.0", "lock_sha256": "abc"},
        tool={"name": "echo", "version": "0.1.0"},
        params={"text": "hello", "repeat": 2},
    )
    asset.write_text("hellohello", encoding="utf-8")
    record_module.write_record(asset, record_module.build_record(asset, spec))


def _spy_hold(calls: list[tuple[str, str, bool, float]]) -> Any:
    """A ``lock.hold`` that records its arguments and then takes the real lock."""
    real = cli_module.lock.hold

    @contextlib.contextmanager
    def hold(mode: str, purpose: str, wait: bool = True, timeout_s: float = 3600.0) -> Any:
        calls.append((mode, purpose, wait, timeout_s))
        with real(mode, purpose, wait, timeout_s):
            yield

    return hold


def test_reverify_untouched_asset_passes(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, facet_home: Path
) -> None:
    assert facet_home.is_dir()
    out = tmp_path / "out"
    code = main(["run", "echo.text", str(_piece(tmp_path)), "--out", str(out), "--json"])
    assert code == 0
    _envelope(capsys)
    asset = out / "echo.txt"
    before = (out / "echo.txt.facet.json").read_bytes()

    code = main(["check", str(asset), "--json"])
    data = _data(_envelope(capsys))
    assert code == 0
    assert data["capability"] == "echo.text"
    assert data["passed"] is True
    assert data["fresh"]["metrics"] == {"match": 1}
    assert data["fresh"]["metrics"] == data["recorded"]["metrics"]
    # The record file itself is untouched by the re-verification.
    record_path = out / "echo.txt.facet.json"
    assert record_path.is_file()
    assert record_path.read_bytes() == before


def test_reverify_from_the_record_file_itself(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, facet_home: Path
) -> None:
    """The record path names the same asset as the asset path does."""
    out = tmp_path / "out"
    assert main(["run", "echo.text", str(_piece(tmp_path)), "--out", str(out), "--json"]) == 0
    _envelope(capsys)

    code = main(["check", str(out / "echo.txt.facet.json"), "--json"])
    data = _data(_envelope(capsys))
    assert code == 0
    assert data["asset"] == str(out / "echo.txt")
    assert data["passed"] is True


def test_reverify_edited_bytes_fails_with_exit_3(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, facet_home: Path
) -> None:
    assert facet_home.is_dir()
    out = tmp_path / "out"
    code = main(["run", "echo.text", str(_piece(tmp_path)), "--out", str(out), "--json"])
    assert code == 0
    _envelope(capsys)
    asset = out / "echo.txt"
    with asset.open("ab") as handle:
        handle.write(b"x")

    code = main(["check", str(asset), "--json"])
    error = _error(_envelope(capsys))
    assert code == EXIT_VERIFICATION_FAILED
    assert error["code"] == "verification_failed"


def test_reverify_missing_record_is_invalid_input(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, facet_home: Path
) -> None:
    assert facet_home.is_dir()
    asset = tmp_path / "lonely.txt"
    asset.write_text("hello", encoding="utf-8")

    code = main(["check", str(asset), "--json"])
    error = _error(_envelope(capsys))
    assert code == EXIT_INVALID_INPUT
    assert error["code"] == "invalid_input"


def test_check_without_an_existing_file_is_invalid_input(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, facet_home: Path
) -> None:
    assert facet_home.is_dir()

    code = main(["check", str(tmp_path / "nope.txt"), "--json"])
    error = _error(_envelope(capsys))
    assert code == EXIT_INVALID_INPUT
    assert error["code"] == "invalid_input"
    assert "no such asset file" in error["message"]


def test_check_a_capability_id_points_at_doctor(
    capsys: pytest.CaptureFixture[str], facet_home: Path
) -> None:
    """The old `<capability-id>` form fails and names the new command."""
    assert facet_home.is_dir()

    code = main(["check", "echo.text", "--json"])
    error = _error(_envelope(capsys))
    assert code == EXIT_INVALID_INPUT
    assert error["code"] == "invalid_input"
    assert error["hint"] == "facet doctor echo.text"


def test_check_unknown_argument_is_invalid_input(
    capsys: pytest.CaptureFixture[str], facet_home: Path
) -> None:
    assert facet_home.is_dir()

    code = main(["check", "nope.thing", "--json"])
    error = _error(_envelope(capsys))
    assert code == EXIT_INVALID_INPUT
    assert error["code"] == "invalid_input"
    assert error["hint"] is None


def test_reverify_record_without_provenance_is_invalid_input(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, facet_home: Path
) -> None:
    assert facet_home.is_dir()
    asset = tmp_path / "echo.txt"
    asset.write_text("hellohello", encoding="utf-8")
    (tmp_path / "echo.txt.facet.json").write_text('{"record": 1}', encoding="utf-8")

    code = main(["check", str(asset), "--json"])
    error = _error(_envelope(capsys))
    assert code == EXIT_INVALID_INPUT
    assert error["code"] == "invalid_input"


def test_reverify_without_an_installed_environment_is_not_installed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FACET_HOME", str(tmp_path / "fresh-home"))
    _registry(monkeypatch)
    asset = tmp_path / "echo.txt"
    _write_echo_record(asset)

    code = main(["check", str(asset), "--json"])
    error = _error(_envelope(capsys))
    assert code == EXIT_NOT_INSTALLED
    assert error["code"] == "not_installed"


def test_check_asset_takes_a_shared_lock_for_a_light_package(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    facet_home: Path,
) -> None:
    assert facet_home.is_dir()
    calls: list[tuple[str, str, bool, float]] = []
    monkeypatch.setattr(cli_module.lock, "hold", _spy_hold(calls))
    asset = tmp_path / "echo.txt"
    _write_echo_record(asset)

    code = main(["check", str(asset), "--json"])
    assert code == 0
    _envelope(capsys)
    assert calls == [("shared", "facet check", True, 3600.0)]


def test_check_asset_takes_an_exclusive_lock_and_reports_not_installed(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    facet_home: Path,
) -> None:
    """An exclusive package's record holds the lock, then fails not_installed."""
    assert facet_home.is_dir()
    calls: list[tuple[str, str, bool, float]] = []
    monkeypatch.setattr(cli_module.lock, "hold", _spy_hold(calls))
    asset = tmp_path / "termsy.txt"
    _write_echo_record(asset, capability="termsy.make")

    code = main(["check", str(asset), "--json"])
    error = _error(_envelope(capsys))
    assert code == EXIT_NOT_INSTALLED
    assert error["code"] == "not_installed"
    assert calls == [("exclusive", "facet check", True, 3600.0)]
