import json
import re
import tempfile
from pathlib import Path

import pytest

from facetmedia.cli import main
from facetmedia.registry import validate_registry

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "registry"
EXIT_INVALID = 2
MIN_BROKEN_PROBLEMS = 3

MANIFEST_TEMPLATE = """name = "missing"
version = "0.1.0"
requires_facet = ">=0.1.0"
protocol = 1
provider = "test"
tool_name = "Test"
description = "test"
facet = "text"

[engine]
type = "python-env"
python = "3.12"

[transport]
type = "cli"
entry = "runner.py"

[requirements]
gpu = false
ram_gb = 0.1
disk_gb = 0.01
exclusive = false
platforms = ["linux"]
host_libs = []
host_hint = ""

[[licenses]]
subject = "test"
spdx = "Apache-2.0"
"""

SKILL_TEMPLATE = """---
name: {name}
description: A valid test skill for the {name} package fixture.
---
# {title}

Body mentions {capabilities}.

## When to use

Use it in tests.

## Pieces

```toml
[inputs]
```

Pieces for {capabilities}.

## Judging the result

Verify measures nothing; read the generic preview fields.

## Pitfalls

- None.
"""


DOCTOR_STUB = '''"""Test runner stub: only the decorator the validator looks for."""

from __future__ import annotations

from typing import Any

from facet_runner_sdk import Request, Runner

runner = Runner()


@runner.verb("doctor")
def doctor(req: Request) -> dict[str, Any]:
    return {"checks": [{"name": "runner", "ok": True, "detail": "ready", "fix": ""}]}


runner.main()
'''


def _problems(root: Path) -> set[tuple[str, str, str]]:
    return {(p["file"], p["key"], p["problem"]) for p in validate_registry(root)}


def _write_package(root: Path, manifest: str) -> Path:
    (root / "index.toml").write_text(
        'version = 1\n[[packages]]\nname = "missing"\n', encoding="utf-8"
    )
    pkg_dir = root / "packages" / "missing"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.toml").write_text(manifest, encoding="utf-8")
    capabilities = sorted(set(re.findall(r"\[capabilities\.([a-z0-9-]+)\]", manifest)))
    cap_ids = ", ".join(f"missing.{cap}" for cap in capabilities) or "no capabilities"
    skill_dir = pkg_dir / "skill"
    skill_dir.mkdir(exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        SKILL_TEMPLATE.format(name="missing", title="Missing", capabilities=cap_ids),
        encoding="utf-8",
    )
    return pkg_dir


def test_validate_clean_exit_0() -> None:
    assert main(["registry", "validate", str(FIXTURE_ROOT)]) == 0


def test_validate_clean_json_output(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["registry", "validate", str(FIXTURE_ROOT), "--json"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert envelope["data"]["valid"] is True
    assert envelope["data"]["packages"] == ["echo", "future", "termsy"]


def test_validate_broken_exit_2() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "index.toml").write_text(
            'version = 1\n[[packages]]\nname = "bad"\n', encoding="utf-8"
        )
        pkg_dir = root / "packages" / "bad"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "package.toml").write_text('name = "bad"\nversion = "x"\n', encoding="utf-8")
        assert main(["registry", "validate", str(root)]) == EXIT_INVALID


def test_validate_broken_json_errors_in_details(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "index.toml").write_text(
        'version = 1\n[[packages]]\nname = "bad"\n[[packages]]\nname = "also"\n',
        encoding="utf-8",
    )
    for name in ["bad", "also"]:
        pkg_dir = tmp_path / "packages" / name
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "package.toml").write_text(
            f'name = "{name}"\nversion = "x"\nunknown = "key"\n', encoding="utf-8"
        )
    assert main(["registry", "validate", str(tmp_path), "--json"]) == EXIT_INVALID
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "invalid_input"
    errors = envelope["error"]["details"]["errors"]
    found = {(e["file"], e["key"], e["problem"]) for e in errors}
    for name in ["bad", "also"]:
        file = f"packages/{name}/package.toml"
        assert (file, "unknown", "unknown") in found
        assert (file, "version", "value") in found
        assert (file, "description", "required") in found
    assert len(errors) >= MIN_BROKEN_PROBLEMS


