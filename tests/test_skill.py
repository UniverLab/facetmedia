"""``facet skill``: the core skill, each package skill, and its validation."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from facetmedia import introspect as introspect_module
from facetmedia import skill as skill_module
from facetmedia.cli import main
from facetmedia.contract import FacetError
from facetmedia.doctor import CHECK_ORDER
from facetmedia.home import Home
from facetmedia.registry import load_registry, validate_registry
from facetmedia.registry_types import Model, Package, Registry, Requirements, weight

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
EXIT_INVALID_INPUT = 2
EXIT_DOWNLOAD_FAILED = 8
MAX_CORE_SKILL_LINES = 150

MANIFEST = """name = "pkg"
version = "0.1.0"
requires_facet = ">=0.1.0"
protocol = 1
provider = "test"
tool_name = "Test"
description = "test"
facet = "voice"

[engine]
type = "python-env"
python = "3.12"

[transport]
type = "cli"
entry = "runner.py"

[requirements]
gpu = false
exclusive = false

[[licenses]]
subject = "test"
spdx = "Apache-2.0"

[capabilities.speak]
summary = "speaks"
kind = "audio"
"""

VALID_SKILL = """---
name: pkg
description: Speak test audio for the skill validation fixture suite.
---
# Pkg

The pkg.speak capability speaks.

## When to use

Use it in tests.

## Pieces

```toml
[inputs]
```

Pieces for pkg.speak.

## Judging the result

Verify measures nothing; read the generic preview fields.

## Pitfalls

