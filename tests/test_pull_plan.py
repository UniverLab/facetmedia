"""``facet pull``'s plan: facets, tiers, confirmation, disk and the JSON shape.

Everything here builds its own tiny registry and patches the probes and
``env.pull``; nothing downloads and no real environment is built.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from facetmedia import plan
from facetmedia.cli import main
from facetmedia.contract import EXIT_CODES, FacetError
from facetmedia.env.package_marker import package_fingerprint
from facetmedia.home import Home
from facetmedia.registry import load_registry, validate_registry

GB = 1024**3
IMAGE = "image"
EXIT_MACHINE_CANNOT = 5
EXIT_CONFIRMATION = 11
TWO_PULLS_BYTES = 20
ONE_PULL_BYTES = 10
DISK_NEEDED_GB = 8
DISK_FREE_GB = 1


@dataclass
class Spec:
    """The manifest knobs a test sets on the packages it writes."""

    facet: str = IMAGE
    gpu: bool = False
    vram_gb: float = 0.0
    ram_gb: float = 0.0
    disk_gb: float = 1.0
    platforms: tuple[str, ...] = ("linux",)
    tier_group: str = ""
    tier: str = ""
    host_app: bool = False


def write_package(root: Path, name: str, spec: Spec | None = None) -> None:
    """One package directory with the smallest manifest the loader accepts."""
    spec = spec or Spec()
    directory = root / "packages" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.1.0"\ndependencies = []\n',
        encoding="utf-8",
    )
    (directory / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    lines = [
        f'name = "{name}"',
        'version = "0.1.0"',
        'requires_facet = ">=0.1.0"',
        "protocol = 1",
        'provider = "test"',
        'tool_name = "Test"',
        'description = "a test package"',
        f'facet = "{spec.facet}"',
    ]
    if spec.tier_group:
        lines += [f'tier_group = "{spec.tier_group}"', f'tier = "{spec.tier}"']
    if spec.host_app:
        lines += ["", "[engine]", 'type = "host-app"']
    else:
        lines += ["", "[engine]", 'type = "python-env"', 'python = "3.12"']
    if spec.host_app:
        lines += [
            "",
            "[engine.host_app]",
            'app = "blender"',
            'min_version = "4.0"',
            'config_key = "blender.path"',
            'env = "BLENDER_PATH"',
        ]
    listed = ", ".join(f'"{item}"' for item in spec.platforms)
    lines += [
        "",
        "[transport]",
        'type = "cli"',
        'entry = "runner.py"',
        "",
        "[requirements]",
        f"gpu = {str(spec.gpu).lower()}",
        f"vram_gb = {spec.vram_gb}",
        f"ram_gb = {spec.ram_gb}",
        f"disk_gb = {spec.disk_gb}",
        f"platforms = [{listed}]",
        "",
    ]
    (directory / "package.toml").write_text("\n".join(lines), encoding="utf-8")


def write_registry(root: Path, packages: list[str], spec: Spec | None = None) -> Path:
    """An index over ``packages``, each written with the same spec."""
    root.mkdir(parents=True)
    for name in packages:
        write_package(root, name, spec)
    listed = "\n\n".join(f'[[packages]]\nname = "{name}"' for name in packages)
    (root / "index.toml").write_text(f"version = 1\n\n{listed}\n", encoding="utf-8")
    return root


@pytest.fixture
def use_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point facet at a registry built inside the test."""

    def select(packages: list[str], spec: Spec | None = None) -> Path:
        root = write_registry(tmp_path / "registry", packages, spec)
        monkeypatch.setenv("FACET_REGISTRY", str(root))
        return root

    return select


def set_machine(monkeypatch: pytest.MonkeyPatch, gpu_mib: int | None, ram_gb: float | None) -> None:
    monkeypatch.setattr(plan.probes, "gpu_total_mib", lambda: gpu_mib)
    monkeypatch.setattr(plan.probes, "mem_total_gb", lambda: ram_gb)