def test_validate_broken_manifest_still_reports_missing_files(tmp_path: Path) -> None:
    (tmp_path / "index.toml").write_text(
        'version = 1\n[[packages]]\nname = "bad"\n', encoding="utf-8"
    )
    pkg_dir = tmp_path / "packages" / "bad"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.toml").write_text('name = "bad"\nversion = "x"\n', encoding="utf-8")
    found = {(p["file"], p["key"], p["problem"]) for p in validate_registry(tmp_path)}
    assert ("packages/bad/pyproject.toml", "", "missing") in found
    assert ("packages/bad/uv.lock", "", "missing") in found


def test_validate_missing_pyproject_toml(tmp_path: Path) -> None:
    pkg_dir = _write_package(tmp_path, MANIFEST_TEMPLATE)
    (pkg_dir / "uv.lock").write_text("", encoding="utf-8")
    (pkg_dir / "runner.py").write_text(DOCTOR_STUB, encoding="utf-8")
    assert _problems(tmp_path) == {("packages/missing/pyproject.toml", "", "missing")}
    assert main(["registry", "validate", str(tmp_path)]) == EXIT_INVALID


def test_validate_missing_uv_lock(tmp_path: Path) -> None:
    pkg_dir = _write_package(tmp_path, MANIFEST_TEMPLATE)
    (pkg_dir / "pyproject.toml").write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (pkg_dir / "runner.py").write_text(DOCTOR_STUB, encoding="utf-8")
    assert _problems(tmp_path) == {("packages/missing/uv.lock", "", "missing")}
    assert main(["registry", "validate", str(tmp_path)]) == EXIT_INVALID


def test_validate_missing_runner_entry(tmp_path: Path) -> None:
    pkg_dir = _write_package(tmp_path, MANIFEST_TEMPLATE)
    (pkg_dir / "pyproject.toml").write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (pkg_dir / "uv.lock").write_text("", encoding="utf-8")
    assert _problems(tmp_path) == {("packages/missing/runner.py", "", "missing")}
    assert main(["registry", "validate", str(tmp_path)]) == EXIT_INVALID


def test_validate_missing_viewer_directory(tmp_path: Path) -> None:
    manifest_with_viewer = (
        MANIFEST_TEMPLATE
        + """
[capabilities.test]
summary = "test"
kind = "text"

[capabilities.test.preview]
viewer = "missing"
"""
    )
    pkg_dir = _write_package(tmp_path, manifest_with_viewer)
    (pkg_dir / "pyproject.toml").write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (pkg_dir / "uv.lock").write_text("", encoding="utf-8")
    (pkg_dir / "runner.py").write_text(DOCTOR_STUB, encoding="utf-8")
    assert _problems(tmp_path) == {("viewers/missing/index.html", "", "missing")}
    assert main(["registry", "validate", str(tmp_path)]) == EXIT_INVALID


def _complete_package(tmp_path: Path, runner_text: str) -> Path:
    """A package whose environment files and manifest are complete."""
    pkg_dir = _write_package(tmp_path, MANIFEST_TEMPLATE)
    (pkg_dir / "pyproject.toml").write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (pkg_dir / "uv.lock").write_text("", encoding="utf-8")
    (pkg_dir / "runner.py").write_text(runner_text, encoding="utf-8")
    return pkg_dir


def test_validate_rejects_a_runner_without_a_doctor_verb(tmp_path: Path) -> None:
    without_doctor = DOCTOR_STUB.replace('@runner.verb("doctor")', '@runner.verb("run")')
    _complete_package(tmp_path, without_doctor)
    found = _problems(tmp_path)
    assert ("packages/missing/runner.py", "runner.verb", "missing doctor verb") in found
    assert main(["registry", "validate", str(tmp_path)]) == EXIT_INVALID


def test_validate_rejects_a_runner_that_still_registers_probe(tmp_path: Path) -> None:
    probe_runner = (
        DOCTOR_STUB + '\n@runner.verb("probe")\ndef probe(req: object) -> dict:\n    return {}\n'
    )
    _complete_package(tmp_path, probe_runner)
    found = _problems(tmp_path)
    assert ("packages/missing/runner.py", "runner.verb", "rename probe to doctor") in found
    assert main(["registry", "validate", str(tmp_path)]) == EXIT_INVALID
