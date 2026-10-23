"""``facet pull`` end to end: real uv, the fixture registry, a temp home.

These tests run the real ``uv sync``, so the first environment of each test
home downloads the managed CPython interpreter (network needed).  The model
fixtures are ``file://`` paths and are never fetched from the network.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from facetmedia import env as env_module
from facetmedia.cli import main
from facetmedia.home import Home
from facetmedia.registry import load_registry

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
REAL_SHA = "85a73737b5d85eaba43e73ba33ceade28de49f1f4f7573377f02d58408f715c7"
TERMSY_BYTES = 21
EXIT_LICENSE_NOT_ACCEPTED = 4
EXIT_DOWNLOAD_FAILED = 8
EXIT_MACHINE_CANNOT = 5
GB = 1024**3


def _pull(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, object]]:
    """Run ``facet <argv> --json`` and return (exit code, envelope)."""
    code = main([*argv, "--json"])
    envelope = json.loads(capsys.readouterr().out)
    assert isinstance(envelope, dict)
    return code, envelope


def _data(envelope: dict[str, object]) -> dict[str, object]:
    data = envelope.get("data")
    assert isinstance(data, dict)
    return data


def _registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))


def _accept_termsy(home: Home) -> None:
    """The acceptance record ``facet license accept`` will write (feat(license))."""
    record = {"model": "termsy-weights", "spdx": "CC-BY-NC-4.0"}
    home.licenses_file.write_text(
        json.dumps({"version": 1, "accepted": [record]}),
        encoding="utf-8",
    )


def test_pull_echo_installs_then_reports_already_installed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    home = Home.from_env()
    package = load_registry(FIXTURES).packages["echo"]
    env_dir = home.env_dir("echo", "0.1.0")

    code, envelope = _pull(capsys, "pull", "echo")
    assert code == 0
    data = _data(envelope)
    assert data["already_installed"] is False
    assert data["env"] == str(env_dir)
    assert data["python"] == "3.12"
    assert data["downloaded_bytes"] == 0
    assert data["models"] == []
    assert (env_dir / ".complete").read_text(encoding="utf-8") == package.lock_sha256
    run = subprocess.run(
        [str(env_dir / "venv" / "bin" / "python"), "-c", "import sys; print(sys.version_info[:2])"],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert run.returncode == 0
    assert run.stdout.strip() == "(3, 12)"
    # runner_sdk.py ships with feat(run); without it the pull still succeeds.
    assert list((env_dir / "venv").rglob("facet_runner_sdk.py")) == []

    code, envelope = _pull(capsys, "pull", "echo")
    assert code == 0
    second = _data(envelope)
    assert second["already_installed"] is True
    assert second["downloaded_bytes"] == 0
    assert second["env"] == str(env_dir)


def test_pull_echo_rebuilds_when_the_complete_marker_is_gone(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _registry(monkeypatch)
    home = Home.from_env()
    env_dir = home.env_dir("echo", "0.1.0")
    _pull(capsys, "pull", "echo")
    (env_dir / "stale-marker").write_text("left by an interrupted pull", encoding="utf-8")
    (env_dir / ".complete").unlink()
    sdk = tmp_path / "runner_sdk.py"
    sdk.write_text("# runner SDK\n", encoding="utf-8")
    monkeypatch.setattr(env_module, "RUNNER_SDK", sdk)

    code, envelope = _pull(capsys, "pull", "echo")
    assert code == 0
    data = _data(envelope)
    assert data["already_installed"] is False
    assert not (env_dir / "stale-marker").exists()
    copied = [
        path.read_text(encoding="utf-8") for path in (env_dir / "venv").rglob("facet_runner_sdk.py")
    ]
    assert copied == ["# runner SDK\n"]


def test_termsy_without_acceptance_downloads_nothing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    home = Home.from_env()

    code, envelope = _pull(capsys, "pull", "termsy")
    assert code == EXIT_LICENSE_NOT_ACCEPTED
    error = envelope["error"]
    assert isinstance(error, dict)
    assert error["code"] == "license_not_accepted"
    assert error["details"]["models"] == [
        {
            "id": "termsy-weights",
            "spdx": "CC-BY-NC-4.0",
            "terms_url": "https://example.invalid/termsy",
            "command": "facet license accept termsy-weights",
        }
    ]
    assert list((home.models_dir / "direct").iterdir()) == []
    assert list(home.tmp_dir.iterdir()) == []
    assert not home.env_dir("termsy", "0.1.0").exists()


def test_termsy_with_acceptance_downloads_the_file_model(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    home = Home.from_env()
    _accept_termsy(home)

    code, envelope = _pull(capsys, "pull", "termsy")
    assert code == 0
    data = _data(envelope)
    assert data["already_installed"] is False
    assert data["downloaded_bytes"] == TERMSY_BYTES
    assert data["models"][0]["status"] == "downloaded"
    target = home.models_dir / "direct" / "termsy-weights" / "termsy.bin"
    assert target.stat().st_size == TERMSY_BYTES

    code, envelope = _pull(capsys, "pull", "termsy")
    assert code == 0
    data = _data(envelope)
    assert data["already_installed"] is True
    assert data["downloaded_bytes"] == 0
    assert data["models"][0]["status"] == "present"


def test_wrong_sha256_fails_and_leaves_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_copy = tmp_path / "registry"
    shutil.copytree(FIXTURES, registry_copy)
    manifest = registry_copy / "packages" / "termsy" / "package.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(REAL_SHA, "0" * 64),
        encoding="utf-8",
    )
    monkeypatch.setenv("FACET_REGISTRY", str(registry_copy))
    home = Home.from_env()
    _accept_termsy(home)

    code, envelope = _pull(capsys, "pull", "termsy")
    assert code == EXIT_DOWNLOAD_FAILED
    error = envelope["error"]
    assert isinstance(error, dict)
    assert error["code"] == "download_failed"
    assert error["details"]["expected"] == "0" * 64
    assert error["details"]["actual"] == REAL_SHA
    assert list((home.models_dir / "direct").rglob("*")) == []
    assert list(home.tmp_dir.iterdir()) == []


def test_insufficient_disk_fails_before_writing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    home = Home.from_env()
    usage = SimpleNamespace(total=10 * GB, used=9 * GB, free=0)
    monkeypatch.setattr(env_module.shutil, "disk_usage", lambda _path: usage)

    code, envelope = _pull(capsys, "pull", "echo")
    assert code == EXIT_MACHINE_CANNOT
    error = envelope["error"]
    assert isinstance(error, dict)
    assert error["code"] == "machine_cannot"
    assert "GB free" in error["message"]
    assert error["details"]["free_bytes"] == 0
    assert error["details"]["need_bytes"] > 0
    assert not home.env_dir("echo", "0.1.0").exists()