def fake_pull(monkeypatch: pytest.MonkeyPatch, calls: list[str], fail: str = "") -> None:
    """Record each pull and return a report shaped like ``env.pull``'s."""

    def pull(package, home, verify_models=False, run_id="", progress=None):
        calls.append(package.name)
        if package.name == fail:
            raise FacetError("download_failed", f"{package.name} broke")
        return {
            "package": package.name,
            "version": package.version,
            "env": "env",
            "python": "3.12",
            "already_installed": False,
            "downloaded_bytes": 10,
            "seconds": 0.0,
            "models": [],
        }

    monkeypatch.setattr(plan.env, "pull", pull)


def plan_of(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, object], list]:
    code = main([*argv, "--json"])
    envelope = json.loads(capsys.readouterr().out)
    assert isinstance(envelope, dict)
    data = envelope.get("data")
    assert isinstance(data, dict)
    entries = data.get("plan")
    assert isinstance(entries, list)
    return code, envelope, entries


def by_name(entries: list) -> dict[str, dict[str, object]]:
    return {str(entry["name"]): entry for entry in entries}


def make_installed(home: Home, registry_root: Path, name: str) -> None:
    """The markers an installed package carries, so the plan sees a no-op."""
    package = load_registry(registry_root).packages[name]
    env_dir = home.env_dir(package.name, package.version)
    env_dir.mkdir(parents=True)
    (env_dir / ".complete").write_text(package.lock_sha256, encoding="utf-8")
    (env_dir / ".package").write_text(package_fingerprint(package), encoding="utf-8")


