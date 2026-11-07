"""Backend-owned models: declared without a source, gated per piece.

A ``backend = true`` model (OmniVoice is CC-BY-NC) is never fetched, but its
terms still gate the pieces that select it through a ``model`` input.  The
fixture registry below is a copy of the shared one with three models on the
echo package: one downloadable gated, one backend-owned gated and one
ungated, plus a capability with a ``model`` enum input and one without.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from facetmedia import doctor as doctor_module
from facetmedia.cli import main
from facetmedia.home import Home
from facetmedia.registry import load_registry, validate_registry

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
REAL_SHA = "85a73737b5d85eaba43e73ba33ceade28de49f1f4f7573377f02d58408f715c7"
EXIT_LICENSE_NOT_ACCEPTED = 4
MODEL_BYTES = 21
BOTH_DOWNLOADED_BYTES = 2 * MODEL_BYTES

MODELS_TOML = f"""
[[models]]
id = "dl-gated"
spdx = "CC-BY-NC-4.0"
commercial_use = false
requires_acceptance = true
terms_url = "https://example.invalid/dl-gated"
url = "file://tests/fixtures/models/termsy.bin"
sha256 = "{REAL_SHA}"
bytes = 21
path = "dl-gated/termsy.bin"

[[models]]
id = "backend-gated"
spdx = "CC-BY-NC-4.0"
commercial_use = false
requires_acceptance = true
terms_url = "https://example.invalid/backend-gated"
backend = true

[[models]]
id = "plain-weights"
spdx = "Apache-2.0"
commercial_use = true
requires_acceptance = false
bytes = 21
url = "file://tests/fixtures/models/termsy.bin"
sha256 = "{REAL_SHA}"
path = "plain-weights/termsy.bin"
"""

CAPABILITIES_TOML = """
[capabilities.vox]
summary = "Echo with the selected model."
kind = "text"

[capabilities.vox.inputs.text]
type = "string"
required = true
description = "Text to echo."

[capabilities.vox.inputs.model]
type = "enum"
values = ["dl-gated", "backend-gated", "plain-weights"]
required = true
description = "The model to use."

[capabilities.vox.preview]
viewer = "generic"

[capabilities.plain]
summary = "Echo without a model input."
kind = "text"

[capabilities.plain.inputs.text]
type = "string"
required = true
description = "Text to echo."

