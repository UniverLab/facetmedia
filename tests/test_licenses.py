"""``facet license``: explicit acceptance before models with terms are used."""

from __future__ import annotations

import dataclasses
import json
import shutil
import sys
from pathlib import Path

import pytest

from facetmedia import licenses as licenses_module
from facetmedia.cli import main
from facetmedia.contract import FacetError
from facetmedia.home import Home
from facetmedia.registry import load_registry

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
MODEL_ID = "termsy-weights"
EXIT_INVALID_INPUT = 2
EXIT_NEEDS_PERSON = 10


def _use_fixture_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))


def _acceptance_records() -> list[dict[str, object]]:
    path = Home.peek_env().licenses_file
    if not path.is_file():
        return []
    state = json.loads(path.read_text(encoding="utf-8"))
    accepted = state.get("accepted")
    assert isinstance(accepted, list)
    return accepted


def _not_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)


def _terminal_with_answer(monkeypatch: pytest.MonkeyPatch, answer: str) -> list[str]:
    prompts: list[str] = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    def _fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        return answer

    monkeypatch.setattr("builtins.input", _fake_input)
    return prompts


def test_accept_without_yes_off_terminal_fails(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_fixture_registry(monkeypatch)
    _not_a_terminal(monkeypatch)
    assert main(["license", "accept", MODEL_ID, "--json"]) == EXIT_NEEDS_PERSON
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["error"]["code"] == "license_needs_person"
    assert "--yes" in envelope["error"]["hint"]
    assert MODEL_ID in captured.err
    assert _acceptance_records() == []


def test_accept_yes_records_flag_and_warns(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_fixture_registry(monkeypatch)
    _not_a_terminal(monkeypatch)
    assert main(["license", "accept", MODEL_ID, "--yes"]) == 0
    captured = capsys.readouterr()
    for fragment in (
        MODEL_ID,
        "termsy",
        "CC-BY-NC-4.0",
        "commercial_use",
        "https://example.invalid/termsy",
        "Commercial use is not allowed.",
        "Accepting makes the person who runs facet responsible",
    ):
        assert fragment in captured.err
    records = _acceptance_records()
    assert len(records) == 1
    record = records[0]
    assert record["model"] == MODEL_ID
    assert record["spdx"] == "CC-BY-NC-4.0"
    assert record["method"] == "flag"
    assert isinstance(record["by"], str) and record["by"]
    assert isinstance(record["at"], str) and record["at"].endswith("Z")
    # Accepting again replaces the earlier record instead of appending.
    assert main(["license", "accept", MODEL_ID, "--yes"]) == 0
    capsys.readouterr()
    assert len(_acceptance_records()) == 1


def test_accept_tty_with_right_id_records_interactive(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_fixture_registry(monkeypatch)
    prompts = _terminal_with_answer(monkeypatch, MODEL_ID)
    assert main(["license", "accept", MODEL_ID]) == 0
    capsys.readouterr()
    assert prompts == ["Type the model id to accept: "]
    records = _acceptance_records()
    assert len(records) == 1
    assert records[0]["method"] == "interactive"


def test_accept_tty_with_wrong_id_records_nothing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_fixture_registry(monkeypatch)
    _terminal_with_answer(monkeypatch, "not-the-model")
    assert main(["license", "accept", MODEL_ID, "--json"]) == EXIT_INVALID_INPUT
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"]["code"] == "invalid_input"
    assert _acceptance_records() == []


def test_accept_unknown_model_lists_ids_requiring_acceptance(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_fixture_registry(monkeypatch)
    assert main(["license", "accept", "nope", "--yes", "--json"]) == EXIT_INVALID_INPUT
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["error"]["code"] == "invalid_input"
    assert MODEL_ID in envelope["error"]["details"]["known"]


def test_accept_model_without_terms_answers_nothing_to_accept(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry_copy = tmp_path / "registry"
    shutil.copytree(FIXTURES, registry_copy)
    manifest = registry_copy / "packages" / "echo" / "package.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + """
[[models]]
id = "echo-weights"
spdx = "Apache-2.0"
commercial_use = true
requires_acceptance = false
bytes = 1
url = "file://tests/fixtures/models/termsy.bin"
sha256 = "85a73737b5d85eaba43e73ba33ceade28de49f1f4f7573377f02d58408f715c7"
path = "echo-weights/termsy.bin"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("FACET_REGISTRY", str(registry_copy))
    assert main(["license", "accept", "echo-weights"]) == 0
    captured = capsys.readouterr()
    assert "nothing to accept" in captured.out
    assert not Home.peek_env().licenses_file.is_file()


def test_require_passes_after_acceptance_and_fails_on_spdx_change(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_fixture_registry(monkeypatch)
    _not_a_terminal(monkeypatch)
    package = load_registry(FIXTURES).packages["termsy"]
    with pytest.raises(FacetError) as before:
        licenses_module.require(package.models)
    assert before.value.code == "license_not_accepted"
    entry = before.value.details["models"][0]
    assert entry["id"] == MODEL_ID
    assert entry["command"] == f"facet license accept {MODEL_ID}"
    assert "--yes" not in entry["command"]
    assert main(["license", "accept", MODEL_ID, "--yes"]) == 0
    capsys.readouterr()
    matched = licenses_module.require(package.models)
    assert [record["model"] for record in matched] == [MODEL_ID]
    changed = dataclasses.replace(package.models[0], spdx="CC-BY-4.0")
    with pytest.raises(FacetError) as after:
        licenses_module.require([changed])
    assert after.value.code == "license_not_accepted"


def test_license_list_json_shape(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_fixture_registry(monkeypatch)
    assert main(["license", "list", "--json"]) == 0
    before = json.loads(capsys.readouterr().out)["data"]["models"]
    assert len(before) == 1
    assert before[0]["id"] == MODEL_ID
    assert before[0]["accepted"] is False
    assert before[0]["by"] is None
    assert before[0]["at"] is None
    assert before[0]["method"] is None
    _not_a_terminal(monkeypatch)
    assert main(["license", "accept", MODEL_ID, "--yes", "--json"]) == 0
    capsys.readouterr()
    assert main(["license", "list", "--json"]) == 0
    after = json.loads(capsys.readouterr().out)["data"]["models"]
    assert len(after) == 1
    row = after[0]
    assert row["id"] == MODEL_ID
    assert row["package"] == "termsy"
    assert row["spdx"] == "CC-BY-NC-4.0"
    assert row["accepted"] is True
    assert isinstance(row["by"], str) and row["by"]
    assert isinstance(row["at"], str) and row["at"]
    assert row["method"] == "flag"