def test_every_skip_rule_names_its_reason(
    use_registry, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_registry(["wrongplatform", "bigvram", "bigram"], Spec(gpu=True, vram_gb=24))
    write_package(_root(), "wrongplatform", Spec(platforms=("win32",)))
    write_package(_root(), "bigram", Spec(ram_gb=64))
    set_machine(monkeypatch, gpu_mib=8192, ram_gb=8.0)
    code, envelope, entries = plan_of(capsys, "pull", IMAGE, "--plan")
    found = by_name(entries)
    assert code == 0
    assert found["wrongplatform"]["install"] is False
    assert "does not support this platform" in str(found["wrongplatform"]["reason"])
    assert found["bigvram"]["reason"] == "needs 24 GB VRAM, this machine has 8.00 GB"
    assert found["bigram"]["reason"] == "needs 64 GB RAM, this machine has 8.00 GB"
    assert envelope["ok"] is True


def test_gpu_absent_skips_a_gpu_package(use_registry, monkeypatch, capsys) -> None:
    use_registry(["gpuonly"], Spec(gpu=True))
    set_machine(monkeypatch, gpu_mib=None, ram_gb=8.0)
    _code, _envelope, entries = plan_of(capsys, "pull", IMAGE, "--plan")
    assert entries[0]["install"] is False
    assert entries[0]["reason"] == "needs an NVIDIA GPU"


def test_cpu_skips_gpu_package(use_registry, monkeypatch, capsys) -> None:
    use_registry(["gpu"], Spec(gpu=True))
    set_machine(monkeypatch, gpu_mib=8192, ram_gb=64.0)
    code, _envelope, entries = plan_of(capsys, "pull", IMAGE, "--cpu", "--plan")
    assert code == 0
    assert entries[0]["install"] is False
    assert entries[0]["reason"] == "needs a GPU and --cpu was given"


def test_host_app_skip_carries_the_exact_config_command(use_registry, monkeypatch, capsys) -> None:
    use_registry(["blendery"], Spec(host_app=True))
    set_machine(monkeypatch, gpu_mib=None, ram_gb=8.0)
    fix = "facet config set blender.path /path/to/blender"
    monkeypatch.setattr(
        plan.config,
        "host_app_problem",
        lambda app, home: SimpleNamespace(message="not found", fix=fix, details={}),
    )
    _code, _envelope, entries = plan_of(capsys, "pull", IMAGE, "--plan")
    assert entries[0]["install"] is False
    assert entries[0]["reason"] == fix


def test_auto_tier_choice_is_the_highest_fitting_vram(use_registry, monkeypatch, capsys) -> None:
    root = use_registry(["comfy", "comfy-xl"])
    write_package(root, "comfy-xl", Spec(gpu=True, vram_gb=24, tier_group="comfy", tier="xl"))
    _mark_tier(root, "comfy", "standard")
    set_machine(monkeypatch, gpu_mib=32 * 1024, ram_gb=64.0)
    _code, _envelope, entries = plan_of(capsys, "pull", IMAGE, "--plan")
    found = by_name(entries)
    assert found["comfy-xl"]["install"] is True
    assert found["comfy"]["install"] is False
    assert found["comfy"]["reason"] == "tier: chose comfy-xl"


def test_auto_tier_choice_skips_a_member_that_does_not_fit(
    use_registry, monkeypatch, capsys
) -> None:
    root = use_registry(["comfy", "comfy-xl"])
    write_package(root, "comfy-xl", Spec(gpu=True, vram_gb=24, tier_group="comfy", tier="xl"))
    _mark_tier(root, "comfy", "standard")
    set_machine(monkeypatch, gpu_mib=8 * 1024, ram_gb=64.0)
    _code, _envelope, entries = plan_of(capsys, "pull", IMAGE, "--plan")
    found = by_name(entries)
    assert found["comfy"]["install"] is True
    assert found["comfy-xl"]["install"] is False
    assert found["comfy-xl"]["reason"] == "tier: chose comfy"


def test_tier_is_forced_even_when_it_does_not_fit(use_registry, monkeypatch, capsys) -> None:
    root = use_registry(["comfy", "comfy-xl"], Spec(tier_group="comfy", tier="standard"))
    write_package(root, "comfy-xl", Spec(gpu=True, vram_gb=24, tier_group="comfy", tier="xl"))
    set_machine(monkeypatch, gpu_mib=8 * 1024, ram_gb=64.0)
    _code, _envelope, entries = plan_of(capsys, "pull", IMAGE, "--tier", "xl", "--plan")
    found = by_name(entries)
    assert found["comfy-xl"]["install"] is True
    assert "warning" in str(found["comfy-xl"]["reason"])
    assert "--tier xl" in str(found["comfy-xl"]["reason"])
    assert found["comfy"]["install"] is False
    assert found["comfy"]["reason"] == "tier: chose comfy-xl"


def test_missing_tier_keeps_the_automatic_choice(use_registry, monkeypatch, capsys) -> None:
    root = use_registry(["comfy", "comfy-xl"], Spec(tier_group="comfy", tier="standard"))
    write_package(root, "comfy-xl", Spec(gpu=True, vram_gb=24, tier_group="comfy", tier="xl"))
    set_machine(monkeypatch, gpu_mib=8 * 1024, ram_gb=64.0)
    _code, _envelope, entries = plan_of(capsys, "pull", IMAGE, "--tier", "nope", "--plan")
    found = by_name(entries)
    assert found["comfy"]["install"] is True
    assert found["comfy"]["reason"] == "no nope tier; chose comfy"


def test_plan_without_json_prints_and_installs_nothing(use_registry, monkeypatch, capsys) -> None:
    use_registry(["a", "b"])
    calls: list[str] = []
    fake_pull(monkeypatch, calls)
    set_machine(monkeypatch, gpu_mib=None, ram_gb=8.0)
    assert main(["pull", IMAGE, "--plan"]) == 0
    out = capsys.readouterr().out
    assert "a 0.1.0" in out and "b 0.1.0" in out
    assert calls == []


def test_confirmation_required_off_tty_exits_11(use_registry, monkeypatch, capsys) -> None:
    use_registry(["a", "b"])
    calls: list[str] = []
    fake_pull(monkeypatch, calls)
    set_machine(monkeypatch, gpu_mib=None, ram_gb=8.0)
    code = main(["pull", IMAGE, "--json"])
    envelope = json.loads(capsys.readouterr().out)
    assert code == EXIT_CODES["confirmation_required"] == EXIT_CONFIRMATION
    assert envelope["ok"] is False
    error = envelope["error"]
    assert error["code"] == "confirmation_required"
    assert error["hint"] == "add --yes"
    assert [item["name"] for item in error["details"]["plan"]] == ["a", "b"]
    assert calls == []


def test_repeated_package_flags_take_the_planned_path(use_registry, monkeypatch, capsys) -> None:
    use_registry(["a", "b"])
    calls: list[str] = []
    fake_pull(monkeypatch, calls)
    set_machine(monkeypatch, gpu_mib=None, ram_gb=8.0)
    code = main(["pull", "--package", "a", "--package", "b", "--json"])
    envelope = json.loads(capsys.readouterr().out)
    assert code == EXIT_CONFIRMATION
    assert envelope["error"]["code"] == "confirmation_required"
    assert [item["name"] for item in envelope["error"]["details"]["plan"]] == ["a", "b"]
    assert calls == []


def test_yes_installs_the_plan_and_summarizes(use_registry, monkeypatch, capsys) -> None:
    use_registry(["a", "b"])
    calls: list[str] = []
    fake_pull(monkeypatch, calls)
    set_machine(monkeypatch, gpu_mib=None, ram_gb=8.0)
    code, envelope, entries = plan_of(capsys, "pull", IMAGE, "--yes")
    data = envelope["data"]
    assert code == 0
    assert calls == ["a", "b"]
    assert data["installed"] == [
        {"name": "a", "version": "0.1.0"},
        {"name": "b", "version": "0.1.0"},
    ]
    assert data["skipped"] == []
    assert data["downloaded_bytes"] == TWO_PULLS_BYTES
    assert [entry["name"] for entry in entries] == ["a", "b"]


def test_summary_lists_installed_skipped_and_bytes(use_registry, monkeypatch, capsys) -> None:
    root = use_registry(["a", "b"], Spec(gpu=True))
    write_package(root, "b")
    fake_pull(monkeypatch, [])
    set_machine(monkeypatch, gpu_mib=None, ram_gb=8.0)
    assert main(["pull", IMAGE, "--yes"]) == 0
    out = capsys.readouterr().out
    assert "installed b 0.1.0" in out
    assert "skipped a: needs an NVIDIA GPU" in out
    assert "10 bytes downloaded" in out


def test_tty_no_answer_installs_nothing(use_registry, monkeypatch, capsys) -> None:
    use_registry(["a", "b"])
    calls: list[str] = []
    fake_pull(monkeypatch, calls)
    set_machine(monkeypatch, gpu_mib=None, ram_gb=8.0)
    monkeypatch.setattr(plan.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert main(["pull", IMAGE]) == 0
    out = capsys.readouterr().out
    assert "nothing installed" in out
    assert calls == []


def test_tty_yes_answer_installs(use_registry, monkeypatch, capsys) -> None:
    use_registry(["a", "b"])
    calls: list[str] = []
    fake_pull(monkeypatch, calls)
    set_machine(monkeypatch, gpu_mib=None, ram_gb=8.0)
    monkeypatch.setattr(plan.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    asked: list[str] = []

    def answer(prompt: str) -> str:
        asked.append(prompt)
        return "y"

    monkeypatch.setattr("builtins.input", answer)
    assert main(["pull", IMAGE]) == 0
    out = capsys.readouterr().out
    assert "a 0.1.0" in out and "b 0.1.0" in out
    assert asked and "install 2 packages?" in asked[0]
    assert calls == ["a", "b"]


def test_disk_refusal_happens_before_any_download(use_registry, monkeypatch, capsys) -> None:
    use_registry(["a", "b"], Spec(disk_gb=4))
    calls: list[str] = []
    fake_pull(monkeypatch, calls)
    set_machine(monkeypatch, gpu_mib=None, ram_gb=8.0)
    monkeypatch.setattr(plan.shutil, "disk_usage", lambda path: SimpleNamespace(free=GB))
    code = main(["pull", IMAGE, "--yes", "--json"])
    envelope = json.loads(capsys.readouterr().out)
    error = envelope["error"]
    assert code == EXIT_MACHINE_CANNOT
    assert error["code"] == "machine_cannot"
    details = error["details"]
    assert details["reason"] == "disk"
    assert details["needed_gb"] == DISK_NEEDED_GB
    assert details["free_gb"] == DISK_FREE_GB
    assert details["packages"] == [
        {"name": "a", "disk_gb": 4},
        {"name": "b", "disk_gb": 4},
    ]
    assert calls == []


def test_facet_wins_over_a_package_with_the_same_name(use_registry, monkeypatch, capsys) -> None:
    use_registry(["cad", "freecad"], Spec(facet="cad"))
    set_machine(monkeypatch, gpu_mib=None, ram_gb=8.0)
    code, _envelope, entries = plan_of(capsys, "pull", "cad", "--plan")
    assert code == 0
    assert {entry["name"] for entry in entries} == {"cad", "freecad"}


def test_positional_name_mixed_with_package_is_invalid(use_registry, monkeypatch, capsys) -> None:
    use_registry(["a"], Spec(facet="text"))
    set_machine(monkeypatch, gpu_mib=None, ram_gb=8.0)
    code = main(["pull", "text", "--package", "a", "--json"])
    envelope = json.loads(capsys.readouterr().out)
    assert code == EXIT_CODES["invalid_input"]
    assert envelope["error"]["code"] == "invalid_input"


def test_unknown_name_lists_facets_and_packages(use_registry, monkeypatch, capsys) -> None:
    use_registry(["a"])
    set_machine(monkeypatch, gpu_mib=None, ram_gb=8.0)
    code = main(["pull", "nope", "--json"])
    envelope = json.loads(capsys.readouterr().out)
    assert code == EXIT_CODES["invalid_input"]
    details = envelope["error"]["details"]
    assert "image" in details["facets"]
    assert details["packages"] == ["a"]


@pytest.mark.parametrize(
    "argv",
    [
        ["pull", "solo", "--json"],
        ["pull", "--package", "solo", "--json"],
        ["pull", "solo", "--yes", "--json"],
        ["pull", "solo", "--yes"],
        ["pull", "--package", "solo"],
    ],
)
def test_single_package_pull_keeps_todays_shape(
    use_registry, monkeypatch, capsys, argv: list[str]
) -> None:
    use_registry(["solo"], Spec(facet="text"))
    calls: list[str] = []
    fake_pull(monkeypatch, calls)
    set_machine(monkeypatch, gpu_mib=None, ram_gb=8.0)
    code = main(argv)
    out = capsys.readouterr().out
    assert code == 0
    assert calls == ["solo"]
    if "--json" in argv:
        data = json.loads(out)["data"]
        assert data["already_installed"] is False
        assert "plan" not in data
        assert "installed" not in data
    else:
        assert out == "solo 0.1.0 installed at env (no model downloads in 0.0 s)\n"


def test_single_package_with_plan_or_cpu_asks_the_plan(use_registry, monkeypatch, capsys) -> None:
    use_registry(["gpuonly"], Spec(gpu=True))
    calls: list[str] = []
    fake_pull(monkeypatch, calls)
    set_machine(monkeypatch, gpu_mib=8192, ram_gb=64.0)
    code, _envelope, entries = plan_of(capsys, "pull", "--package", "gpuonly", "--plan")
    assert code == 0
    assert entries[0]["install"] is True
    code, _envelope, entries = plan_of(capsys, "pull", "--package", "gpuonly", "--cpu")
    assert code == 0
    assert entries[0]["install"] is False
    assert entries[0]["reason"] == "needs a GPU and --cpu was given"
    assert calls == []


def test_already_installed_is_a_noop_and_needs_no_confirmation(
    use_registry, monkeypatch, capsys
) -> None:
    root = use_registry(["ready", "other"], Spec(facet="text"))
    make_installed(Home.from_env(), root, "ready")
    calls: list[str] = []
    fake_pull(monkeypatch, calls)
    set_machine(monkeypatch, gpu_mib=None, ram_gb=8.0)
    code, envelope, entries = plan_of(capsys, "pull", "text")
    data = envelope["data"]
    found = by_name(entries)
    assert code == 0
    assert found["ready"]["reason"] == "already installed"
    assert data["installed"] == [
        {"name": "ready", "version": "0.1.0"},
        {"name": "other", "version": "0.1.0"},
    ]
    assert calls == ["other"]


def test_failure_stops_and_reports_what_was_installed(use_registry, monkeypatch, capsys) -> None:
    use_registry(["a", "b"])
    calls: list[str] = []
    fake_pull(monkeypatch, calls, fail="b")
    set_machine(monkeypatch, gpu_mib=None, ram_gb=8.0)
    code = main(["pull", IMAGE, "--yes", "--json"])
    envelope = json.loads(capsys.readouterr().out)
    assert code == EXIT_CODES["download_failed"]
    assert envelope["error"]["code"] == "download_failed"
    assert envelope["error"]["details"]["installed"] == [{"name": "a", "version": "0.1.0"}]
    assert envelope["error"]["details"]["downloaded_bytes"] == ONE_PULL_BYTES
    assert calls == ["a", "b"]


def test_manifest_requires_both_tier_keys(use_registry) -> None:
    root = use_registry(["lonely"])
    manifest = root / "packages" / "lonely" / "package.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("[engine]", 'tier = "xl"\n\n[engine]'),
        encoding="utf-8",
    )
    problems = [
        (item["file"], item["key"], item["problem"])
        for item in validate_registry(root)
        if item["file"] == "packages/lonely/package.toml"
    ]
    assert ("packages/lonely/package.toml", "tier", "value") in problems


def test_manifest_rejects_duplicate_tier_in_a_group(use_registry) -> None:
    root = use_registry(["one", "two"], Spec(tier_group="comfy", tier="standard"))
    problems = [
        (item["file"], item["key"], item["problem"])
        for item in validate_registry(root)
        if item["file"] == "packages/two/package.toml"
    ]
    assert ("packages/two/package.toml", "tier", "duplicate") in problems


def test_registry_validate_accepts_tiers(use_registry) -> None:
    root = use_registry(["one"], Spec(tier_group="comfy", tier="standard"))
    write_package(root, "two", Spec(tier_group="comfy", tier="xl"))
    index = root / "index.toml"
    index.write_text(
        index.read_text(encoding="utf-8") + '\n[[packages]]\nname = "two"\n',
        encoding="utf-8",
    )
    registry = load_registry(root)
    assert registry.packages["one"].tier_group == "comfy"
    assert registry.packages["one"].tier == "standard"
    tier_problems = [
        item for item in validate_registry(root) if item["key"] in {"tier", "tier_group"}
    ]
    assert tier_problems == []


def _root() -> Path:
    return Path(os.environ["FACET_REGISTRY"])


def _mark_tier(root: Path, group: str, tier: str) -> None:
    """Rewrite a package's manifest with a ``tier_group`` and ``tier``."""
    directory = root / "packages" / "comfy"
    text = (directory / "package.toml").read_text(encoding="utf-8")
    text = text.replace(
        'facet = "image"', f'facet = "image"\ntier_group = "{group}"\ntier = "{tier}"'
    )
    (directory / "package.toml").write_text(text, encoding="utf-8")
