import hashlib
import os
import tempfile
from pathlib import Path

import pytest

from facetmedia.contract import FacetError
from facetmedia.registry import (
    PROTOCOL_VERSION,
    load_registry,
    registry_dir,
    validate_registry,
)

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
    assert echo.requires_facet == ">=0.1.0.dev0,<0.2"
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


def test_registry_dir_no_snapshot_raises() -> None:
    old_home = os.environ.get("FACET_HOME")
    old_registry = os.environ.get("FACET_REGISTRY")
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            os.environ["FACET_HOME"] = str(home)
            os.environ.pop("FACET_REGISTRY", None)
            with pytest.raises(FacetError) as exc:
                registry_dir()
            assert exc.value.code == "download_failed"
            assert "facet update" in exc.value.hint
    finally:
        if old_home is None:
            os.environ.pop("FACET_HOME", None)
        else:
            os.environ["FACET_HOME"] = old_home
        if old_registry is None:
            os.environ.pop("FACET_REGISTRY", None)
        else:
            os.environ["FACET_REGISTRY"] = old_registry
