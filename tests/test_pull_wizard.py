"""``facet pull`` with no argument: the wizard at a TTY, the error elsewhere.

Everything here builds its own tiny registry and drives the wizard with
scripted stdin and fake TTYs; nothing downloads.  ``make_installed`` is
copied from ``tests.test_pull_plan`` so this file reads alone.
"""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from facetmedia import probes, wizard
from facetmedia.cli import main
from facetmedia.contract import EXIT_CODES
from facetmedia.env.package_marker import package_fingerprint
from facetmedia.home import Home
from facetmedia.registry import load_registry
from facetmedia.registry_types import FACETS
from tests.test_pull_plan import Spec, fake_pull, set_machine, write_package, write_registry

GPU_INFO = "RTX 3060, 12288 MiB, 100 MiB"
EXIT_INVALID_INPUT = 2
SelectRegistry = Callable[..., Path]


@pytest.fixture
def use_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SelectRegistry:
    """Point facet at a registry built inside the test."""

    def select(packages: list[str], spec: Spec | None = None) -> Path:
        root = write_registry(tmp_path / "registry", packages, spec)
        monkeypatch.setenv("FACET_REGISTRY", str(root))
        return root

    return select


def make_installed(home: Home, registry_root: Path, name: str) -> None:
    """The markers an installed package carries, so the plan sees a no-op."""
    package = load_registry(registry_root).packages[name]
    env_dir = home.env_dir(package.name, package.version)
    env_dir.mkdir(parents=True)
    (env_dir / ".complete").write_text(package.lock_sha256, encoding="utf-8")
    (env_dir / ".package").write_text(package_fingerprint(package), encoding="utf-8")


class _TtyProxy:
    """Wrap a stream so ``isatty()`` is True while writes still get captured."""

    def __init__(self, wrapped: object) -> None:
        self._wrapped = wrapped

    def isatty(self) -> bool:
        return True

    def __getattr__(self, name: str) -> object:
        return getattr(self._wrapped, name)


class _TtyIn(io.StringIO):
    """A stdin whose only truth is ``isatty()``; answers come from ``input``."""

    def isatty(self) -> bool:
        return True


def scripted_tty(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> None:
    """Fake TTYs on both stdio and feed ``answers`` to ``input`` in order."""
    monkeypatch.setattr(sys, "stdin", _TtyIn(""))
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout))
    monkeypatch.setattr(probes, "gpu_info", lambda: GPU_INFO)
    pending = list(answers)

    def fake_input(prompt: str = "") -> str:
        print(prompt, end="")
        return pending.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)


