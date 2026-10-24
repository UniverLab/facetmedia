"""``facet rm`` and ``facet gc``: what they free, what they keep, what they cost.

Only the tests that need a real environment pull the dependency-free ``echo``
fixture; everything else is planted on disk, so gc's fixed reasons are asserted
exactly and no test depends on the machine's network or on real model weights.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from facetmedia.cli import main
from facetmedia.home import Home

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
TERMSY_MODEL = "termsy-weights"
CURRENT_COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
EXIT_RUNNER_FAILED = 9
PAYLOAD = b"x" * 16

#: A runner that dies before it writes any result: run keeps its temp dir.
CRASHING_RUNNER = '''"""Echo run that dies before the result file."""

from __future__ import annotations

from facet_runner_sdk import Request, Runner

runner = Runner()


@runner.verb("run")
def run(req: Request) -> dict[str, object]:
    raise SystemExit(1)


runner.main()
'''


# --- helpers.


def _registry(monkeypatch: pytest.MonkeyPatch, path: Path = FIXTURES) -> None:
    monkeypatch.setenv("FACET_REGISTRY", str(path))


def _run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    """Run ``facet <argv> --json``; return (exit code, envelope data)."""
    code = main([*argv, "--json"])
    envelope = json.loads(capsys.readouterr().out)
    assert isinstance(envelope, dict)
    assert envelope["ok"] is True, envelope
    data = envelope["data"]
    assert isinstance(data, dict)
    return code, data


def _plant_env(home: Home, name: str, version: str, complete: bool = True) -> Path:
    """An environment directory with its payload and, when asked, its marker."""
    env_dir = home.env_dir(name, version)
    env_dir.mkdir(parents=True)
    (env_dir / "payload").write_bytes(PAYLOAD)
    if complete:
        (env_dir / ".complete").write_text("0" * 64, encoding="utf-8")
    return env_dir


def _items(data: dict[str, Any]) -> list[dict[str, Any]]:
    items = data["items"]
    assert isinstance(items, list)
    return items


def _reasons(data: dict[str, Any]) -> dict[str, str]:
    return {Path(item["path"]).name: item["reason"] for item in _items(data)}


def _reason_by_path(data: dict[str, Any]) -> dict[Path, str]:
    return {Path(item["path"]): item["reason"] for item in _items(data)}


def _bytes_by_path(data: dict[str, Any]) -> dict[Path, int]:
    return {Path(item["path"]): int(item["bytes"]) for item in _items(data)}


def _piece(directory: Path, text: str = "hello", repeat: int = 2) -> Path:
    piece = directory / "piece.toml"
    piece.write_text(f'[inputs]\ntext = "{text}"\nrepeat = {repeat}\n', encoding="utf-8")
    return piece


# --- facet rm (FR1).


def test_rm_echo_frees_its_environment_then_is_a_noop(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    home = Home.from_env()
    assert main(["pull", "echo", "--json"]) == 0
    capsys.readouterr()
    env_dir = home.env_dir("echo", "0.1.0")
    assert (env_dir / ".complete").is_file()

    code, data = _run_json(capsys, "rm", "echo")
    assert code == 0
    assert data["removed"] == [str(env_dir)]
    assert int(data["freed_bytes"]) > 0
    assert not (home.envs_dir / "echo").exists()

    # Removing a package that is not installed is a no-op with exit 0.
    code, data = _run_json(capsys, "rm", "echo")
    assert code == 0
    assert data == {"removed": [], "freed_bytes": 0}


def test_rm_models_deletes_the_model_of_the_package(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    home = Home.from_env()
    env_dir = _plant_env(home, "termsy", "0.1.0")
    model = home.models_dir / "direct" / TERMSY_MODEL / "termsy.bin"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"weights")
    expected = sum(entry.stat().st_size for entry in env_dir.rglob("*") if entry.is_file())
    expected += len(b"weights")

    code, data = _run_json(capsys, "rm", "termsy", "--models")
    assert code == 0
    assert data["removed"] == [str(env_dir), str(model.parent)]
    assert int(data["freed_bytes"]) == expected
    assert not env_dir.exists()
    assert not model.parent.exists()


def test_rm_keeps_a_model_another_installed_package_lists(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry = tmp_path / "registry"
    shutil.copytree(FIXTURES, registry)
    manifest = registry / "packages" / "echo" / "package.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + f"""
