import argparse
import json

import pytest

from facetmedia import __version__
from facetmedia import cli as cli_module
from facetmedia.cli import main
from facetmedia.contract import (
    CONTRACT_VERSION,
    EXIT_CODES,
    FacetError,
    emit,
    failure,
    ok,
)

EXPECTED_EXIT_MAP = {
    "internal": 1,
    "invalid_input": 2,
    "verification_failed": 3,
    "license_not_accepted": 4,
    "machine_cannot": 5,
    "lock_busy": 6,
    "not_installed": 7,
    "download_failed": 8,
    "runner_failed": 9,
    "license_needs_person": 10,
}


def test_ok_shape() -> None:
    envelope = ok({"tools": []})
    assert envelope == {
        "contract": CONTRACT_VERSION,
        "ok": True,
        "facet": __version__,
        "data": {"tools": []},
    }
    assert CONTRACT_VERSION == 1


def test_failure_shape_with_hint_and_details() -> None:
    err = FacetError("lock_busy", "locked", hint="retry later", details={"owner": 42})
    assert failure(err) == {
        "contract": CONTRACT_VERSION,
        "ok": False,
        "facet": __version__,
        "error": {
            "code": "lock_busy",
            "message": "locked",
            "hint": "retry later",
            "details": {"owner": 42},
        },
    }


def test_failure_shape_defaults() -> None:
    envelope = failure(FacetError("internal", "boom"))
    error = envelope["error"]
    assert isinstance(error, dict)
    assert error["hint"] is None
    assert error["details"] == {}


def test_exit_codes_table_is_exhaustive() -> None:
    assert EXIT_CODES == EXPECTED_EXIT_MAP


@pytest.mark.parametrize("code,exit_code", sorted(EXPECTED_EXIT_MAP.items()))
def test_every_code_maps_to_its_exit_code(code: str, exit_code: int) -> None:
    assert FacetError(code, "m").exit_code == exit_code


def test_emit_json_writes_sorted_ascii_single_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    envelope = ok({"z": "ñ", "a": 1})
    emit(envelope, True)
    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1
    assert captured.out.endswith("\n")
    assert json.loads(captured.out) == envelope
    raw = captured.out[:-1]
    assert raw == json.dumps(envelope, ensure_ascii=False, sort_keys=True)
    positions = [raw.index(f'"{key}"') for key in ("contract", "data", "facet", "ok")]
    assert positions == sorted(positions)
    assert "ñ" in captured.out
    assert captured.err == ""


def test_emit_human_failure_goes_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emit(failure(FacetError("internal", "boom", hint="see log")), False)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "boom" in captured.err
    assert "see log" in captured.err


def test_emit_human_success_is_noop(capsys: pytest.CaptureFixture[str]) -> None:
    emit(ok({}), False)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_main_json_single_doc_on_unimplemented(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["list", "--json"])
    assert code == 1
    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1
    envelope = json.loads(captured.out)
    assert envelope["ok"] is False
    assert envelope["contract"] == 1
    assert envelope["error"]["code"] == "internal"
    assert "not implemented yet" in envelope["error"]["message"]
    assert "contract" not in captured.err


def test_argparse_error_json_envelope(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # sys.argv deliberately lacks --json: the envelope must follow the argv
    # passed to main(), not the process argv (library use).
    monkeypatch.setattr("sys.argv", ["facet"])
    with pytest.raises(SystemExit) as exit_info:
        main(["--json", "nope"])
    assert exit_info.value.code == EXIT_CODES["invalid_input"]
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert captured.out.count("\n") == 1
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "invalid_input"
    assert '"ok"' not in captured.err
    assert '"contract"' not in captured.err


def test_argparse_error_without_json_emits_nothing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["facet"])
    with pytest.raises(SystemExit) as exit_info:
        main(["nope"])
    assert exit_info.value.code == EXIT_CODES["invalid_input"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid" in captured.err.lower() or "usage" in captured.err.lower()


def _run_main_with_handler_error(code: str, monkeypatch: pytest.MonkeyPatch) -> int:
    def _fail(_args: argparse.Namespace) -> int:
        raise FacetError(code, f"msg for {code}")

    parser = cli_module.build_parser()
    monkeypatch.setattr(
        parser,
        "parse_args",
        lambda argv=None: argparse.Namespace(handler=_fail, json=True),
    )
    monkeypatch.setattr(cli_module, "build_parser", lambda: parser)
    return main(["list", "--json"])


@pytest.mark.parametrize("code,exit_code", sorted(EXPECTED_EXIT_MAP.items()))
def test_main_returns_each_exit_code(
    code: str,
    exit_code: int,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _run_main_with_handler_error(code, monkeypatch) == exit_code
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == code


def test_stderr_never_contains_envelope_on_json_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["list", "--json"]) == 1
    captured = capsys.readouterr()
    assert '"ok"' not in captured.err
    assert '"contract"' not in captured.err


def test_unimplemented_is_internal(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["list"])
    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "not implemented yet\n"


def test_unexpected_exception_becomes_internal(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(_args: argparse.Namespace) -> int:
        raise RuntimeError("kablam")

    parser = cli_module.build_parser()
    monkeypatch.setattr(
        parser,
        "parse_args",
        lambda argv=None: argparse.Namespace(handler=_boom, json=True),
    )
    monkeypatch.setattr(cli_module, "build_parser", lambda: parser)
    code = main(["list", "--json"])
    assert code == 1
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "internal"
    assert "internal error, see stderr" in envelope["error"]["message"]


def test_unexpected_exception_human_goes_to_stderr(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(_args: argparse.Namespace) -> int:
        raise RuntimeError("kablam")

    parser = cli_module.build_parser()
    monkeypatch.setattr(
        parser,
        "parse_args",
        lambda argv=None: argparse.Namespace(handler=_boom, json=False),
    )
    monkeypatch.setattr(cli_module, "build_parser", lambda: parser)
    code = main(["list"])
    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "internal error, see stderr" in captured.err
