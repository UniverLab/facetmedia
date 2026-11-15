"""``facet example``: mapping rules, the copy, flags, errors, --run and preview."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from facetmedia import example as example_module
from facetmedia import preview as preview_module
from facetmedia import run as run_module
from facetmedia import schema
from facetmedia.cli import main
from facetmedia.registry import load_active_registry

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
EXIT_INVALID_INPUT = 2
EXIT_VERIFICATION_FAILED = 3

EXTRA_CAPABILITIES = """
[capabilities.pick]
summary = "Take an image as input."
kind = "image"

[capabilities.pick.preview]
viewer = "generic"

[capabilities.pick.inputs.image]
type = "path"
required = true
description = "The image to take."

[capabilities.pick.inputs.extra]
type = "path"
required = false
description = "An extra file or folder."

[capabilities.note]
summary = "Note text."
kind = "text"

[capabilities.note.preview]
viewer = "generic"

[capabilities.note.inputs.note]
type = "string"
required = false
description = "A note."

[capabilities.mark]
summary = "Mark text."
kind = "text"

[capabilities.mark.preview]
viewer = "generic"

[capabilities.mark.inputs.mark]
type = "string"
required = false
description = "A mark."
"""


def _registry_with_examples(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A registry copy whose echo package ships example TOMLs with sibling refs."""
    target = tmp_path / "registry"
    shutil.copytree(FIXTURES, target)
    manifest = target / "packages" / "echo" / "package.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8") + EXTRA_CAPABILITIES)
    examples = target / "packages" / "echo" / "examples"
    examples.mkdir()
    (examples / "r1.toml").write_text(
        'capability = "echo.text"\n[inputs]\ntext = "hello"\n', encoding="utf-8"
    )
    (examples / "text.toml").write_text('[inputs]\ntext = "hello"\n', encoding="utf-8")
    (examples / "custom.toml").write_text(
        '[inputs]\nimage = "lamina.png"\nextra = "bundle"\n', encoding="utf-8"
    )
    (examples / "memo.toml").write_text('[inputs]\nnote = "hello"\n', encoding="utf-8")
    (examples / "empty.toml").write_text("[inputs]\n", encoding="utf-8")
    (examples / "weird.toml").write_text("[inputs]\ntext = 42\n", encoding="utf-8")
    (examples / "lamina.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    bundle = examples / "bundle"
    bundle.mkdir()
    (bundle / "nested.txt").write_text("nested\n", encoding="utf-8")
    monkeypatch.setenv("FACET_REGISTRY", str(target))
    return target


def _envelope(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    captured = capsys.readouterr()
    parsed: Any = json.loads(captured.out)
    assert isinstance(parsed, dict)
    return parsed


def test_find_examples_assigns_each_rule_and_excludes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _registry_with_examples(monkeypatch, tmp_path)
    package = load_active_registry().package("echo")
    assert package is not None
    found = example_module.find_examples(package)
    assert [item.piece for item in found] == ["custom", "memo", "r1", "text"]
    assert {item.piece: item.capability for item in found} == {
        "custom": "echo.pick",
        "memo": "echo.note",
        "r1": "echo.text",
        "text": "echo.text",
    }


def test_copy_default_dest_layout_and_runnable(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _registry_with_examples(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["example", "echo.pick"]) == 0
    dest = tmp_path / "echo-pick"
    assert (dest / "custom.toml").is_file()
    assert (dest / "lamina.png").is_file()
    assert (dest / "bundle" / "nested.txt").is_file()
    capability = load_active_registry().capability("echo.pick")
    raw = {"image": "lamina.png", "extra": "bundle"}
    schema.validate(capability, raw, dest)
    out = capsys.readouterr().out
    assert f"facet run echo.pick {dest}/custom.toml --out {dest}/out" in out


def test_copy_explicit_dest_and_name_into_empty_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _registry_with_examples(monkeypatch, tmp_path)
    dest = tmp_path / "here"
    dest.mkdir()
    assert main(["example", "echo.text", str(dest), "--name", "text"]) == 0
    assert (dest / "text.toml").is_file()
    assert not (dest / "r1.toml").exists()


def test_list_prints_every_example(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _registry_with_examples(monkeypatch, tmp_path)
    assert main(["example", "echo.text", "--list"]) == 0
    out = capsys.readouterr().out
    assert "r1.toml" in out
    assert "text.toml" in out
    assert "custom.toml" not in out


def test_list_json_envelope(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _registry_with_examples(monkeypatch, tmp_path)
    assert main(["example", "echo.pick", "--list", "--json"]) == 0
    data = _envelope(capsys)["data"]
    assert [entry["piece"] for entry in data["examples"]] == ["custom"]


def test_name_miss_fails(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _registry_with_examples(monkeypatch, tmp_path)
    assert main(["example", "echo.text", "--name", "nope"]) == EXIT_INVALID_INPUT
    assert "nope" in capsys.readouterr().err


def test_nonempty_dest_fails_and_copies_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _registry_with_examples(monkeypatch, tmp_path)
    dest = tmp_path / "taken"
    dest.mkdir()
    (dest / "mine.txt").write_text("mine\n", encoding="utf-8")
    assert main(["example", "echo.text", str(dest)]) == EXIT_INVALID_INPUT
    assert [path.name for path in dest.iterdir()] == ["mine.txt"]


def test_no_example_names_capability_and_lists_haves(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _registry_with_examples(monkeypatch, tmp_path)
    assert main(["example", "echo.mark"]) == EXIT_INVALID_INPUT
    err = capsys.readouterr().err
    assert "echo.mark has no example" in err
    assert "echo.note" in err
    assert "echo.pick" in err
    assert "echo.text" in err


def test_unknown_capability_fails(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _registry_with_examples(monkeypatch, tmp_path)
    assert main(["example", "nope.cap"]) == EXIT_INVALID_INPUT
    assert "unknown capability: nope.cap" in capsys.readouterr().err


def test_json_carries_dest_piece_files(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _registry_with_examples(monkeypatch, tmp_path)
    dest = tmp_path / "out-text"
    assert main(["example", "echo.text", str(dest), "--name", "text", "--json"]) == 0
    data = _envelope(capsys)["data"]
    assert data["dest"] == str(dest.resolve())
    assert data["piece"] == "text.toml"
    assert data["files"] == ["text.toml"]


def _fake_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, failed: bool = False
) -> dict[str, list[dict[str, object]]]:
    """Replace ``run.execute`` with a fake writing one asset; return the calls seen."""
    calls: dict[str, list[dict[str, object]]] = {"run": [], "preview": []}

    def fake_run_execute(args: argparse.Namespace, run_id: str) -> tuple[dict[str, object], int]:
        out = Path(str(args.out))
        out.mkdir(parents=True, exist_ok=True)
        asset = out / "echo.txt"
        asset.write_text("hello", encoding="utf-8")
        calls["run"].append({"piece": str(args.piece), "run_id": run_id})
        status = "failed" if failed else "passed"
        return (
            {
                "capability": "echo.text",
                "out_dir": str(out),
                "assets": [
                    {
                        # A record stores the bare file name; out_dir locates it.
                        "path": "echo.txt",
                        "name": "echo",
                        "kind": "text",
                        "verification": {"status": status},
                    }
                ],
            },
            3 if failed else 0,
        )

    def fake_preview_execute(
        args: argparse.Namespace, run_id: str
    ) -> tuple[dict[str, object], int]:
        calls["preview"].append({"asset": str(args.asset), "no_open": bool(args.no_open)})
        return {}, 0

    monkeypatch.setattr(run_module, "execute", fake_run_execute)
    monkeypatch.setattr(preview_module, "execute", fake_preview_execute)
    return calls


def test_json_run_adds_run_data_and_stops(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _registry_with_examples(monkeypatch, tmp_path)
    calls = _fake_run(monkeypatch, tmp_path)
    dest = tmp_path / "run-text"
    assert main(["example", "echo.text", str(dest), "--run", "--json"]) == 0
    data = _envelope(capsys)["data"]
    assert data["run"]["capability"] == "echo.text"
    assert data["run"]["assets"][0]["name"] == "echo"
    assert data["preview_command"] == f"facet preview {dest}/out/echo.txt"
    assert calls["preview"] == []


def test_json_run_failure_envelope(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _registry_with_examples(monkeypatch, tmp_path)
    _fake_run(monkeypatch, tmp_path, failed=True)
    dest = tmp_path / "run-failed"
    assert main(["example", "echo.text", str(dest), "--run", "--json"]) == EXIT_VERIFICATION_FAILED
    envelope = _envelope(capsys)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "verification_failed"
    assert envelope["error"]["details"] == {"assets": [f"{dest}/out/echo.txt"]}
    assert envelope["data"]["run"]["assets"][0]["verification"]["status"] == "failed"


def test_run_previews_first_asset_on_tty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _registry_with_examples(monkeypatch, tmp_path)
    calls = _fake_run(monkeypatch, tmp_path)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    dest = tmp_path / "run-tty"
    assert main(["example", "echo.text", str(dest), "--run", "--no-open"]) == 0
    assert len(calls["preview"]) == 1
    assert calls["preview"][0]["asset"].endswith("echo.txt")
    assert calls["preview"][0]["no_open"] is True


def test_run_skips_preview_without_tty(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _registry_with_examples(monkeypatch, tmp_path)
    calls = _fake_run(monkeypatch, tmp_path)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    dest = tmp_path / "run-plain"
    assert main(["example", "echo.text", str(dest), "--run"]) == 0
    assert calls["run"] != []
    assert calls["preview"] == []
    # The text output still says how to look at what the run made.
    assert capsys.readouterr().out.splitlines()[-1] == (
        f"preview: facet preview {dest}/out/echo.txt"
    )


def test_run_prints_copy_before_run_output(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _registry_with_examples(monkeypatch, tmp_path)
    calls = _fake_run(monkeypatch, tmp_path)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    dest = tmp_path / "run-order"
    assert main(["example", "echo.text", str(dest), "--run", "--no-open"]) == 0
    out = capsys.readouterr().out
    copy_line = out.index("r1.toml\n")
    next_command = out.index("facet run echo.text")
    run_assets = out.index("echo.txt echo passed")
    assert copy_line < next_command < run_assets
    assert len(calls["preview"]) == 1
