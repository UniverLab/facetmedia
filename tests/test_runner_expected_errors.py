"""Expected runner failures print one line, not a traceback."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from facetmedia import runner_sdk


def _request_file(tmp_path: Path, package: str | None) -> Path:
    data: dict[str, Any] = {
        "protocol": 1,
        "verb": "run",
        "capability": "echo.text",
        "inputs": {"text": "hi"},
        "out_dir": str(tmp_path / "out"),
        "piece_dir": str(tmp_path),
        "models": {},
        "assets": [],
    }
    if package is not None:
        data["package"] = package
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps(data), encoding="utf-8")
    return request_file


def _run(sdk: runner_sdk.Runner, tmp_path: Path, package: str | None) -> int:
    request_file = _request_file(tmp_path, package)
    return sdk.run(["run", "--input", str(request_file), "--result", str(tmp_path / "result.json")])


def test_value_error_prints_one_line_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sdk = runner_sdk.Runner()

    @sdk.verb("run")
    def handle(_req: runner_sdk.Request) -> dict[str, Any]:
        raise ValueError("bad input")

    assert _run(sdk, tmp_path, "echo") == 1
    err = capsys.readouterr().err
    assert "error: bad input" in err
    assert "Traceback" not in err


def test_unset_secret_names_the_fix_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FACET_SECRET_CLIENT_ID", raising=False)
    sdk = runner_sdk.Runner()

    @sdk.verb("run")
    def handle(_req: runner_sdk.Request) -> dict[str, Any]:
        return {"token": runner_sdk.secret("client-id")}

    assert _run(sdk, tmp_path, "linkedin") == 1
    err = capsys.readouterr().err
    assert "error: secret client-id is not set; run facet secret set linkedin client-id" in err
    assert "Traceback" not in err
    assert runner_sdk._current_package == ""


def test_secret_not_set_is_a_key_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FACET_SECRET_CLIENT_ID", raising=False)
    with pytest.raises(KeyError, match="client-id"):
        runner_sdk.secret("client-id")
    assert issubclass(runner_sdk.SecretNotSet, KeyError)


def test_plain_key_error_keeps_the_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sdk = runner_sdk.Runner()

    @sdk.verb("run")
    def handle(_req: runner_sdk.Request) -> dict[str, Any]:
        raise KeyError("something else")

    assert _run(sdk, tmp_path, "echo") == 1
    err = capsys.readouterr().err
    assert "Traceback" in err
    assert "something else" in err


def test_runtime_error_keeps_the_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sdk = runner_sdk.Runner()

    @sdk.verb("run")
    def handle(_req: runner_sdk.Request) -> dict[str, Any]:
        raise RuntimeError("kablam")

    assert _run(sdk, tmp_path, "echo") == 1
    err = capsys.readouterr().err
    assert "Traceback" in err
    assert "kablam" in err


def test_request_without_package_keeps_the_old_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FACET_SECRET_CLIENT_ID", raising=False)
    sdk = runner_sdk.Runner()

    @sdk.verb("run")
    def handle(_req: runner_sdk.Request) -> dict[str, Any]:
        return {"token": runner_sdk.secret("client-id")}

    assert _run(sdk, tmp_path, None) == 1
    err = capsys.readouterr().err
    assert "error: secret client-id is not set; run facet secret set or facet auth" in err
    assert "Traceback" not in err