[[models]]
id = "shared-weights"
spdx = "Apache-2.0"
commercial_use = true
requires_acceptance = false
bytes = 21
url = "file://tests/fixtures/models/termsy.bin"
sha256 = "85a73737b5d85eaba43e73ba33ceade28de49f1f4f7573377f02d58408f715c7"
path = "{TERMSY_MODEL}/termsy.bin"
""",
        encoding="utf-8",
    )
    _registry(monkeypatch, registry)
    home = Home.from_env()
    _plant_env(home, "termsy", "0.1.0")
    _plant_env(home, "echo", "0.1.0")
    model = home.models_dir / "direct" / TERMSY_MODEL / "termsy.bin"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"weights")

    code, data = _run_json(capsys, "rm", "termsy", "--models")
    assert code == 0
    assert str(home.env_dir("termsy", "0.1.0")) in data["removed"]
    # echo still lists that model, so removing termsy must not take it away.
    assert str(model.parent) not in data["removed"]
    assert model.read_bytes() == b"weights"

    # Once echo is gone too, nothing lists it any more.
    code, data = _run_json(capsys, "rm", "echo", "--models")
    assert code == 0
    assert str(model.parent) in data["removed"]
    assert not model.parent.exists()


# --- facet gc (FR2): the environment and model families.


def test_gc_removes_incomplete_and_stale_environments_and_keeps_the_current(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    home = Home.from_env()
    current = _plant_env(home, "echo", "0.1.0")
    stale = _plant_env(home, "echo", "0.0.9")
    unfinished = _plant_env(home, "termsy", "0.1.0", complete=False)

    code, data = _run_json(capsys, "gc")
    assert code == 0
    assert data["dry_run"] is False
    reasons = _reason_by_path(data)
    assert reasons[stale] == "stale-version"
    assert reasons[unfinished] == "incomplete-env"
    assert current not in reasons
    assert int(data["freed_bytes"]) > 0
    assert current.is_dir() and (current / ".complete").is_file()
    assert not stale.exists()
    assert not unfinished.exists()


def test_gc_removes_models_no_package_lists(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    home = Home.from_env()
    orphan = home.models_dir / "direct" / "nobody-lists-me"
    orphan.mkdir()
    (orphan / "weights.bin").write_bytes(PAYLOAD)
    known = home.models_dir / "direct" / TERMSY_MODEL
    known.mkdir()
    (known / "termsy.bin").write_bytes(PAYLOAD)

    code, data = _run_json(capsys, "gc")
    assert code == 0
    assert _reasons(data)[orphan.name] == "orphan-model"
    assert int(data["freed_bytes"]) == len(PAYLOAD)
    assert not orphan.exists()
    assert known.is_dir()


def test_gc_dry_run_deletes_nothing_and_reports_the_same_list(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    home = Home.from_env()
    current = _plant_env(home, "echo", "0.1.0")
    stale = _plant_env(home, "echo", "0.0.9")
    unfinished = _plant_env(home, "termsy", "0.1.0", complete=False)
    part = home.tmp_dir / "hf-weights.1a2b.part"
    part.write_bytes(PAYLOAD)

    code, dry = _run_json(capsys, "gc", "--dry-run")
    assert code == 0
    assert dry["dry_run"] is True
    assert stale.exists() and unfinished.exists() and part.exists()
    assert current.is_dir()
    # Everything the dry run listed is still there, including uv's cache item.
    expected = {Path(item["path"]) for item in _items(dry)}
    assert {stale, unfinished, part, home.uv_cache_dir} <= expected
    assert _reasons(dry)[part.name] == "tmp"
    assert _bytes_by_path(dry)[part] == len(PAYLOAD)

    code, real = _run_json(capsys, "gc")
    assert code == 0
    assert real["dry_run"] is False
    assert [item["path"] for item in _items(real)] == [item["path"] for item in _items(dry)]
    assert [item["reason"] for item in _items(real)] == [item["reason"] for item in _items(dry)]
    assert not stale.exists() and not unfinished.exists() and not part.exists()
    assert current.is_dir()


# --- facet gc: the snapshot, leftover and log families.


def test_gc_removes_old_and_partial_snapshots_offline(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FACET_REGISTRY", raising=False)
    home = Home.from_env()
    in_use = home.registry_dir(CURRENT_COMMIT)
    shutil.copytree(FIXTURES, in_use)
    (in_use / ".complete").write_text(f"{CURRENT_COMMIT}\n", encoding="utf-8")
    old = home.registry_dir(OTHER_COMMIT)
    old.mkdir()
    (old / "index.toml").write_text("version = 1\n", encoding="utf-8")
    partial = home.registry_dir(f"{OTHER_COMMIT}.partial")
    partial.mkdir()
    (partial / "index.toml").write_text("version = 1\n", encoding="utf-8")
    home.state_file("registry.json").write_text(
        json.dumps({"commit": CURRENT_COMMIT, "ref": "main"}),
        encoding="utf-8",
    )

    code, data = _run_json(capsys, "gc")
    assert code == 0
    reasons = _reasons(data)
    assert reasons[OTHER_COMMIT] == "old-snapshot"
    assert reasons[f"{OTHER_COMMIT}.partial"] == "partial-snapshot"
    assert not old.exists()
    assert not partial.exists()
    assert (in_use / "index.toml").is_file()
    assert CURRENT_COMMIT not in reasons


def test_gc_removes_old_log_directories_only(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    home = Home.from_env()
    old = home.logs_dir / "2020-01-01"
    old.mkdir()
    (old / "run.log").write_text("old\n", encoding="utf-8")
    recent = home.logs_dir / "2999-01-01"
    recent.mkdir()
    (recent / "run.log").write_text("recent\n", encoding="utf-8")

    code, data = _run_json(capsys, "gc")
    assert code == 0
    assert _reasons(data)["2020-01-01"] == "old-log"
    assert not old.exists()
    assert recent.is_dir()


def test_gc_removes_only_the_leftovers_the_state_file_lists(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _registry(monkeypatch)
    home = Home.from_env()
    kept = _kept_dir(tmp_path, "listed")
    unlisted = _kept_dir(tmp_path, "unlisted")
    state = home.state_file("leftovers.json")
    state.write_text(json.dumps([str(kept), "/nonexistent/already-gone"]), encoding="utf-8")

    code, dry = _run_json(capsys, "gc", "--dry-run")
    assert code == 0
    leftover = [Path(item["path"]) for item in _items(dry) if item["reason"] == "leftover"]
    assert leftover == [kept]
    assert kept.is_dir()

    code, data = _run_json(capsys, "gc")
    assert code == 0
    assert [Path(item["path"]) for item in _items(data) if item["reason"] == "leftover"] == [kept]
    assert not kept.exists()
    assert json.loads(state.read_text(encoding="utf-8")) == []
    # gc never searches the disk: a directory nobody listed stays untouched.
    assert unlisted.is_dir()


def _kept_dir(root: Path, name: str) -> Path:
    directory = root / "out" / f".facet-tmp-{name}"
    directory.mkdir(parents=True)
    (directory / "half.txt").write_text("partial\n", encoding="utf-8")
    return directory


def test_a_failed_run_records_its_kept_directory_for_gc(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _registry(monkeypatch)
    home = Home.from_env()
    assert main(["pull", "echo", "--json"]) == 0
    capsys.readouterr()
    env_dir = home.env_dir("echo", "0.1.0")
    (env_dir / "package" / "runner.py").write_text(CRASHING_RUNNER, encoding="utf-8")
    out = tmp_path / "out"

    code = main(["run", "echo.text", str(_piece(tmp_path)), "--out", str(out), "--json"])
    envelope = json.loads(capsys.readouterr().out)
    assert code == EXIT_RUNNER_FAILED
    assert envelope["error"]["code"] == "runner_failed"
    kept = list(out.glob(".facet-tmp-*"))
    assert len(kept) == 1
    state = home.state_file("leftovers.json")
    assert json.loads(state.read_text(encoding="utf-8")) == [str(kept[0].resolve())]

    code, data = _run_json(capsys, "gc")
    assert code == 0
    assert any(item["reason"] == "leftover" for item in _items(data))
    assert not kept[0].exists()
    assert json.loads(state.read_text(encoding="utf-8")) == []
    # The complete environment a failed run does not own is kept.
    assert (env_dir / ".complete").is_file()


# --- facet run --ephemeral (FR3).


def test_ephemeral_keeps_an_installed_environment_and_removes_a_fresh_one(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _registry(monkeypatch)
    home = Home.from_env()
    assert main(["pull", "echo", "--json"]) == 0
    capsys.readouterr()
    piece = _piece(tmp_path)
    env_dir = home.env_dir("echo", "0.1.0")

    # Already installed: --ephemeral changes nothing, and says so on stderr.
    installed_out = tmp_path / "installed-out"
    code = main(
        ["run", "echo.text", str(piece), "--out", str(installed_out), "--ephemeral", "--json"]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "already installed; --ephemeral changes nothing" in captured.err
    assert (env_dir / ".complete").is_file()
    assert (installed_out / "echo.txt").is_file()

    # Not installed: this run installs it, uses it, and removes it again.
    assert main(["rm", "echo", "--json"]) == 0
    capsys.readouterr()
    fresh_out = tmp_path / "fresh-out"
    code = main(["run", "echo.text", str(piece), "--out", str(fresh_out), "--ephemeral", "--json"])
    data = json.loads(capsys.readouterr().out)["data"]
    assert code == 0
    assert data["pulled"] is True
    assert (fresh_out / "echo.txt").read_text(encoding="utf-8") == "hellohello"
    stored = json.loads((fresh_out / "echo.txt.facet.json").read_text(encoding="utf-8"))
    assert stored["name"] == "echo"
    assert stored["provenance"]["capability"] == "echo.text"
    assert not (home.envs_dir / "echo").exists()
