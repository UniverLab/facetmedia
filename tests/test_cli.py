import json
import subprocess
import sys

import pytest

from facetmedia import __version__
from facetmedia.cli import build_parser, main

EXIT_USAGE = 2

UNIMPLEMENTED_ARGV = [
    ["update"],
    ["pull"],
    ["list"],
    ["describe"],
    ["check"],
    ["run"],
    ["preview"],
    ["license", "accept"],
    ["license", "list"],
    ["gc"],
    ["rm"],
]


def test_version_prints_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


@pytest.mark.parametrize("argv", UNIMPLEMENTED_ARGV, ids=" ".join)
def test_every_verb_parses_and_reports_not_implemented(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([*argv, "--json"]) == 1
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "internal"
    assert envelope["error"]["message"] == "not implemented yet"


@pytest.mark.parametrize("argv", UNIMPLEMENTED_ARGV, ids=" ".join)
def test_every_verb_human_error_goes_to_stderr(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "not implemented yet\n"


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
