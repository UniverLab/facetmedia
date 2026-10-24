"""``facet list`` and ``facet describe``: the whole discovery surface."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from facetmedia import introspect
from facetmedia import schema as schema_module
from facetmedia.cli import main
from facetmedia.home import Home
from facetmedia.registry import load_registry
from facetmedia.registry_types import Capability, Field

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
GOLDEN = Path(__file__).resolve().parent / "golden" / "discover"
EXIT_INVALID_INPUT = 2
EXIT_USAGE = 2
FIXTURE_CAPABILITY_COUNT = 3


def _registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))


def _data(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    """The ``data`` of the single JSON document facet printed on stdout."""
    envelope: Any = json.loads(capsys.readouterr().out)
    assert isinstance(envelope, dict)
    assert envelope["ok"] is True
    data = envelope["data"]
    assert isinstance(data, dict)
    return data


def test_list_available_matches_golden(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    assert main(["list", "--available", "--json"]) == 0
    data = _data(capsys)
    data["registry"]["path"] = "<path>"
    golden: Any = json.loads((GOLDEN / "list-available.json").read_text(encoding="utf-8"))
    assert data == golden


def test_describe_echo_text_matches_golden(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    assert main(["describe", "echo.text", "--json"]) == 0
    golden: Any = json.loads((GOLDEN / "echo-text.json").read_text(encoding="utf-8"))
    assert _data(capsys) == golden


def test_the_future_fixture_is_incompatible(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    assert main(["list", "--available", "--json"]) == 0
    rows = _data(capsys)["packages"]
    by_name = {row["name"]: row for row in rows}
    assert by_name["future"]["compatible"] is False
    assert by_name["echo"]["compatible"] is True
    assert by_name["termsy"]["compatible"] is True


def test_default_list_is_installed_only(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    assert main(["list", "--json"]) == 0
    data = _data(capsys)
    assert data["packages"] == []
    assert data["registry"] == {"path": str(FIXTURES)}


def test_human_list_available_is_three_sorted_lines(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    assert main(["list", "--available"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "echo.text  text  Echo text back, repeated.",
        "future.thing  text  A capability from the future.",
        "termsy.make  text  Make one termsy asset.",
    ]
    assert lines == sorted(lines)


def test_fake_installed_echo_is_listed_alone(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    echo = load_registry(FIXTURES).package("echo")
    assert echo is not None
    env_dir = Home.peek_env().env_dir(echo.name, echo.version)
    env_dir.mkdir(parents=True)
    (env_dir / ".complete").write_text(echo.lock_sha256, encoding="utf-8")
    payload = env_dir / "payload.bin"
    payload.write_bytes(b"1234567")

    assert main(["list", "--json"]) == 0
    rows = _data(capsys)["packages"]
    assert [row["name"] for row in rows] == ["echo"]
    assert rows[0]["installed"] is True
    # env_bytes is tree_bytes(<env>): the 64-byte .complete marker plus the
    # 7-byte payload file.
    assert rows[0]["env_bytes"] == len(echo.lock_sha256) + 7

    assert main(["list"]) == 0
    assert capsys.readouterr().out == "echo.text  text  Echo text back, repeated.  [installed]\n"


@pytest.mark.parametrize(
    "argv",
    [["list", "--no-wait"], ["describe", "echo.text", "--no-wait"]],
    ids=" ".join,
)
def test_lock_flags_are_rejected(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(argv)
    assert exit_info.value.code == EXIT_USAGE


def test_unknown_capability_is_invalid_input(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    assert main(["describe", "nope.nothing", "--json"]) == EXIT_INVALID_INPUT
    envelope: Any = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "invalid_input"
    assert "echo.text" in envelope["error"]["details"]["known"]


def test_every_piece_example_validates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _registry(monkeypatch)
    registry = load_registry(FIXTURES)
    checked = 0
    for package in registry.packages.values():
        for capability in package.capabilities:
            example = introspect.describe_data(capability.id)["piece_example"]
            assert isinstance(example, str)
            piece: Any = tomllib.loads(example)
            schema_module.validate(capability, piece["inputs"], tmp_path)
            checked += 1
    assert checked == FIXTURE_CAPABILITY_COUNT


def test_piece_example_covers_every_field_kind() -> None:
    """Each type gets a schema-valid value: enum first, min, false, [], text."""
    capability = Capability(
        id="demo.every",
        package="demo",
        summary="Every field kind.",
        kind="text",
        inputs=(
            Field(name="mode", type="enum", required=True, values=("fast", "slow")),
            Field(name="gain", type="number", required=True, min=0.5),
            Field(name="loud", type="boolean", default=True),
            Field(name="count", type="integer", required=True, min=2.0),
            Field(name="src", type="path", required=True),
            Field(name="untouched", type="string"),
            Field(
                name="takes",
                type="array",
                required=True,
                items=(Field(name="label", type="string", required=True),),
            ),
        ),
    )
    example = introspect._piece_example(capability)
    assert example == (
        "[inputs]\n"
        'mode = "fast"\n'
        "gain = 0.5\n"
        "loud = false\n"
        "count = 2\n"
        'src = "text"\n'
        "[[inputs.takes]]\n"
        'label = "text"\n'
    )
    tomllib.loads(example)