def no_blender(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never probe the real machine for blender: it is always missing."""
    missing = SimpleNamespace(
        message="blender 4.0+ not found",
        fix="facet config set blender.path /path/to/blender",
    )
    monkeypatch.setattr("facetmedia.config.host_app_problem", lambda _app, _home: missing)


def mixed_root(select: SelectRegistry, home: Home) -> Path:
    """Voice fitting plus installed, audio GPU-bound, video on a host app."""
    root = select(["a", "b", "g", "blendery"])
    write_package(root, "a", Spec(facet="voice"))
    write_package(root, "b", Spec(facet="voice"))
    write_package(root, "g", Spec(facet="audio", gpu=True, vram_gb=24))
    write_package(root, "blendery", Spec(facet="video", host_app=True))
    make_installed(home, root, "b")
    return root


def test_no_arg_with_json_fails_invalid_input(
    use_registry: SelectRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    use_registry(["a"])
    set_machine(monkeypatch, gpu_mib=None, ram_gb=8.0)
    code = main(["pull", "--json"])
    out = capsys.readouterr().out
    envelope = json.loads(out)
    assert code == EXIT_CODES["invalid_input"] == EXIT_INVALID_INPUT
    error = envelope["error"]
    assert error["code"] == "invalid_input"
    assert "facet pull <facet>" in error["message"]
    assert "facet pull --package <name>" in error["message"]
    assert "image" in error["message"]
    assert error["details"]["facets"] == list(FACETS)
    assert error["details"]["packages"] == ["a"]
    assert "█" not in out


def test_no_arg_without_tty_fails_invalid_input(
    use_registry: SelectRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    use_registry(["a"])
    set_machine(monkeypatch, gpu_mib=None, ram_gb=8.0)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    code = main(["pull"])
    captured = capsys.readouterr()
    assert code == EXIT_CODES["invalid_input"] == EXIT_INVALID_INPUT
    assert "facet pull <facet>" in captured.err
    assert "facet pull --package <name>" in captured.err
    assert "image" in captured.err
    assert "█" not in captured.out


def test_wizard_shows_machine_and_installs_default(
    use_registry: SelectRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mixed_root(use_registry, Home.from_env())
    calls: list[str] = []
    fake_pull(monkeypatch, calls)
    set_machine(monkeypatch, gpu_mib=12288, ram_gb=8.0)
    no_blender(monkeypatch)
    scripted_tty(monkeypatch, [""])
    assert main(["pull"]) == 0
    out = capsys.readouterr().out
    assert "█" in out
    assert "RTX 3060" in out
    assert "12.0 GB" in out
    assert "RAM" in out
    assert "free disk" in out
    assert "host app blender" in out
    assert "[x]" in out
    assert "(installed)" in out
    assert "[ ]" in out
    assert calls == ["a"]
    assert "installed a 0.1.0" in out


def test_wizard_toggle_off_lone_facet_selects_nothing(
    use_registry: SelectRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mixed_root(use_registry, Home.from_env())
    calls: list[str] = []
    fake_pull(monkeypatch, calls)
    set_machine(monkeypatch, gpu_mib=12288, ram_gb=8.0)
    no_blender(monkeypatch)
    scripted_tty(monkeypatch, ["1", ""])
    assert main(["pull"]) == 0
    assert "nothing selected" in capsys.readouterr().out
    assert calls == []


def test_wizard_a_selects_every_facet(
    use_registry: SelectRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mixed_root(use_registry, Home.from_env())
    calls: list[str] = []
    fake_pull(monkeypatch, calls)
    set_machine(monkeypatch, gpu_mib=12288, ram_gb=8.0)
    no_blender(monkeypatch)
    scripted_tty(monkeypatch, ["a", ""])
    assert main(["pull"]) == 0
    out = capsys.readouterr().out
    final_listing = out.split(wizard.PROMPT)[-2]
    rows = [line for line in final_listing.splitlines() if line[:1].isdigit() and ") " in line]
    assert len(rows) == len(FACETS)
    assert all("[x]" in row for row in rows)
    assert calls == ["a"]


def test_wizard_garbage_reprompts_then_accepts_default(
    use_registry: SelectRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mixed_root(use_registry, Home.from_env())
    calls: list[str] = []
    fake_pull(monkeypatch, calls)
    set_machine(monkeypatch, gpu_mib=12288, ram_gb=8.0)
    no_blender(monkeypatch)
    scripted_tty(monkeypatch, ["zz", ""])
    assert main(["pull"]) == 0
    assert "type facet numbers, a, or q" in capsys.readouterr().out
    assert calls == ["a"]


def test_wizard_q_quits_without_installing(
    use_registry: SelectRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mixed_root(use_registry, Home.from_env())
    calls: list[str] = []
    fake_pull(monkeypatch, calls)
    set_machine(monkeypatch, gpu_mib=12288, ram_gb=8.0)
    no_blender(monkeypatch)
    scripted_tty(monkeypatch, ["q"])
    assert main(["pull"]) == 0
    assert "nothing installed" in capsys.readouterr().out
    assert calls == []


def test_wizard_two_facets_ask_once_and_install(
    use_registry: SelectRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = use_registry(["a", "c"])
    write_package(root, "a", Spec(facet="voice"))
    write_package(root, "c", Spec(facet="audio"))
    calls: list[str] = []
    fake_pull(monkeypatch, calls)
    set_machine(monkeypatch, gpu_mib=12288, ram_gb=8.0)
    no_blender(monkeypatch)
    scripted_tty(monkeypatch, ["", "y"])
    assert main(["pull"]) == 0
    out = capsys.readouterr().out
    assert "install 2 packages? [y/N]" in out
    assert calls == ["a", "c"]


def test_wizard_two_facets_decline_installs_nothing(
    use_registry: SelectRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = use_registry(["a", "c"])
    write_package(root, "a", Spec(facet="voice"))
    write_package(root, "c", Spec(facet="audio"))
    calls: list[str] = []
    fake_pull(monkeypatch, calls)
    set_machine(monkeypatch, gpu_mib=12288, ram_gb=8.0)
    no_blender(monkeypatch)
    scripted_tty(monkeypatch, ["", "n"])
    assert main(["pull"]) == 0
    out = capsys.readouterr().out
    assert "install 2 packages? [y/N]" in out
    assert "nothing installed" in out
    assert calls == []


def test_wizard_tier_group_installs_chosen_member(
    use_registry: SelectRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = use_registry(["comfy", "comfy-xl"], Spec(tier_group="comfy", tier="standard"))
    write_package(root, "comfy-xl", Spec(gpu=True, vram_gb=24, tier_group="comfy", tier="xl"))
    calls: list[str] = []
    fake_pull(monkeypatch, calls)
    set_machine(monkeypatch, gpu_mib=8 * 1024, ram_gb=64.0)
    no_blender(monkeypatch)
    scripted_tty(monkeypatch, [""])
    monkeypatch.setattr(probes, "gpu_info", lambda: "RTX 3060, 8192 MiB, 100 MiB")
    assert main(["pull"]) == 0
    out = capsys.readouterr().out
    assert "tier: chose comfy" in out
    assert calls == ["comfy"]


def test_wizard_app_line_reports_ready(
    use_registry: SelectRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = use_registry(["blendery"])
    write_package(root, "blendery", Spec(host_app=True))
    monkeypatch.setattr("facetmedia.config.host_app_problem", lambda _app, _home: None)
    lines = wizard._app_lines(load_registry(root), Home.from_env())
    assert any("host app blender" in line and "ready" in line for line in lines)
