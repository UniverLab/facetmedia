import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from facetmedia import __version__
from facetmedia.contract import FacetError
from facetmedia.registry import (
    PROTOCOL_VERSION,
    load_active_registry,
    load_registry,
    registry_dir,
    validate_registry,
)
from tests.registry_server import dead_urls

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "registry"

ECHO_RAM_GB = 0.1
ECHO_DISK_GB = 0.01
ECHO_INPUT_COUNT = 2
REPEAT_MAX = 10
MODEL_BYTES = 21
SHA256_HEX_LEN = 64
MIN_PROBLEMS = 2


def test_load_fixture_registry() -> None:
    registry = load_registry(FIXTURE_ROOT)
    assert registry.version == 1
    assert set(registry.packages.keys()) == {"echo", "termsy", "future"}


def test_packages_sorted() -> None:
    registry = load_registry(FIXTURE_ROOT)
    caps = registry.capabilities()
    assert [cap.id for cap in caps] == ["echo.text", "future.thing", "termsy.make"]


def test_echo_fixture_fields() -> None:
    registry = load_registry(FIXTURE_ROOT)
    echo = registry.package("echo")
    assert echo is not None
    assert echo.name == "echo"
    assert echo.version == "0.1.0"
    assert echo.requires_facet == ">=0.1.0b1,<0.2"
    assert echo.protocol == PROTOCOL_VERSION
    assert echo.provider == "facet"
    assert echo.tool_name == "Echo"
    assert echo.engine_type == "python-env"
    assert echo.python == "3.12"
    assert echo.transport_type == "cli"
    assert echo.entry == "runner.py"
    assert echo.requirements.gpu is False
    assert echo.requirements.ram_gb == ECHO_RAM_GB
    assert echo.requirements.disk_gb == ECHO_DISK_GB
    assert echo.requirements.exclusive is False
    assert echo.requirements.platforms == ("linux",)
    assert len(echo.licenses) == 1
    assert echo.licenses[0].subject == "echo"
    assert echo.licenses[0].spdx == "Apache-2.0"
    assert len(echo.models) == 0
    assert len(echo.capabilities) == 1


def test_echo_capability() -> None:
    registry = load_registry(FIXTURE_ROOT)
    echo = registry.package("echo")
    assert echo is not None
    cap = echo.capabilities[0]
    assert cap.id == "echo.text"
    assert cap.package == "echo"
    assert cap.summary == "Echo text back, repeated."
    assert cap.kind == "text"
    assert cap.viewer == "generic"
    assert len(cap.inputs) == ECHO_INPUT_COUNT
    text_field = cap.input("text")
    assert text_field is not None
    assert text_field.type == "string"
    assert text_field.required is True
    assert text_field.description == "Text to echo."
    repeat_field = cap.input("repeat")
    assert repeat_field is not None
    assert repeat_field.type == "integer"
    assert repeat_field.required is False
    assert repeat_field.default == 1
    assert repeat_field.min == 1
    assert repeat_field.max == REPEAT_MAX


def test_termsy_fixture() -> None:
    registry = load_registry(FIXTURE_ROOT)
    termsy = registry.package("termsy")
    assert termsy is not None
    assert termsy.name == "termsy"
    assert len(termsy.models) == 1
    model = termsy.models[0]
    assert model.id == "termsy-weights"
    assert model.spdx == "CC-BY-NC-4.0"
    assert model.commercial_use is False
    assert model.requires_acceptance is True
    assert model.bytes == MODEL_BYTES
    assert model.url.startswith("file://")
    assert model.sha256 == "85a73737b5d85eaba43e73ba33ceade28de49f1f4f7573377f02d58408f715c7"
    assert model.path == "termsy-weights/termsy.bin"
    assert termsy.requirements.exclusive is True


def test_future_incompatible() -> None:
    registry = load_registry(FIXTURE_ROOT)
    future = registry.package("future")
    assert future is not None
    assert future.compatible is False
    with pytest.raises(FacetError) as exc:
        future.require_compatible()
    assert exc.value.code == "machine_cannot"
    assert "facet update" in exc.value.hint


def test_echo_compatible() -> None:
    registry = load_registry(FIXTURE_ROOT)
    echo = registry.package("echo")
    assert echo is not None
    assert echo.compatible is True
    echo.require_compatible()


def test_b1_specifier_compatible_with_installed_version() -> None:
    assert SpecifierSet(">=0.1.0b1,<0.2").contains(Version(__version__), prereleases=True)
    registry = load_registry(FIXTURE_ROOT)
    echo = registry.package("echo")
    assert echo is not None
    assert echo.requires_facet == ">=0.1.0b1,<0.2"
    assert echo.compatible is True
    echo.require_compatible()


def test_lock_sha256() -> None:
    registry = load_registry(FIXTURE_ROOT)
    echo = registry.package("echo")
    assert echo is not None
    lock_bytes = (FIXTURE_ROOT / "packages" / "echo" / "uv.lock").read_bytes()
    assert echo.lock_sha256 == hashlib.sha256(lock_bytes).hexdigest()
    assert len(echo.lock_sha256) == SHA256_HEX_LEN


def test_capability_lookup() -> None:
    registry = load_registry(FIXTURE_ROOT)
    cap = registry.capability("echo.text")
    assert cap.id == "echo.text"
    assert cap.package == "echo"