[capabilities.plain.preview]
viewer = "generic"
"""


@pytest.fixture(scope="module")
def backend_setup(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """One home with echo installed from a registry copy carrying backend models."""
    root = tmp_path_factory.mktemp("facet-backend")
    registry = root / "registry"
    shutil.copytree(FIXTURES, registry)
    manifest = registry / "packages" / "echo" / "package.toml"
    with manifest.open("a", encoding="utf-8") as handle:
        handle.write(MODELS_TOML + CAPABILITIES_TOML)
    home = root / "home"
    previous = {name: os.environ.get(name) for name in ("FACET_HOME", "FACET_REGISTRY")}
    os.environ["FACET_HOME"] = str(home)
    os.environ["FACET_REGISTRY"] = str(registry)
    try:
        assert main(["license", "accept", "dl-gated", "--yes", "--json"]) == 0
        assert main(["license", "accept", "backend-gated", "--yes", "--json"]) == 0
        assert main(["pull", "echo", "--json"]) == 0
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return home, registry


@pytest.fixture
def backend_home(
    backend_setup: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    home, registry = backend_setup
    monkeypatch.setenv("FACET_HOME", str(home))
    monkeypatch.setenv("FACET_REGISTRY", str(registry))
    return home, registry


@pytest.fixture
def saved_licenses(backend_home: tuple[Path, Path]) -> Iterator[None]:
    """Snapshot the seeded acceptances; tests that forget them restore on exit."""
    home, _registry = backend_home
    path = home / "state" / "licenses.json"
    try:
        saved: bytes | None = path.read_bytes()
    except OSError:
        saved = None
    yield
    if saved is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(saved)


def _forget_licenses(home: Path) -> None:
    (home / "state" / "licenses.json").unlink(missing_ok=True)


def _envelope(capsys: pytest.CaptureFixture[str]) -> tuple[dict[str, Any], str]:
    captured = capsys.readouterr()
    parsed: Any = json.loads(captured.out)
    assert isinstance(parsed, dict)
    return parsed, captured.err


def _piece(directory: Path, model: str) -> Path:
    piece = directory / "piece.toml"
    piece.write_text(f'[inputs]\ntext = "hello"\nmodel = "{model}"\n', encoding="utf-8")
    return piece


def test_ungated_piece_runs_with_no_acceptance(
    capsys: pytest.CaptureFixture[str],
    backend_home: tuple[Path, Path],
    saved_licenses: Iterator[None],
    tmp_path: Path,
) -> None:
    home, _registry = backend_home
    _forget_licenses(home)
    out = tmp_path / "out"

    code = main(
        [
            "run",
            "echo.vox",
            str(_piece(tmp_path, "plain-weights")),
            "--out",
            str(out),
            "--no-verify",
            "--json",
        ]
    )
    envelope, _err = _envelope(capsys)
    assert code == 0
    assert envelope["ok"] is True
    stored = json.loads((out / "echo.txt.facet.json").read_text(encoding="utf-8"))
    assert envelope["data"]["assets"] == [stored]
    assert stored["license"]["accepted"] == []
    models = {entry["id"]: entry for entry in stored["provenance"]["models"]}
    assert models["backend-gated"] == {"id": "backend-gated", "bytes": 0}
    assert not (home / "state" / "licenses.json").exists()


def test_backend_piece_fails_without_acceptance(
    capsys: pytest.CaptureFixture[str],
    backend_home: tuple[Path, Path],
    saved_licenses: Iterator[None],
    tmp_path: Path,
) -> None:
    home, _registry = backend_home
    _forget_licenses(home)
    out = tmp_path / "out"
    piece = _piece(tmp_path, "backend-gated")

    code = main(["run", "echo.vox", str(piece), "--out", str(out), "--no-verify"])
    captured = capsys.readouterr()
    assert code == EXIT_LICENSE_NOT_ACCEPTED
    assert "backend-gated (CC-BY-NC-4.0)" in captured.err
    assert "facet license accept backend-gated" in captured.err

    code = main(
        [
            "run",
            "echo.vox",
            str(piece),
            "--out",
            str(out),
            "--no-verify",
            "--json",
        ]
    )
    envelope, _err = _envelope(capsys)
    assert code == EXIT_LICENSE_NOT_ACCEPTED
    error = envelope["error"]
    assert error["code"] == "license_not_accepted"
    assert "backend-gated (CC-BY-NC-4.0)" in error["message"]
    assert error["hint"] == "facet license accept backend-gated"


def test_backend_piece_runs_after_accept(
    capsys: pytest.CaptureFixture[str],
    backend_home: tuple[Path, Path],
    saved_licenses: Iterator[None],
    tmp_path: Path,
) -> None:
    home, _registry = backend_home
    _forget_licenses(home)
    assert main(["license", "accept", "backend-gated", "--yes", "--json"]) == 0
    capsys.readouterr()
    out = tmp_path / "out"

    code = main(
        [
            "run",
            "echo.vox",
            str(_piece(tmp_path, "backend-gated")),
            "--out",
            str(out),
            "--no-verify",
            "--json",
        ]
    )
    envelope, _err = _envelope(capsys)
    assert code == 0
    stored = json.loads((out / "echo.txt.facet.json").read_text(encoding="utf-8"))
    assert [record["model"] for record in stored["license"]["accepted"]] == ["backend-gated"]


def test_plain_capability_keeps_the_package_gate(
    capsys: pytest.CaptureFixture[str],
    backend_home: tuple[Path, Path],
    saved_licenses: Iterator[None],
    tmp_path: Path,
) -> None:
    home, _registry = backend_home
    _forget_licenses(home)
    piece = tmp_path / "piece.toml"
    piece.write_text('[inputs]\ntext = "hello"\n', encoding="utf-8")

    code = main(
        ["run", "echo.plain", str(piece), "--out", str(tmp_path / "out"), "--no-verify", "--json"]
    )
    envelope, _err = _envelope(capsys)
    assert code == EXIT_LICENSE_NOT_ACCEPTED
    error = envelope["error"]
    assert error["code"] == "license_not_accepted"
    assert error["hint"] == ("facet license accept dl-gated; facet license accept backend-gated")


def test_pull_skips_the_backend_owned_model(
    capsys: pytest.CaptureFixture[str], backend_home: tuple[Path, Path]
) -> None:
    home, _registry = backend_home
    shutil.rmtree(home / "models" / "direct", ignore_errors=True)

    code = main(["pull", "echo", "--json"])
    envelope, err = _envelope(capsys)
    assert code == 0
    assert envelope["ok"] is True
    records = {entry["id"]: entry for entry in envelope["data"]["models"]}
    assert records["backend-gated"]["status"] == "backend-owned"
    assert records["backend-gated"]["bytes"] == 0
    assert records["dl-gated"]["status"] == "downloaded"
    assert envelope["data"]["downloaded_bytes"] == BOTH_DOWNLOADED_BYTES
    assert not (home / "models" / "direct" / "backend-gated").exists()
    assert "model backend-gated backend-owned, skipped" in err

    code = main(["pull", "echo", "--json"])
    envelope, _err = _envelope(capsys)
    assert code == 0
    records = {entry["id"]: entry for entry in envelope["data"]["models"]}
    assert records["backend-gated"]["status"] == "backend-owned"
    assert records["dl-gated"]["status"] == "present"


def test_doctor_reports_backend_owned_without_a_hash_check(
    backend_home: tuple[Path, Path],
    saved_licenses: Iterator[None],
) -> None:
    home, registry = backend_home
    _forget_licenses(home)
    package = load_registry(registry).packages["echo"]

    checks, _failures = doctor_module.machine_checks(package, Home(home))
    assert checks["models"]["ok"] is True
    assert "backend-owned: backend-gated" in str(checks["models"]["detail"])
    assert checks["models"]["fix"] == ""


def test_license_accept_and_list_cover_backend_owned_ids(
    capsys: pytest.CaptureFixture[str],
    backend_home: tuple[Path, Path],
    saved_licenses: Iterator[None],
) -> None:
    home, _registry = backend_home
    _forget_licenses(home)
    assert main(["license", "accept", "backend-gated", "--yes", "--json"]) == 0
    capsys.readouterr()
    assert main(["license", "list", "--json"]) == 0
    envelope, _err = _envelope(capsys)
    rows = {row["id"]: row for row in envelope["data"]["models"]}
    assert rows["backend-gated"]["accepted"] is True
    assert rows["backend-gated"]["method"] == "flag"
    assert rows["dl-gated"]["accepted"] is False


def test_manifest_rejects_a_backend_model_with_a_source(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    root.mkdir()
    (root / "index.toml").write_text('version = 1\n[[packages]]\nname = "pkg"\n', encoding="utf-8")
    pkg_dir = root / "packages" / "pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.toml").write_text(
        """name = "pkg"
