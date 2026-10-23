import json
import os
import re
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from facetmedia.contract import FacetError
from facetmedia.home import (
    Home,
    atomic_write_json,
    log_file,
    new_run_id,
    read_json,
    require_posix,
    same_filesystem_check,
    uv_env,
)

_ROOT_MODE = 0o700


def _home() -> Home:
    return Home.from_env()


def test_layout_is_created_with_private_root() -> None:
    home = _home()
    assert home.root.is_dir()
    assert stat.S_IMODE(home.root.stat().st_mode) == _ROOT_MODE
    for directory in (
        home.root / "registry",
        home.root / "state",
        home.root / "envs",
        home.root / "models" / "direct",
        home.root / "models" / "hf",
        home.root / "python",
        home.root / "cache" / "uv",
        home.root / "logs",
        home.root / "tmp",
    ):
        assert directory.is_dir()


def test_path_properties() -> None:
    home = _home()
    root = home.root
    assert home.registry_dir("abc123") == root / "registry" / "abc123"
    assert home.state_file("registry.json") == root / "state" / "registry.json"
    assert home.env_dir("pkg", "1.0") == root / "envs" / "pkg" / "1.0"
    assert home.model_dir("m1") == root / "models" / "direct" / "m1"
    assert home.hf_dir == root / "models" / "hf"
    assert home.python_dir == root / "python"
    assert home.uv_cache_dir == root / "cache" / "uv"
    assert home.licenses_file == root / "state" / "licenses.json"
    assert home.lock_file == root / "state" / "lock"
    assert home.lock_json == root / "state" / "lock.json"
    assert home.logs_dir == root / "logs"
    assert home.tmp_dir == root / "tmp"


def test_uv_env_has_every_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    home = _home()
    env = uv_env(home)
    assert env["UV_CACHE_DIR"] == str(home.uv_cache_dir)
    assert env["UV_PYTHON_INSTALL_DIR"] == str(home.python_dir)
    assert env["UV_PYTHON_PREFERENCE"] == "only-managed"
    assert env["UV_NO_CONFIG"] == "1"
    assert env["HF_HOME"] == str(home.hf_dir)
    assert env["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert env["PATH"] == "/usr/bin:/bin"


def test_atomic_write_replaces(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    atomic_write_json(target, {"b": 2, "a": 1}, tmp_dir=scratch)
    assert target.read_text(encoding="utf-8") == '{\n  "a": 1,\n  "b": 2\n}\n'
    atomic_write_json(target, {"a": 2}, tmp_dir=scratch)
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}


def test_atomic_write_uses_home_tmp_by_default() -> None:
    home = _home()
    target = home.state_file("registry.json")
    atomic_write_json(target, {"commit": "abc"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"commit": "abc"}
    assert list(home.tmp_dir.iterdir()) == []


def test_atomic_write_failure_keeps_old_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    atomic_write_json(target, {"v": 1}, tmp_dir=scratch)
    before = target.read_text(encoding="utf-8")

    def _boom(_src: str, _dst: str) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        atomic_write_json(target, {"v": 2}, tmp_dir=scratch)
    assert target.read_text(encoding="utf-8") == before
    assert list(scratch.iterdir()) == []


def test_windows_drive_home_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = Path("/mnt/c/facet-home-refused-7f3a")
    monkeypatch.setenv("FACET_HOME", str(candidate))
    with pytest.raises(FacetError) as exc:
        Home.from_env()
    assert exc.value.code == "machine_cannot"
    assert exc.value.hint == "keep FACET_HOME on the Linux filesystem"
    assert not candidate.exists()


def test_same_filesystem_mismatch_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Home(tmp_path / "h")
    home.ensure()

    def _fake_stat(path: object, *args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(st_dev=1 if str(path).endswith("tmp") else 2)

    monkeypatch.setattr(os, "stat", _fake_stat)
    with pytest.raises(FacetError) as exc:
        same_filesystem_check(home)
    assert exc.value.code == "machine_cannot"


def test_read_json_missing_returns_default(tmp_path: Path) -> None:
    assert read_json(tmp_path / "nope.json", {"d": 1}) == {"d": 1}


def test_read_json_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "s.json"
    atomic_write_json(target, {"k": [1, 2]}, tmp_dir=tmp_path)
    assert read_json(target, None) == {"k": [1, 2]}


def test_read_json_corrupt_names_the_file(tmp_path: Path) -> None:
    target = tmp_path / "s.json"
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(FacetError) as exc:
        read_json(target, None)
    assert exc.value.code == "internal"
    assert str(target) in exc.value.message


def test_read_json_unreadable_names_the_file(tmp_path: Path) -> None:
    target = tmp_path / "s.json"
    target.mkdir()
    with pytest.raises(FacetError) as exc:
        read_json(target, None)
    assert exc.value.code == "internal"
    assert str(target) in exc.value.message


def test_new_run_id_shape() -> None:
    first, second = new_run_id(), new_run_id()
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{6}", first)
    assert first != second


def test_log_file_path_and_no_creation() -> None:
    home = _home()
    run_id = "20260304T050607Z-abcdef"
    assert log_file(run_id) == home.logs_dir / "2026-03-04" / f"{run_id}.log"
    assert not (home.logs_dir / "2026-03-04").exists()


def test_log_file_falls_back_to_today_for_unknown_run_id() -> None:
    home = _home()
    path = log_file("garbage")
    assert path.name == "garbage.log"
    assert path.parent.parent == home.logs_dir


def test_require_posix_rejects_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    with pytest.raises(FacetError) as exc:
        require_posix()
    assert exc.value.code == "machine_cannot"
    assert "WSL" in exc.value.message


def test_require_posix_passes_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    require_posix()