def test_unknown_capability_raises() -> None:
    registry = load_registry(FIXTURE_ROOT)
    with pytest.raises(FacetError) as exc:
        registry.capability("nonexistent.cap")
    assert exc.value.code == "invalid_input"
    assert "unknown capability" in exc.value.message
    assert "known" in exc.value.details
    assert set(exc.value.details["known"]) == {"echo.text", "future.thing", "termsy.make"}


def test_broken_manifest_raises_with_file_and_key() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "index.toml").write_text(
            'version = 1\n[[packages]]\nname = "broken"\n', encoding="utf-8"
        )
        pkg_dir = root / "packages" / "broken"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "package.toml").write_text('name = "wrong"\n', encoding="utf-8")
        with pytest.raises(FacetError) as exc:
            load_registry(root)
        assert exc.value.code == "internal"
        assert exc.value.message.startswith("packages/broken/package.toml: ")
        assert exc.value.details["key"] == "description"
        keys = {problem["key"] for problem in validate_registry(root)}
        assert {"name", "description"} <= keys


def test_validate_clean_returns_empty() -> None:
    problems = validate_registry(FIXTURE_ROOT)
    assert problems == []


def test_validate_broken_manifest_reports_all_problems() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "index.toml").write_text(
            'version = 1\n[[packages]]\nname = "bad"\n', encoding="utf-8"
        )
        pkg_dir = root / "packages" / "bad"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "package.toml").write_text(
            'name = "bad"\nversion = "invalid"\nunknown_key = "foo"\n', encoding="utf-8"
        )
        problems = validate_registry(root)
        assert len(problems) >= MIN_PROBLEMS
        problem_keys = {p["key"] for p in problems}
        assert "version" in problem_keys
        assert "unknown_key" in problem_keys


def _manifest_with_model(name: str, model_id: str) -> str:
    sha256 = "a" * SHA256_HEX_LEN
    return f"""name = "{name}"
version = "0.1.0"
requires_facet = ">=0.1.0b1,<0.2"
protocol = 1
provider = "test"
tool_name = "Test"
description = "test"

[engine]
type = "python-env"
python = "3.12"

[transport]
type = "cli"
entry = "runner.py"

[requirements]
exclusive = false

[[models]]
id = "{model_id}"
spdx = "MIT"
bytes = 10
url = "https://example.com/m.bin"
sha256 = "{sha256}"
path = "m.bin"
"""


def test_model_ids_unique_across_packages(tmp_path: Path) -> None:
    (tmp_path / "index.toml").write_text(
        'version = 1\n[[packages]]\nname = "one"\n[[packages]]\nname = "two"\n',
        encoding="utf-8",
    )
    for name in ("one", "two"):
        pkg_dir = tmp_path / "packages" / name
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "package.toml").write_text(
            _manifest_with_model(name, "shared-model"), encoding="utf-8"
        )
        for file_name in ("pyproject.toml", "uv.lock", "runner.py"):
            (pkg_dir / file_name).write_text("", encoding="utf-8")
    found = {(p["file"], p["key"], p["problem"]) for p in validate_registry(tmp_path)}
    assert ("packages/two/package.toml", "models[0].id", "duplicate") in found


def test_registry_dir_absolute_override() -> None:
    override = FIXTURE_ROOT
    old = os.environ.get("FACET_REGISTRY")
    try:
        os.environ["FACET_REGISTRY"] = str(override)
        result = registry_dir()
        assert result == override
    finally:
        if old is None:
            os.environ.pop("FACET_REGISTRY", None)
        else:
            os.environ["FACET_REGISTRY"] = old


def test_registry_dir_relative_override_raises() -> None:
    old = os.environ.get("FACET_REGISTRY")
    try:
        os.environ["FACET_REGISTRY"] = "relative/path"
        with pytest.raises(FacetError) as exc:
            registry_dir()
        assert exc.value.code == "invalid_input"
        assert "absolute" in exc.value.message
    finally:
        if old is None:
            os.environ.pop("FACET_REGISTRY", None)
        else:
            os.environ["FACET_REGISTRY"] = old


def test_registry_dir_uses_recorded_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    snapshot = home / "registry" / "abc123"
    shutil.copytree(FIXTURE_ROOT, snapshot)
    (home / "state").mkdir(parents=True)
    (home / "state" / "registry.json").write_text(
        json.dumps({"commit": "abc123"}), encoding="utf-8"
    )
    monkeypatch.delenv("FACET_REGISTRY", raising=False)
    monkeypatch.setenv("FACET_HOME", str(home))
    assert registry_dir() == snapshot
    assert set(load_active_registry().packages) == {"echo", "termsy", "future"}


def test_registry_dir_corrupt_state_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    (home / "state" / "registry.json").write_text("{not json", encoding="utf-8")
    monkeypatch.delenv("FACET_REGISTRY", raising=False)
    monkeypatch.setenv("FACET_HOME", str(home))
    with pytest.raises(FacetError) as exc:
        registry_dir()
    assert exc.value.code == "internal"
    assert "corrupt registry state" in exc.value.message


def test_registry_dir_no_snapshot_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dead_urls(monkeypatch)
    monkeypatch.delenv("FACET_REGISTRY", raising=False)
    with pytest.raises(FacetError) as exc:
        registry_dir()
    assert exc.value.code == "download_failed"
    assert exc.value.hint == "run facet update when online"
    assert "fetching the registry (first use)" in capsys.readouterr().err