version = "0.1.0"
requires_facet = ">=0.1.0"
protocol = 1
provider = "test"
tool_name = "Test"
description = "a test package"
facet = "text"

[engine]
type = "python-env"
python = "3.12"

[transport]
type = "cli"
entry = "runner.py"

[requirements]
gpu = false
ram_gb = 1
disk_gb = 1

[[models]]
id = "m1"
spdx = "CC-BY-NC-4.0"
commercial_use = false
requires_acceptance = true
terms_url = "https://example.invalid/m1"
backend = true
url = "https://example.com/m1.bin"
sha256 = "QUOTE"
bytes = 10
path = "m1.bin"
""".replace("QUOTE", "a" * 64),
        encoding="utf-8",
    )
    for name in ("pyproject.toml", "uv.lock", "runner.py"):
        (pkg_dir / name).write_text("", encoding="utf-8")
    problems = {
        (problem["file"], problem["key"], problem["problem"]) for problem in validate_registry(root)
    }
    assert ("packages/pkg/package.toml", "models[0].url", "value") in problems


def test_manifest_requires_terms_url_for_a_gated_backend_model(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    root.mkdir()
    (root / "index.toml").write_text('version = 1\n[[packages]]\nname = "pkg"\n', encoding="utf-8")
    pkg_dir = root / "packages" / "pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.toml").write_text(
        """name = "pkg"
version = "0.1.0"
requires_facet = ">=0.1.0"
protocol = 1
provider = "test"
tool_name = "Test"
description = "a test package"
facet = "text"

[engine]
type = "python-env"
python = "3.12"

[transport]
type = "cli"
entry = "runner.py"

[requirements]
gpu = false
ram_gb = 1
disk_gb = 1

[[models]]
id = "m1"
spdx = "CC-BY-NC-4.0"
commercial_use = false
requires_acceptance = true
backend = true
""",
        encoding="utf-8",
    )
    for name in ("pyproject.toml", "uv.lock", "runner.py"):
        (pkg_dir / name).write_text("", encoding="utf-8")
    problems = {
        (problem["file"], problem["key"], problem["problem"]) for problem in validate_registry(root)
    }
    assert ("packages/pkg/package.toml", "models[0].terms_url", "required") in problems