- None.
"""

DOCTOR_RUNNER = '''"""Test runner stub: only the decorator the validator looks for."""

from __future__ import annotations

from typing import Any


@runner.verb("doctor")
def doctor(req: object) -> dict[str, Any]:
    return {"checks": [{"name": "runner", "ok": True, "detail": "ready", "fix": ""}]}
'''


def _registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))


def _data(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    envelope: Any = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    data = envelope["data"]
    assert isinstance(data, dict)
    return data


def _error(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    envelope: Any = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is False
    error = envelope["error"]
    assert isinstance(error, dict)
    return error


def _package(
    name: str = "pkg", *, gpu: bool = False, exclusive: bool = False, model_bytes: int = 0
) -> Package:
    models = (
        (
            Model(
                id="m",
                spdx="MIT",
                commercial_use=True,
                requires_acceptance=False,
                bytes=model_bytes,
                url="https://example.invalid/m.bin",
                sha256="a" * 64,
                path="m.bin",
            ),
        )
        if model_bytes
        else ()
    )
    return Package(
        name=name,
        version="0.1.0",
        requires_facet=">=0.1.0",
        protocol=1,
        provider="test",
        tool_name="Test",
        description="test",
        facet="voice",
        engine_type="python-env",
        python="3.12",
        transport_type="cli",
        entry="runner.py",
        invocation_template="{{python}}",
        requirements=Requirements(gpu=gpu, exclusive=exclusive),
        directory=Path("packages") / name,
        licenses=(),
        models=models,
        capabilities=(),
    )


# --- weight: one case per branch, with the 100 MB boundary on both sides.


def test_weight_gpu_wins_over_everything() -> None:
    assert weight(_package(gpu=True, exclusive=True, model_bytes=10**9)) == "gpu"


def test_weight_exclusive_without_models_is_cpu_heavy() -> None:
    assert weight(_package(exclusive=True)) == "cpu-heavy"


def test_weight_exactly_100mb_is_light() -> None:
    assert weight(_package(model_bytes=100_000_000)) == "light"


def test_weight_one_byte_over_100mb_is_cpu_heavy() -> None:
    assert weight(_package(model_bytes=100_000_001)) == "cpu-heavy"


def test_weight_shared_small_is_light() -> None:
    assert weight(_package()) == "light"


# --- manifest facet problems, via the registry validator.


def _problems(manifest: str, root: Path) -> set[tuple[str, str, str]]:
    (root / "index.toml").write_text('version = 1\n[[packages]]\nname = "pkg"\n', encoding="utf-8")
    pkg_dir = root / "packages" / "pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.toml").write_text(manifest, encoding="utf-8")
    for name in ("pyproject.toml", "uv.lock", "runner.py"):
        (pkg_dir / name).write_text("", encoding="utf-8")
    return {(p["file"], p["key"], p["problem"]) for p in validate_registry(root)}


def test_manifest_missing_facet_is_rejected(tmp_path: Path) -> None:
    manifest = MANIFEST.replace('facet = "voice"\n', "")
    assert ("packages/pkg/package.toml", "facet", "required") in _problems(manifest, tmp_path)


def test_manifest_unknown_facet_names_the_allowed_values(tmp_path: Path) -> None:
    manifest = MANIFEST.replace('facet = "voice"', 'facet = "song"')
    allowed = "value: one of voice, audio, image, video, cad, text, presentation"
    assert ("packages/pkg/package.toml", "facet", allowed) in _problems(manifest, tmp_path)


# --- skill validation: one problem per violation.


def _skill_registry(tmp_path: Path, text: str | None) -> set[tuple[str, str, str]]:
    (tmp_path / "index.toml").write_text(
        'version = 1\n[[packages]]\nname = "pkg"\n', encoding="utf-8"
    )
    pkg_dir = tmp_path / "packages" / "pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.toml").write_text(MANIFEST, encoding="utf-8")
    for name in ("pyproject.toml", "uv.lock"):
        (pkg_dir / name).write_text("", encoding="utf-8")
    (pkg_dir / "runner.py").write_text(DOCTOR_RUNNER, encoding="utf-8")
    viewer = tmp_path / "viewers" / "generic"
    viewer.mkdir(parents=True)
    (viewer / "index.html").write_text("", encoding="utf-8")
    if text is not None:
        skill_dir = pkg_dir / "skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    return {(p["file"], p["key"], p["problem"]) for p in validate_registry(tmp_path)}


def test_skill_valid_is_clean(tmp_path: Path) -> None:
    assert _skill_registry(tmp_path, VALID_SKILL) == set()


def test_skill_missing_is_rejected(tmp_path: Path) -> None:
    assert _skill_registry(tmp_path, None) == {("packages/pkg/skill/SKILL.md", "skill", "missing")}


def test_skill_wrong_name_is_rejected(tmp_path: Path) -> None:
    found = _skill_registry(tmp_path, VALID_SKILL.replace("name: pkg", "name: other"))
    assert found == {("packages/pkg/skill/SKILL.md", "skill.name", "value")}


def test_skill_short_description_is_rejected(tmp_path: Path) -> None:
    text = VALID_SKILL.replace(
        "description: Speak test audio for the skill validation fixture suite.",
        "description: Speak.",
    )
    found = _skill_registry(tmp_path, text)
    assert found == {("packages/pkg/skill/SKILL.md", "skill.description", "value")}


def test_skill_long_description_is_rejected(tmp_path: Path) -> None:
    text = VALID_SKILL.replace(
        "description: Speak test audio for the skill validation fixture suite.",
        "description: " + "x" * 201,
    )
    found = _skill_registry(tmp_path, text)
    assert found == {("packages/pkg/skill/SKILL.md", "skill.description", "value")}


def test_skill_missing_heading_is_rejected(tmp_path: Path) -> None:
    found = _skill_registry(tmp_path, VALID_SKILL.replace("## Pitfalls\n", ""))
    assert ("packages/pkg/skill/SKILL.md", "skill.headings", "missing ## Pitfalls") in found


def test_skill_headings_out_of_order_are_rejected(tmp_path: Path) -> None:
    text = VALID_SKILL.replace("## When to use", "## TMP").replace("## Pitfalls", "## When to use")
    text = text.replace("## TMP", "## Pitfalls")
    found = _skill_registry(tmp_path, text)
    assert ("packages/pkg/skill/SKILL.md", "skill.headings", "order") in found


def test_skill_missing_capability_id_is_rejected(tmp_path: Path) -> None:
    found = _skill_registry(tmp_path, VALID_SKILL.replace("pkg.speak", "something else"))
    assert ("packages/pkg/skill/SKILL.md", "skill.capability.pkg.speak", "missing") in found


def test_skill_broken_frontmatter_is_rejected(tmp_path: Path) -> None:
    found = _skill_registry(tmp_path, "no front matter here\n## When to use\n")
    assert found == {("packages/pkg/skill/SKILL.md", "skill.frontmatter", "value")}


# --- `facet skill` with no argument: the core skill plus the registry.


def test_skill_core_lists_fixtures_by_facet(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    assert main(["skill"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("---\nname: facet\n")
    assert "## Packages in this registry" in out
    assert "### text" in out
    assert "- echo (light): echo.text" in out
    assert "Registry" in out and str(FIXTURES) in out


def test_skill_core_json_shape(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    assert main(["skill", "--json"]) == 0
    data = _data(capsys)
    assert set(data) == {
        "scope",
        "name",
        "facet",
        "weight",
        "description",
        "body",
        "references",
        "state",
    }
    assert data["scope"] == "core"
    assert data["name"] == "facet"
    assert data["facet"] is None
    assert data["weight"] is None
    assert data["references"] == []
    facets = data["state"]["facets"]
    assert [package["name"] for package in facets["text"]] == ["echo", "future", "termsy"]
    assert [package["weight"] for package in facets["text"]] == ["light", "cpu-heavy", "cpu-heavy"]
    assert data["body"].rstrip("\n").endswith(facets["text"][-1]["description"])
    assert data["state"]["registry_error"] is None


def _loader_fails(monkeypatch: pytest.MonkeyPatch, error: FacetError) -> None:
    def failing() -> Registry:
        raise error

    monkeypatch.setattr(introspect_module, "load_active_registry", failing)


def test_skill_core_fetch_failure_still_prints(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _loader_fails(monkeypatch, FacetError("download_failed", "HTTP 403"))
    assert main(["skill"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("---\nname: facet\n")
    assert "## Packages in this registry" in out
    assert "Registry not available: HTTP 403. Run `facet update`, then `facet skill` again." in out
    assert "- echo" not in out
    assert main(["skill", "--json"]) == 0
    data = _data(capsys)
    assert data["state"] == {
        "registry": None,
        "facets": {},
        "registry_error": {"code": "download_failed", "message": "HTTP 403"},
    }


def test_skill_core_invalid_snapshot_still_prints(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _loader_fails(monkeypatch, FacetError("internal", "index.toml: : missing"))
    assert main(["skill"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("---\nname: facet\n")
    assert "## Packages in this registry" in out
    assert (
        "Registry not available: index.toml: : missing. "
        "Run `facet update`, then `facet skill` again."
    ) in out
    assert main(["skill", "--json"]) == 0
    data = _data(capsys)
    assert data["state"] == {
        "registry": None,
        "facets": {},
        "registry_error": {"code": "internal", "message": "index.toml: : missing"},
    }


def test_skill_package_target_still_needs_registry(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _loader_fails(monkeypatch, FacetError("download_failed", "HTTP 403"))
    assert main(["skill", "echo", "--json"]) == EXIT_DOWNLOAD_FAILED
    assert _error(capsys)["code"] == "download_failed"


# --- `facet skill <package|capability>`: the skill plus the machine section.


def _fake_install_echo() -> None:
    echo = load_registry(FIXTURES).package("echo")
    assert echo is not None
    env_dir = Home.peek_env().env_dir(echo.name, echo.version)
    env_dir.mkdir(parents=True)
    (env_dir / ".complete").write_text(echo.lock_sha256, encoding="utf-8")


def test_skill_echo_not_installed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    assert main(["skill", "echo"]) == 0
    out = capsys.readouterr().out
    assert "name: echo" in out
    assert "echo.text" in out
    assert out.split("## On this machine")[1].startswith("\n\n- installed: no")
    assert "`facet pull echo`" in out
    assert main(["skill", "echo", "--json"]) == 0
    data = _data(capsys)
    assert data["scope"] == "package"
    assert (data["name"], data["facet"], data["weight"]) == ("echo", "text", "light")
    assert data["references"] == ["batching"]
    assert data["state"]["installed"] is False
    assert data["state"]["download_bytes"] == 0
    assert data["state"]["doctor"] is None
    assert data["body"] == out


def test_skill_capability_id_names_the_capability(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    assert main(["skill", "echo.text"]) == 0
    out = capsys.readouterr().out
    assert "## On this machine (echo.text)" in out
    assert main(["skill", "echo.text", "--json"]) == 0
    data = _data(capsys)
    assert data["name"] == "echo"


def test_skill_termsy_reports_licence_state(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    assert main(["skill", "termsy", "--no-doctor"]) == 0
    out = capsys.readouterr().out
    assert "- installed: no" in out
    assert "`facet pull termsy` (21 bytes)" in out
    assert "- termsy-weights: needs `facet license accept termsy-weights`." in out
    assert main(["skill", "termsy", "--no-doctor", "--json"]) == 0
    models = _data(capsys)["state"]["models"]
    assert [(row["id"], row["accepted"]) for row in models] == [("termsy-weights", False)]


@pytest.mark.parametrize("installed", [False, True], ids=["not-installed", "installed"])
@pytest.mark.parametrize("no_doctor", [False, True], ids=["doctor", "no-doctor"])
@pytest.mark.parametrize("target", ["echo", "echo.text"])
def test_skill_target_matrix(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    installed: bool,
    no_doctor: bool,
    target: str,
) -> None:
    """A package and a capability id, installed and not, with and without the doctor."""
    _registry(monkeypatch)
    if installed:
        _fake_install_echo()
    argv = ["skill", target, "--json"] + (["--no-doctor"] if no_doctor else [])
    assert main(argv) == 0
    data = _data(capsys)
    assert set(data) == {
        "scope",
        "name",
        "facet",
        "weight",
        "description",
        "body",
        "references",
        "state",
    }
    assert (data["scope"], data["name"], data["facet"], data["weight"]) == (
        "package",
        "echo",
        "text",
        "light",
    )
    state = data["state"]
    assert set(state) == {"installed", "download_bytes", "models", "doctor"}
    assert state["installed"] is installed
    expected_doctor = installed and not no_doctor
    assert (state["doctor"] is not None) is expected_doctor
    heading = "## On this machine (echo.text)" if target == "echo.text" else "## On this machine"
    assert heading in data["body"]
    assert data["body"].startswith("---\nname: echo\n")
    human = ["skill", target] + (["--no-doctor"] if no_doctor else [])
    assert main(human) == 0
    out = capsys.readouterr().out
    assert heading in out
    assert ("Quick doctor:" in out) is expected_doctor
    assert ("- installed: yes." in out) is installed


def test_skill_echo_installed_runs_the_quick_doctor(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    _fake_install_echo()
    assert main(["skill", "echo", "--json"]) == 0
    state = _data(capsys)["state"]
    assert state["installed"] is True
    assert state["download_bytes"] == 0
    assert state["doctor"] is not None
    assert [entry["name"] for entry in state["doctor"]] == [*CHECK_ORDER, "doctor"]
    assert all(set(entry) == {"name", "ok", "detail", "fix"} for entry in state["doctor"])
    assert main(["skill", "echo"]) == 0
    out = capsys.readouterr().out
    assert "- installed: yes." in out
    assert "Quick doctor:" in out
    assert main(["skill", "echo", "--no-doctor", "--json"]) == 0
    assert _data(capsys)["state"]["doctor"] is None


# --- references, errors, and the no-write rule.


def test_skill_reference_known_and_unknown(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    assert main(["skill", "echo", "--reference", "batching"]) == 0
    expected = FIXTURES / "packages" / "echo" / "skill" / "references" / "batching.md"
    assert capsys.readouterr().out == expected.read_text(encoding="utf-8")
    assert main(["skill", "echo", "--reference", "nope", "--json"]) == EXIT_INVALID_INPUT
    error = _error(capsys)
    assert error["code"] == "invalid_input"
    assert error["details"]["known"] == ["batching"]


def test_skill_unknown_target_lists_names(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    assert main(["skill", "nope", "--json"]) == EXIT_INVALID_INPUT
    error = _error(capsys)
    assert error["code"] == "invalid_input"
    assert "echo.text" in error["details"]["known"]
    assert "echo" in error["details"]["known"]


def test_skill_reference_on_core_is_invalid(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    assert main(["skill", "--reference", "batching", "--json"]) == EXIT_INVALID_INPUT
    assert _error(capsys)["details"]["known"] == []


def test_skill_writes_nothing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """HOME and FACET_HOME gain no file but the run's own log."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FACET_HOME", str(tmp_path / "facet-home"))
    _registry(monkeypatch)

    def listed(root: Path) -> set[str]:
        return {str(path) for path in root.rglob("*") if path.is_file()} if root.exists() else set()

    before = listed(home) | listed(Path(os.environ["FACET_HOME"]))
    assert main(["skill"]) == 0
    capsys.readouterr()
    assert main(["skill", "echo"]) == 0
    capsys.readouterr()
    assert main(["skill", "echo", "--json"]) == 0
    capsys.readouterr()
    assert main(["skill", "termsy", "--no-doctor"]) == 0
    capsys.readouterr()
    assert main(["list", "--available"]) == 0
    capsys.readouterr()
    after = listed(home) | listed(Path(os.environ["FACET_HOME"]))
    new = sorted(after - before)
    assert new, "every run writes its own log file"
    assert [path for path in new if "/logs/" not in path] == []
    assert [path for path in new if str(home) in path] == []


def test_core_skill_fits_in_150_lines() -> None:
    assert len(skill_module.core_skill_text().splitlines()) < MAX_CORE_SKILL_LINES


def test_fixture_packages_carry_facets() -> None:
    registry = load_registry(FIXTURES)
    assert {name: package.facet for name, package in registry.packages.items()} == {
        "echo": "text",
        "future": "text",
        "termsy": "text",
    }
    termsy = registry.package("termsy")
    echo = registry.package("echo")
    assert termsy is not None and echo is not None
    assert weight(termsy) == "cpu-heavy"
    assert weight(echo) == "light"
