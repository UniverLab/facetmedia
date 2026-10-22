import pytest

from facetmedia.cli import main


def test_main_greets_the_given_name(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--name", "test"]) == 0
    assert capsys.readouterr().out == "Hello from facetmedia, test!\n"
