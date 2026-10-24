"""``facet check``: machine readiness before pull, re-verification after run."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from facetmedia import check as check_module
from facetmedia import cli as cli_module
from facetmedia import record as record_module
from facetmedia.cli import main
from facetmedia.contract import FacetError
from facetmedia.home import Home
from facetmedia.record import RecordSpec
from facetmedia.registry import load_registry

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
EXIT_INVALID_INPUT = 2
EXIT_VERIFICATION_FAILED = 3
EXIT_LICENSE_NOT_ACCEPTED = 4
EXIT_MACHINE_CANNOT = 5
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


def test_check_fresh_home_reports_needs_pull(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FACET_HOME", str(tmp_path / "fresh-home"))
    _registry(monkeypatch)

    code = main(["check", "echo.text", "--json"])
    data = _data(_envelope(capsys))
    assert code == 0
    assert data["capability"] == "echo.text"
    assert data["can_run"] is True
    assert data["needs_pull"] is True
    assert set(data["checks"]) == {
        "compatible",
        "platform",
        "ram",
        "disk",
        "gpu",
        "host_libs",
        "installed",
        "licenses",
        "models",
        "probe",
    }
    assert data["checks"]["installed"] == {
        "ok": False,
        "detail": data["checks"]["installed"]["detail"],
    }
    assert data["checks"]["probe"]["ok"] is True
    assert "not installed" in str(data["checks"]["probe"]["detail"])


def test_check_after_pull_needs_nothing(
    capsys: pytest.CaptureFixture[str], facet_home: Path
) -> None:
    assert facet_home.is_dir()

    code = main(["check", "echo.text", "--json"])
    data = _data(_envelope(capsys))
    assert code == 0
    assert data["can_run"] is True
    assert data["needs_pull"] is False
    assert data["checks"]["installed"]["ok"] is True
    assert data["checks"]["probe"]["ok"] is True


def test_check_low_ram_machine_fails_with_exit_5(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    facet_home: Path,
) -> None:
    assert facet_home.is_dir()
    monkeypatch.setattr(check_module, "mem_total_gb", lambda: 0.05)

    code = main(["check", "echo.text", "--json"])
    data = _data(_envelope(capsys))
    assert code == EXIT_MACHINE_CANNOT
    assert data["can_run"] is False
    assert data["checks"]["ram"]["ok"] is False


def test_check_incompatible_package_fails_with_exit_5(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FACET_HOME", str(tmp_path / "fresh-home"))
    _registry(monkeypatch)

    code = main(["check", "future.thing", "--json"])
    data = _data(_envelope(capsys))
    assert code == EXIT_MACHINE_CANNOT
    assert data["can_run"] is False
    assert data["checks"]["compatible"]["ok"] is False


def test_check_unsupported_platform_fails_with_exit_5(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FACET_HOME", str(tmp_path / "fresh-home"))
    _registry(monkeypatch)
    monkeypatch.setattr(check_module.sys, "platform", "darwin")

    code = main(["check", "echo.text", "--json"])
    data = _data(_envelope(capsys))
    assert code == EXIT_MACHINE_CANNOT
    assert data["can_run"] is False
    assert data["checks"]["platform"]["ok"] is False
    assert "linux" in str(data["checks"]["platform"]["detail"])


def test_check_insufficient_disk_fails_with_exit_5(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FACET_HOME", str(tmp_path / "fresh-home"))
    _registry(monkeypatch)
    monkeypatch.setattr(check_module.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))

    code = main(["check", "echo.text", "--json"])
    data = _data(_envelope(capsys))
    assert code == EXIT_MACHINE_CANNOT
    assert data["can_run"] is False
    assert data["checks"]["disk"]["ok"] is False


def test_check_missing_model_reports_needs_pull(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FACET_HOME", str(tmp_path / "fresh-home"))
    _registry(monkeypatch)
    assert main(["license", "accept", "termsy-weights", "--yes", "--json"]) == 0
    _envelope(capsys)

    code = main(["check", "termsy.make", "--json"])
    data = _data(_envelope(capsys))
    assert code == 0
    assert data["can_run"] is True
    assert data["needs_pull"] is True
    assert data["checks"]["licenses"] == {
        "ok": True,
        "detail": "termsy-weights (CC-BY-NC-4.0): accepted",
    }
    assert data["checks"]["models"]["ok"] is False


def _piece(directory: Path) -> Path:
    piece = directory / "piece.toml"
    piece.write_text('[inputs]\ntext = "hello"\nrepeat = 2\n', encoding="utf-8")
    return piece


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

    code = main(["check", "echo.text", str(asset), "--json"])
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

    code = main(["check", "echo.text", str(asset), "--json"])
    error = _error(_envelope(capsys))
    assert code == EXIT_VERIFICATION_FAILED
    assert error["code"] == "verification_failed"


def test_reverify_unknown_capability_is_invalid_input(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, facet_home: Path
) -> None:
    assert facet_home.is_dir()
    asset = tmp_path / "echo.txt"
    asset.write_text("hello", encoding="utf-8")

    code = main(["check", "nope.thing", str(asset), "--json"])
    error = _error(_envelope(capsys))
    assert code == EXIT_INVALID_INPUT
    assert error["code"] == "invalid_input"


def test_reverify_missing_record_is_invalid_input(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, facet_home: Path
) -> None:
    assert facet_home.is_dir()
    asset = tmp_path / "lonely.txt"
    asset.write_text("hello", encoding="utf-8")

    code = main(["check", "echo.text", str(asset), "--json"])
    error = _error(_envelope(capsys))
    assert code == EXIT_INVALID_INPUT
    assert error["code"] == "invalid_input"


def test_machine_checks_report_every_check_in_order(facet_home: Path) -> None:
    home = Home.from_env()
    package = check_module.resolve("echo.text")[1]
    checks, failures = check_module.machine_checks(package, home, "test-run")
    assert list(checks) == list(check_module.CHECK_ORDER)
    assert failures == []
    assert all(set(entry) == {"ok", "detail"} for entry in checks.values())


def test_license_gate_maps_to_exit_4(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FACET_HOME", str(tmp_path / "fresh-home"))
    _registry(monkeypatch)
    assert check_module.resolve("termsy.make")[1].name == "termsy"

    code = main(["check", "termsy.make", "--json"])
    data = _data(_envelope(capsys))
    assert code == EXIT_LICENSE_NOT_ACCEPTED
    assert data["can_run"] is False
    assert data["checks"]["licenses"]["ok"] is False
    assert data["checks"]["licenses"]["detail"] == ("termsy-weights (CC-BY-NC-4.0): not accepted")


def test_check_unreadable_meminfo_fails_with_exit_5(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FACET_HOME", str(tmp_path / "fresh-home"))
    _registry(monkeypatch)
    monkeypatch.setattr(check_module, "mem_total_gb", lambda: None)

    code = main(["check", "echo.text", "--json"])
    data = _data(_envelope(capsys))
    assert code == EXIT_MACHINE_CANNOT
    assert data["can_run"] is False
    assert data["checks"]["ram"]["ok"] is False
    assert "MemTotal" in str(data["checks"]["ram"]["detail"])


def test_check_missing_host_lib_fails_with_the_manifest_hint(tmp_path: Path) -> None:
    package = load_registry(FIXTURES).packages["echo"]
    requirements = dataclasses.replace(
        package.requirements,
        host_libs=("libfacet_fixture_missing.so",),
        host_hint="install libfacet_fixture_missing",
    )
    home = Home.from_env()
    checks, failures = check_module.machine_checks(
        dataclasses.replace(package, requirements=requirements), home, "test-run"
    )
    assert checks["host_libs"]["ok"] is False
    assert "libfacet_fixture_missing.so" in str(checks["host_libs"]["detail"])
    assert failures[0].code == "machine_cannot"
    assert failures[0].hint == "install libfacet_fixture_missing"


def test_check_gpu_without_nvidia_reports_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = load_registry(FIXTURES).packages["echo"]
    monkeypatch.setattr(check_module, "gpu_info", lambda: None)
    checks, failures = check_module.machine_checks(package, Home.from_env(), "test-run")
    assert checks["gpu"] == {"ok": True, "detail": "none"}
    assert failures == []


def test_check_required_gpu_without_nvidia_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = load_registry(FIXTURES).packages["echo"]
    requirements = dataclasses.replace(package.requirements, gpu=True)
    monkeypatch.setattr(check_module, "gpu_info", lambda: None)
    checks, failures = check_module.machine_checks(
        dataclasses.replace(package, requirements=requirements), Home.from_env(), "test-run"
    )
    assert checks["gpu"]["ok"] is False
    assert failures[0].code == "machine_cannot"


def test_check_probe_failure_is_machine_cannot(
    monkeypatch: pytest.MonkeyPatch, facet_home: Path
) -> None:
    assert facet_home.is_dir()

    def _boom(*args: object, **kwargs: object) -> object:
        raise FacetError("runner_failed", "boom")

    monkeypatch.setattr(check_module.runner, "call", _boom)
    home = Home.from_env()
    package = check_module.resolve("echo.text")[1]
    checks, failures = check_module.machine_checks(package, home, "test-run")
    assert checks["probe"]["ok"] is False
    assert failures[0].code == "machine_cannot"


def test_reverify_capability_mismatch_is_invalid_input(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, facet_home: Path
) -> None:
    assert facet_home.is_dir()
    out = tmp_path / "out"
    code = main(["run", "echo.text", str(_piece(tmp_path)), "--out", str(out), "--json"])
    assert code == 0
    _envelope(capsys)

    code = main(["check", "termsy.make", str(out / "echo.txt"), "--json"])
    error = _error(_envelope(capsys))
    assert code == EXIT_INVALID_INPUT
    assert error["code"] == "invalid_input"


def _write_echo_record(asset: Path) -> None:
    spec = RecordSpec(
        name="echo",
        kind="text",
        mime="text/plain",
        facet="0.1.0.dev0",
        capability="echo.text",
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


def test_check_machine_form_takes_no_lock(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, facet_home: Path
) -> None:
    assert facet_home.is_dir()
    calls: list[tuple[str, str, bool, float]] = []
    monkeypatch.setattr(cli_module.lock, "hold", _spy_hold(calls))

    assert main(["check", "echo.text", "--json"]) == 0
    _envelope(capsys)
    assert calls == []


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

    code = main(["check", "echo.text", str(asset), "--json"])
    assert code == 0
    _envelope(capsys)
    assert calls == [("shared", "facet check", True, 3600.0)]


def test_check_asset_takes_an_exclusive_lock_for_an_exclusive_package(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    facet_home: Path,
) -> None:
    assert facet_home.is_dir()
    calls: list[tuple[str, str, bool, float]] = []
    monkeypatch.setattr(cli_module.lock, "hold", _spy_hold(calls))

    code = main(["check", "termsy.make", str(tmp_path / "missing.txt"), "--json"])
    assert code == EXIT_INVALID_INPUT
    _error(_envelope(capsys))
    assert calls == [("exclusive", "facet check", True, 3600.0)]


def test_reverify_without_an_installed_environment_is_not_installed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FACET_HOME", str(tmp_path / "fresh-home"))
    _registry(monkeypatch)
    asset = tmp_path / "echo.txt"
    _write_echo_record(asset)

    code = main(["check", "echo.text", str(asset), "--json"])
    error = _error(_envelope(capsys))
    assert code == EXIT_NOT_INSTALLED
    assert error["code"] == "not_installed"


def test_reverify_record_without_provenance_is_invalid_input(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, facet_home: Path
) -> None:
    assert facet_home.is_dir()
    asset = tmp_path / "echo.txt"
    asset.write_text("hellohello", encoding="utf-8")
    (tmp_path / "echo.txt.facet.json").write_text('{"record": 1}', encoding="utf-8")

    code = main(["check", "echo.text", str(asset), "--json"])
    error = _error(_envelope(capsys))
    assert code == EXIT_INVALID_INPUT
    assert error["code"] == "invalid_input"
