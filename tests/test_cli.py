import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from facetmedia import __version__
from facetmedia import cli as cli_module
from facetmedia.cli import build_parser, main

EXIT_USAGE = 2
EXIT_MACHINE_CANNOT = 5
DEFAULT_WAIT_TIMEOUT_S = 3600.0
SHORT_TIMEOUT_S = 5.0
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"


def test_version_prints_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["--json", "list"],
        ["list", "--json"],
        ["--json", "license", "list"],
        ["license", "list", "--json"],
    ],
    ids=" ".join,
)
def test_json_flag_accepted_before_and_after_verb(argv: list[str]) -> None:
    assert build_parser().parse_args(argv).json is True


def test_json_flag_defaults_to_false() -> None:
    assert build_parser().parse_args(["list"]).json is False


def test_run_flags_default_to_the_spec() -> None:
    args = build_parser().parse_args(["run", "echo.text", "piece.toml"])
    assert args.capability == "echo.text"
    assert args.piece == "piece.toml"
    assert args.out is None
    assert args.verify is True
    assert args.pull is True
    assert args.ephemeral is False
    assert args.timeout is None
    assert args.wait is True
    assert args.wait_timeout == DEFAULT_WAIT_TIMEOUT_S


def test_run_flags_are_booleans_that_flip() -> None:
    args = build_parser().parse_args(["run", "echo.text", "-", "--ephemeral"])
    assert args.ephemeral is True


def test_rm_and_gc_flags_default_to_the_spec() -> None:
    rm = build_parser().parse_args(["rm", "echo"])
    assert rm.package == "echo"
    assert rm.models is False
    assert rm.wait is True
    gc = build_parser().parse_args(["gc"])
    assert gc.dry_run is False
    assert gc.wait is True


def test_run_no_flags_flip_their_dests() -> None:
    args = build_parser().parse_args(
        ["run", "echo.text", "-", "--no-verify", "--no-pull", "--timeout", "5", "--no-wait"]
    )
    assert args.verify is False
    assert args.pull is False
    assert args.timeout == SHORT_TIMEOUT_S
    assert args.wait is False


def test_no_verb_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main([])
    assert exit_info.value.code == EXIT_USAGE
    assert "usage" in capsys.readouterr().err


def test_python_m_facetmedia_prints_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "facetmedia", "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0
    assert __version__ in result.stdout


def test_unexpected_exception_is_logged_to_run_log(
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
    assert main(["list"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    log_path = Path(captured.err.removeprefix("internal error, see ").strip())
    assert log_path.is_file()
    assert log_path.parent.parent == Path(os.environ["FACET_HOME"]) / "logs"
    content = log_path.read_text(encoding="utf-8")
    assert "Traceback" in content
    assert "kablam" in content


def test_every_invocation_creates_the_home_layout(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))
    assert main(["gc"]) == 0
    capsys.readouterr()
    root = Path(os.environ["FACET_HOME"])
    for directory in (
        root / "registry",
        root / "state",
        root / "envs",
        root / "models" / "direct",
        root / "models" / "hf",
        root / "python",
        root / "cache" / "uv",
        root / "logs",
        root / "tmp",
    ):
        assert directory.is_dir()


def test_main_refuses_native_windows(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    assert main(["list", "--json"]) == EXIT_MACHINE_CANNOT
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "machine_cannot"
    assert "WSL" in envelope["error"]["message"]
    assert not Path(os.environ["FACET_HOME"]).exists()
