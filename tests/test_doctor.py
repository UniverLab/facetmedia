"""``facet doctor``: facet's own section, machine readiness, the package doctor.

The readiness tests moved here from ``test_check.py`` with the command they
now belong to; the doctor-specific tests cover the facet section, the runner
depths, and the exit classes.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from facetmedia import doctor as doctor_module
from facetmedia import env as env_module
from facetmedia import shared as shared_module
from facetmedia.cli import main
from facetmedia.home import Home
from facetmedia.registry import load_registry
from tests.registry_server import COMMIT, OTHER_COMMIT
from tests.test_pull import BROWSER_REVISION, FakeBrowser

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
EXIT_INVALID_INPUT = 2
EXIT_LICENSE_NOT_ACCEPTED = 4
EXIT_MACHINE_CANNOT = 5
EXIT_NOT_INSTALLED = 7
EXIT_RUNNER_FAILED = 9
#: VRAM the fixture GPU line reports: round(8151 / 1024, 3).
RTX_5060_VRAM_GB = 7.96
#: More than the fixture GPU reports, so the VRAM gate must fail.
TOO_MUCH_VRAM_GB = 12.0

#: An SDK runner without a ``doctor`` verb: the SDK answers for it.
NO_DOCTOR_RUNNER = '''"""Echo run; no doctor verb is registered."""

from __future__ import annotations

from typing import Any

from facet_runner_sdk import Request, Runner

runner = Runner()


@runner.verb("run")
def run(req: Request) -> dict[str, Any]:
    return {"tool": {"name": "echo", "version": "0.1.0"}, "assets": []}


runner.main()
'''

#: An SDK runner whose doctor reports a failing check.
FAILING_DOCTOR_RUNNER = '''"""Echo run; the doctor reports a failing check."""

from __future__ import annotations

from typing import Any

from facet_runner_sdk import Request, Runner

runner = Runner()


@runner.verb("run")
def run(req: Request) -> dict[str, Any]:
    return {"tool": {"name": "echo", "version": "0.1.0"}, "assets": []}


@runner.verb("doctor")
def doctor(req: Request) -> dict[str, Any]:
    return {
        "checks": [
            {"name": "imports", "ok": False, "detail": "cannot import the tool", "fix": "reinstall"}
        ]
    }


runner.main()
'''

#: An SDK runner whose doctor breaks the protocol (no checks list).
BAD_PROTOCOL_RUNNER = '''"""Echo run; the doctor writes a result without checks."""

from __future__ import annotations

from typing import Any

from facet_runner_sdk import Request, Runner

runner = Runner()


@runner.verb("run")
def run(req: Request) -> dict[str, Any]:
    return {"tool": {"name": "echo", "version": "0.1.0"}, "assets": []}


@runner.verb("doctor")
def doctor(req: Request) -> dict[str, Any]:
    return {"ok": True}


runner.main()
'''

#: A stdlib runner whose doctor sleeps past the quick budget.
SLOW_DOCTOR_RUNNER = '''"""Stdlib runner whose doctor sleeps past the quick budget."""

import sys
import time


def main() -> int:
    if sys.argv[1] == "doctor":
        time.sleep(30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

#: A stdlib runner whose doctor dies with exit 1.
CRASHING_DOCTOR_RUNNER = '''"""Stdlib runner whose doctor exits 1 before writing a result."""

import sys


def main() -> int:
    if sys.argv[1] == "doctor":
        print("doctor exploded", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

#: An SDK runner whose run echoes like the fixture; its doctor sleeps.
SLOW_DOCTOR_ECHO_RUNNER = '''"""Echo run; the doctor sleeps past the quick budget."""

from __future__ import annotations

import time
from typing import Any

from facet_runner_sdk import Request, Runner

runner = Runner()


@runner.verb("run")
def run(req: Request) -> dict[str, Any]:
    text = str(req.inputs["text"]) * int(req.inputs["repeat"])
    (req.out_dir / "echo.txt").write_text(text, encoding="utf-8")
    return {
        "tool": {"name": "echo", "version": "0.1.0"},
        "assets": [
            {
                "file": "echo.txt",
                "name": "echo",
                "kind": "text",
                "mime": "text/plain",
                "properties": {},
                "params": {},
                "seed": None,
                "inputs": [],
            }
        ],
    }


@runner.verb("verify")
def verify(req: Request) -> dict[str, Any]:
    expected = str(req.inputs["text"]) * int(req.inputs["repeat"])
    entries = []
    for path in req.assets:
        matched = path.read_text(encoding="utf-8") == expected
        entries.append(
            {
                "file": path.name,
                "passed": matched,
                "metrics": {"match": 1 if matched else 0},
                "thresholds": {},
                "verifier": "echo-content",
            }
        )
    return {"assets": entries}


@runner.verb("doctor")
def doctor(req: Request) -> dict[str, Any]:
    time.sleep(30)
    return {"checks": [{"name": "imports", "ok": True, "detail": "too late", "fix": ""}]}


runner.main()
'''


def _registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))


def _envelope(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    parsed: Any = json.loads(capsys.readouterr().out)
    assert isinstance(parsed, dict)
    return parsed


def _data(envelope: dict[str, Any]) -> dict[str, Any]:
    data = envelope["data"]
    assert isinstance(data, dict)
    assert envelope["ok"] is (data["healthy"] is True)
    return data


@pytest.fixture(scope="module")
def seed_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One home with echo installed; shared by every test in this module."""
    root = tmp_path_factory.mktemp("facet-home-seed")
    previous = {name: os.environ.get(name) for name in ("FACET_HOME", "FACET_REGISTRY")}
    os.environ["FACET_HOME"] = str(root)
    os.environ["FACET_REGISTRY"] = str(FIXTURES)
    try:
        assert main(["pull", "echo", "--json"]) == 0
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return root


@pytest.fixture
def facet_home(seed_home: Path, monkeypatch: pytest.MonkeyPatch, _facet_home_tmp: None) -> Path:
    """The shared home, after the per-test temp home has been set and replaced."""
    monkeypatch.setenv("FACET_HOME", str(seed_home))
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))
    return seed_home


@pytest.fixture
def swap_echo_runner(facet_home: Path) -> Any:
    """Replace the installed echo runner for one test, then restore it."""
    target = facet_home / "envs" / "echo" / "0.1.0" / "package" / "runner.py"
    original = target.read_text(encoding="utf-8")

    def swap(source: str) -> None:
        target.write_text(source, encoding="utf-8")

    yield swap
    target.write_text(original, encoding="utf-8")


def _fresh_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "fresh-home"
    monkeypatch.setenv("FACET_HOME", str(home))
    _registry(monkeypatch)
    return home


def _dead_pid() -> int:
    """A pid that certainly no longer runs: a just-exited child's."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def _spy_hold(calls: list[tuple[str, str, bool, float]]) -> Any:
    """A ``lock.hold`` that records its arguments and then takes the real lock."""
    real = doctor_module.lock.hold

    @contextlib.contextmanager
    def hold(mode: str, purpose: str, wait: bool = True, timeout_s: float = 3600.0) -> Any:
        calls.append((mode, purpose, wait, timeout_s))
        with real(mode, purpose, wait, timeout_s):
            yield

    return hold


# --- FR8: the JSON shape, with the capability keys.


def test_doctor_capability_target_json_shape(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _fresh_home(monkeypatch, tmp_path)

    code = main(["doctor", "echo.text", "--json"])
    data = _data(_envelope(capsys))
    # A named package that is not installed is the not_installed class.
    assert code == EXIT_NOT_INSTALLED
    assert set(data) == {
        "healthy",
        "facet",
        "packages",
        "capability",
        "can_run",
        "needs_pull",
    }
    assert data["capability"] == "echo.text"
    assert data["can_run"] is True
    assert data["needs_pull"] is True
    assert data["healthy"] is False
    facet_names = [entry["name"] for entry in doctor_module.facet_checks(Home.from_env())]
    assert facet_names == list(doctor_module.FACET_CHECK_ORDER)
    assert set(data["facet"]) == set(doctor_module.FACET_CHECK_ORDER)
    for entry in data["facet"].values():
        assert set(entry) == {"ok", "detail", "fix"}
    section = data["packages"]["echo"]
    assert set(section) == {"installed", "readiness", "doctor"}
    assert section["installed"] is False
    assert set(section["readiness"]) == set(doctor_module.CHECK_ORDER)
    for entry in section["readiness"].values():
        assert set(entry) == {"ok", "detail", "fix"}
    for entry in section["doctor"]:
        assert set(entry) == {"name", "ok", "detail", "fix"}
    # The doctor never downloaded anything: the fresh home still has no envs.
    assert list((home / "envs").glob("*/*")) == []


def test_doctor_after_pull_is_healthy_and_reports_the_full_depth(
    capsys: pytest.CaptureFixture[str], facet_home: Path
) -> None:
    assert facet_home.is_dir()

    code = main(["doctor", "echo.text", "--json"])
    data = _data(_envelope(capsys))
    assert code == 0
    assert data["healthy"] is True
    assert data["can_run"] is True
    assert data["needs_pull"] is False
    section = data["packages"]["echo"]
    assert section["installed"] is True
    assert section["readiness"]["installed"]["ok"] is True
    details = [entry["detail"] for entry in section["doctor"]]
    assert any("(full)" in detail for detail in details)
    assert any("throwaway word" in detail for detail in details)


def test_doctor_quick_runs_only_the_quick_depth(
    capsys: pytest.CaptureFixture[str], facet_home: Path
) -> None:
    assert facet_home.is_dir()

    code = main(["doctor", "echo.text", "--quick", "--json"])
    data = _data(_envelope(capsys))
    assert code == 0
    checks = data["packages"]["echo"]["doctor"]
    assert len(checks) == 1
    assert "(quick)" in checks[0]["detail"]
    # quick writes nothing: the doctor's throwaway word is not there.
    assert not list((facet_home / "tmp").glob(".facet-doctor-*"))


def test_doctor_without_a_target_lists_only_installed_packages(
    capsys: pytest.CaptureFixture[str], facet_home: Path
) -> None:
    assert facet_home.is_dir()

    code = main(["doctor", "--json"])
    data = _data(_envelope(capsys))
    assert code == 0
    assert data["healthy"] is True
    assert "capability" not in data
    assert set(data["packages"]) == {"echo"}


def test_doctor_unknown_target_is_invalid_input(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fresh_home(monkeypatch, tmp_path)

    code = main(["doctor", "nope.thing", "--json"])
    assert code == EXIT_INVALID_INPUT
    envelope = _envelope(capsys)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "invalid_input"


def test_doctor_package_name_target_covers_the_package_without_capability_keys(
    capsys: pytest.CaptureFixture[str], facet_home: Path
) -> None:
    """A package name resolves as a package: the capability keys stay out."""
    assert facet_home.is_dir()

    code = main(["doctor", "echo", "--json"])
    data = _data(_envelope(capsys))
    assert code == 0
    assert data["healthy"] is True
    assert set(data) == {"healthy", "facet", "packages"}
    assert set(data["packages"]) == {"echo"}
    section = data["packages"]["echo"]
    assert section["installed"] is True
    details = [entry["detail"] for entry in section["doctor"]]
    assert any("echo.text" in detail for detail in details)


def test_doctor_takes_the_lock_shared_and_honours_the_wait_flags(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Doctor holds the facet lock shared and passes --no-wait through."""
    _fresh_home(monkeypatch, tmp_path)
    calls: list[tuple[str, str, bool, float]] = []
    monkeypatch.setattr(doctor_module.lock, "hold", _spy_hold(calls))

    assert main(["doctor", "--json"]) == 0
    _envelope(capsys)
    assert calls == [("shared", "facet doctor", True, 3600.0)]

    assert main(["doctor", "--no-wait", "--wait-timeout", "30", "--json"]) == 0
    _envelope(capsys)
    assert calls[1] == ("shared", "facet doctor", False, 30.0)


# --- FR3: the readiness checks and their fixes.


def test_doctor_low_ram_fails_with_exit_5_and_a_fix(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, facet_home: Path
) -> None:
    assert facet_home.is_dir()
    monkeypatch.setattr(doctor_module, "mem_total_gb", lambda: 0.05)

    code = main(["doctor", "echo.text", "--json"])
    data = _data(_envelope(capsys))
    assert code == EXIT_MACHINE_CANNOT
    assert data["healthy"] is False
    assert data["can_run"] is False
    entry = data["packages"]["echo"]["readiness"]["ram"]
    assert entry["ok"] is False
    assert entry["fix"] == "echo needs 0.1 GB of RAM"


def test_doctor_incompatible_package_fails_with_exit_5(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fresh_home(monkeypatch, tmp_path)

    code = main(["doctor", "future.thing", "--json"])
    data = _data(_envelope(capsys))
    assert code == EXIT_MACHINE_CANNOT
    entry = data["packages"]["future"]["readiness"]["compatible"]
    assert entry["ok"] is False
    assert entry["fix"] == "facet update, or upgrade facet"
    assert data["can_run"] is False


def test_doctor_unsupported_platform_fails_with_exit_5(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fresh_home(monkeypatch, tmp_path)
    monkeypatch.setattr(doctor_module.sys, "platform", "darwin")

    code = main(["doctor", "echo.text", "--json"])
    data = _data(_envelope(capsys))
    assert code == EXIT_MACHINE_CANNOT
    entry = data["packages"]["echo"]["readiness"]["platform"]
    assert entry["ok"] is False
    assert "linux" in entry["detail"]
    assert "linux" in entry["fix"]


def test_doctor_insufficient_disk_fails_with_exit_5(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fresh_home(monkeypatch, tmp_path)
    monkeypatch.setattr(doctor_module.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))

    code = main(["doctor", "echo.text", "--json"])
    data = _data(_envelope(capsys))
    assert code == EXIT_MACHINE_CANNOT
    entry = data["packages"]["echo"]["readiness"]["disk"]
    assert entry["ok"] is False
    assert "GB free at" in entry["fix"]


def test_doctor_unreadable_meminfo_fails_with_exit_5(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fresh_home(monkeypatch, tmp_path)
    monkeypatch.setattr(doctor_module, "mem_total_gb", lambda: None)

    code = main(["doctor", "echo.text", "--json"])
    data = _data(_envelope(capsys))
    assert code == EXIT_MACHINE_CANNOT
    entry = data["packages"]["echo"]["readiness"]["ram"]
    assert entry["ok"] is False
    assert "MemTotal" in entry["detail"]


def test_doctor_license_gate_maps_to_exit_4_with_the_accept_fix(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fresh_home(monkeypatch, tmp_path)

    code = main(["doctor", "termsy.make", "--json"])
    data = _data(_envelope(capsys))
    assert code == EXIT_LICENSE_NOT_ACCEPTED
    entry = data["packages"]["termsy"]["readiness"]["licenses"]
    assert entry["ok"] is False
    assert entry["detail"] == "termsy-weights (CC-BY-NC-4.0): not accepted"
    assert entry["fix"] == "facet license accept termsy-weights"


def test_doctor_missing_model_reports_needs_pull_with_the_pull_fix(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fresh_home(monkeypatch, tmp_path)
    assert main(["license", "accept", "termsy-weights", "--yes", "--json"]) == 0
    _envelope(capsys)

    code = main(["doctor", "termsy.make", "--json"])
    data = _data(_envelope(capsys))
    # needs_pull never picks a class on its own; the named, uninstalled
    # package does, after every other class has been considered.
    assert code == EXIT_NOT_INSTALLED
    assert data["can_run"] is True
    assert data["needs_pull"] is True
    licenses_entry = data["packages"]["termsy"]["readiness"]["licenses"]
    assert licenses_entry == {
        "ok": True,
        "detail": "termsy-weights (CC-BY-NC-4.0): accepted",
        "fix": "",
    }
    models_entry = data["packages"]["termsy"]["readiness"]["models"]
    assert models_entry["ok"] is False
    assert models_entry["fix"] == "facet pull termsy"


def test_machine_checks_report_every_check_in_order(facet_home: Path) -> None:
    home = Home.from_env()
    package = load_registry(FIXTURES).packages["echo"]
    checks, failures = doctor_module.machine_checks(package, home)
    assert list(checks) == list(doctor_module.CHECK_ORDER)
    assert failures == []
    assert all(set(entry) == {"ok", "detail", "fix"} for entry in checks.values())


def test_installed_check_carries_the_pull_fix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fresh_home(monkeypatch, tmp_path)
    package = load_registry(FIXTURES).packages["echo"]
    checks, _failures = doctor_module.machine_checks(package, Home.from_env())
    assert checks["installed"]["ok"] is False
    assert checks["installed"]["fix"] == "facet pull echo"


def test_check_missing_host_lib_fails_with_the_manifest_hint(tmp_path: Path) -> None:
    package = load_registry(FIXTURES).packages["echo"]
    requirements = dataclasses.replace(
        package.requirements,
        host_libs=("libfacet_fixture_missing.so",),
        host_hint="install libfacet_fixture_missing",
    )
    home = Home.from_env()
    checks, failures = doctor_module.machine_checks(
        dataclasses.replace(package, requirements=requirements), home
    )
    assert checks["host_libs"]["ok"] is False
    assert "libfacet_fixture_missing.so" in str(checks["host_libs"]["detail"])
    assert checks["host_libs"]["fix"] == "install libfacet_fixture_missing"
    assert failures[0].code == "machine_cannot"


def test_check_gpu_without_nvidia_reports_none(monkeypatch: pytest.MonkeyPatch) -> None:
    package = load_registry(FIXTURES).packages["echo"]
    monkeypatch.setattr(doctor_module, "gpu_info", lambda: None)
    checks, failures = doctor_module.machine_checks(package, Home.from_env())
    assert checks["gpu"] == {"ok": True, "detail": "none", "fix": ""}
    assert failures == []


def test_check_required_gpu_without_nvidia_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    package = load_registry(FIXTURES).packages["echo"]
    requirements = dataclasses.replace(package.requirements, gpu=True)
    monkeypatch.setattr(doctor_module, "gpu_info", lambda: None)
    checks, failures = doctor_module.machine_checks(
        dataclasses.replace(package, requirements=requirements), Home.from_env()
    )
    assert checks["gpu"]["ok"] is False
    assert "GPU" in checks["gpu"]["fix"]
    assert failures[0].code == "machine_cannot"


def test_check_gpu_with_enough_vram_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    package = load_registry(FIXTURES).packages["echo"]
    requirements = dataclasses.replace(package.requirements, gpu=True, vram_gb=4.0)
    monkeypatch.setattr(
        doctor_module, "gpu_info", lambda: "NVIDIA GeForce RTX 5060, 8151 MiB, 931 MiB"
    )
    checks, failures = doctor_module.machine_checks(
        dataclasses.replace(package, requirements=requirements), Home.from_env()
    )
    assert checks["gpu"]["ok"] is True
    assert "8.0 GB" in str(checks["gpu"]["detail"])
    assert failures == []


def test_check_gpu_with_too_little_vram_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    package = load_registry(FIXTURES).packages["echo"]
    requirements = dataclasses.replace(package.requirements, gpu=True, vram_gb=TOO_MUCH_VRAM_GB)
    monkeypatch.setattr(
        doctor_module, "gpu_info", lambda: "NVIDIA GeForce RTX 5060, 8151 MiB, 931 MiB"
    )
    checks, failures = doctor_module.machine_checks(
        dataclasses.replace(package, requirements=requirements), Home.from_env()
    )
    assert checks["gpu"]["ok"] is False
    assert failures[0].code == "machine_cannot"
    assert failures[0].details["required_gb"] == TOO_MUCH_VRAM_GB
    assert failures[0].details["available_gb"] == RTX_5060_VRAM_GB
    assert f"needs {TOO_MUCH_VRAM_GB} GB of VRAM" in failures[0].message


def test_check_gpu_with_unreadable_total_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    package = load_registry(FIXTURES).packages["echo"]
    requirements = dataclasses.replace(package.requirements, gpu=True, vram_gb=4.0)
    info = "NVIDIA GeForce RTX 5060, N/A, 931 MiB"
    monkeypatch.setattr(doctor_module, "gpu_info", lambda: info)
    checks, failures = doctor_module.machine_checks(
        dataclasses.replace(package, requirements=requirements), Home.from_env()
    )
    assert checks["gpu"]["ok"] is False
    assert failures[0].code == "machine_cannot"
    assert info in failures[0].message


# --- FR2: the facet section.


def test_doctor_corrupt_state_file_fails_with_exit_5(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _fresh_home(monkeypatch, tmp_path)
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "licenses.json").write_text("{not json", encoding="utf-8")

    code = main(["doctor", "--json"])
    data = _data(_envelope(capsys))
    assert code == EXIT_MACHINE_CANNOT
    assert data["healthy"] is False
    state = data["facet"]["state"]
    assert state["ok"] is False
    assert "licenses.json" in state["detail"]
    assert state["fix"]


def test_doctor_reports_a_stale_lock_and_leaves_it_alone(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _fresh_home(monkeypatch, tmp_path)
    (home / "state").mkdir(parents=True, exist_ok=True)
    lock_json = home / "state" / "lock.json"
    lock_json.write_text(
        json.dumps({"mode": "exclusive", "pid": _dead_pid(), "purpose": "long gone"}),
        encoding="utf-8",
    )

    code = main(["doctor", "--json"])
    data = _data(_envelope(capsys))
    assert code == EXIT_MACHINE_CANNOT
    lock_check = data["facet"]["lock"]
    assert lock_check["ok"] is False
    assert "stale info" in lock_check["detail"]
    assert lock_check["fix"].endswith("state/lock.json")
    # Reported, never removed: doctor repairs nothing.
    assert lock_json.is_file()


def test_doctor_reports_orphaned_environments_with_the_gc_fix(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _fresh_home(monkeypatch, tmp_path)
    (home / "envs" / "ghost" / "1.0.0").mkdir(parents=True)

    code = main(["doctor", "--json"])
    data = _data(_envelope(capsys))
    assert code == EXIT_MACHINE_CANNOT
    orphans = data["facet"]["orphans"]
    assert orphans["ok"] is False
    assert "ghost" in orphans["detail"]
    assert orphans["fix"] == "facet clean"
    # Reported, never removed.
    assert (home / "envs" / "ghost" / "1.0.0").is_dir()


# --- FR4: the package doctor's depths and failures.


def test_doctor_without_a_doctor_verb_is_machine_cannot(
    capsys: pytest.CaptureFixture[str], facet_home: Path, swap_echo_runner: Any
) -> None:
    swap_echo_runner(NO_DOCTOR_RUNNER)

    code = main(["doctor", "echo.text", "--json"])
    data = _data(_envelope(capsys))
    assert code == EXIT_MACHINE_CANNOT
    checks = data["packages"]["echo"]["doctor"]
    assert checks[0]["ok"] is False
    assert checks[0]["detail"] == "this package has no doctor"
    assert checks[0]["fix"] == "report it to the package's maintainer"


def test_quick_doctor_over_its_budget_times_out_as_machine_cannot(
    capsys: pytest.CaptureFixture[str],
    facet_home: Path,
    swap_echo_runner: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swap_echo_runner(SLOW_DOCTOR_RUNNER)
    monkeypatch.setitem(doctor_module.DOCTOR_TIMEOUTS, "quick", 1.0)

    code = main(["doctor", "echo.text", "--quick", "--json"])
    data = _data(_envelope(capsys))
    assert code == EXIT_MACHINE_CANNOT
    checks = data["packages"]["echo"]["doctor"]
    assert checks[0]["ok"] is False
    assert "did not finish within 1.0 s" in checks[0]["detail"]


def test_the_quick_budget_is_ten_seconds_for_cold_imports() -> None:
    """The quick budget leaves room for cold imports of OCP, onnxruntime, CTranslate2."""
    cold_import_budget_s = 10.0
    assert cold_import_budget_s == doctor_module.QUICK_TIMEOUT_S
    assert doctor_module.DOCTOR_TIMEOUTS["quick"] == doctor_module.QUICK_TIMEOUT_S


def test_crashing_doctor_is_runner_failed(
    capsys: pytest.CaptureFixture[str], facet_home: Path, swap_echo_runner: Any
) -> None:
    swap_echo_runner(CRASHING_DOCTOR_RUNNER)

    code = main(["doctor", "echo.text", "--json"])
    data = _data(_envelope(capsys))
    assert code == EXIT_RUNNER_FAILED
    checks = data["packages"]["echo"]["doctor"]
    assert checks[0]["ok"] is False
    assert "exit code 1" in checks[0]["detail"]


def test_a_doctor_result_without_checks_is_runner_failed(
    capsys: pytest.CaptureFixture[str], facet_home: Path, swap_echo_runner: Any
) -> None:
    swap_echo_runner(BAD_PROTOCOL_RUNNER)

    code = main(["doctor", "echo.text", "--json"])
    data = _data(_envelope(capsys))
    assert code == EXIT_RUNNER_FAILED
    checks = data["packages"]["echo"]["doctor"]
    assert checks[0]["ok"] is False
    assert "no checks" in checks[0]["detail"]


def test_a_failing_doctor_check_is_machine_cannot(
    capsys: pytest.CaptureFixture[str], facet_home: Path, swap_echo_runner: Any
) -> None:
    swap_echo_runner(FAILING_DOCTOR_RUNNER)

    code = main(["doctor", "echo.text", "--json"])
    data = _data(_envelope(capsys))
    assert code == EXIT_MACHINE_CANNOT
    assert data["can_run"] is False
    checks = data["packages"]["echo"]["doctor"]
    assert checks[0] == {
        "name": "imports",
        "ok": False,
        "detail": "cannot import the tool",
        "fix": "reinstall",
    }


# --- FR5: the gate pull and run run first.


def test_run_gate_refuses_when_the_quick_doctor_fails(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    facet_home: Path,
    swap_echo_runner: Any,
) -> None:
    """The gate calls the installed package's doctor at quick depth."""
    swap_echo_runner(FAILING_DOCTOR_RUNNER)
    piece = tmp_path / "piece.toml"
    piece.write_text('[inputs]\ntext = "hello"\nrepeat = 1\n', encoding="utf-8")

    code = main(["run", "echo.text", str(piece), "--no-pull", "--json"])
    assert code == EXIT_MACHINE_CANNOT
    envelope = _envelope(capsys)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "machine_cannot"
    assert "cannot import the tool" in envelope["error"]["message"]


def test_run_gate_runs_with_a_slow_doctor_warning(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    facet_home: Path,
    swap_echo_runner: Any,
) -> None:
    """A quick doctor that only times out warns the run gate and does not block."""
    swap_echo_runner(SLOW_DOCTOR_ECHO_RUNNER)
    monkeypatch.setitem(doctor_module.DOCTOR_TIMEOUTS, "quick", 1.0)
    piece = tmp_path / "piece.toml"
    piece.write_text('[inputs]\ntext = "hello"\nrepeat = 1\n', encoding="utf-8")
    warning = "echo doctor did not answer within 1.0 s; running anyway"

    code = main(["run", "echo.text", str(piece), "--no-pull", "--json"])
    captured = capsys.readouterr()
    assert code == 0
    envelope: Any = json.loads(captured.out)
    assert isinstance(envelope, dict)
    assert envelope["ok"] is True
    assert envelope["data"]["warnings"] == [warning]
    assert warning in captured.err
    assert (tmp_path / "echo.txt").read_text(encoding="utf-8") == "hello"


def test_gate_skips_the_doctor_before_the_env_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A gate that runs before a pull has nothing to call: readiness only."""
    _fresh_home(monkeypatch, tmp_path)
    package = load_registry(FIXTURES).packages["echo"]

    errors, warnings = doctor_module.requirement_errors(package, Home.from_env(), "test-run")
    assert errors == []
    assert warnings == []


def test_requirement_errors_include_the_quick_doctor(
    facet_home: Path, swap_echo_runner: Any
) -> None:
    """The gate asks the installed package's doctor, so its failure gates too."""
    swap_echo_runner(FAILING_DOCTOR_RUNNER)
    package = load_registry(FIXTURES).packages["echo"]

    errors, warnings = doctor_module.requirement_errors(package, Home.from_env(), "test-run")
    assert [error.code for error in errors] == ["machine_cannot"]
    assert "cannot import the tool" in errors[0].message
    assert warnings == []


def test_human_output_marks_each_check_and_indents_the_fix(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fresh_home(monkeypatch, tmp_path)

    code = main(["doctor", "termsy.make"])
    out = capsys.readouterr().out
    assert code == EXIT_LICENSE_NOT_ACCEPTED
    assert "✓ python: python" in out
    assert "✗ licenses: termsy-weights (CC-BY-NC-4.0): not accepted" in out
    assert "    → fix: facet license accept termsy-weights" in out
    assert "✗ installed:" in out
    assert "    → fix: facet pull termsy" in out


# --- FR4: the shared-modules check and its stale environment.


def _fake_env(home: Home, package: Any, marker: str) -> Path:
    """An environment directory whose markers say what a pull would have written."""
    env_dir = home.env_dir(package.name, package.version)
    env_dir.mkdir(parents=True)
    (env_dir / ".complete").write_text(package.lock_sha256, encoding="utf-8")
    (env_dir / ".registry").write_text(marker, encoding="utf-8")
    return env_dir


def _shared_entry(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    data = _data(_envelope(capsys))
    entry = data["packages"]["echo"]["readiness"]["shared_modules"]
    assert isinstance(entry, dict)
    return entry


def test_shared_modules_check_reports_the_pulled_copy(facet_home: Path) -> None:
    assert facet_home.is_dir()
    package = load_registry(FIXTURES).packages["echo"]
    home = Home.from_env()

    checks, failures = doctor_module.machine_checks(package, home)
    assert checks["shared_modules"] == {
        "ok": True,
        "detail": "1 shared module(s) present",
        "fix": "",
    }
    assert failures == []


def test_shared_modules_check_is_absent_from_can_run_checks() -> None:
    """What a pull fixes never gates a pull or a run."""
    assert "shared_modules" in doctor_module.CHECK_ORDER
    assert "shared_modules" not in doctor_module.CAN_RUN_CHECKS
    assert list(doctor_module.CHECK_ORDER)[-1] == "shared_modules"


def test_shared_modules_check_without_uses_or_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fresh_home(monkeypatch, tmp_path)
    home = Home.from_env()
    registry = load_registry(FIXTURES)

    echo_checks, _ = doctor_module.machine_checks(registry.packages["echo"], home)
    assert echo_checks["shared_modules"] == {
        "ok": True,
        "detail": "no environment to be stale",
        "fix": "",
    }
    termsy_checks, _ = doctor_module.machine_checks(registry.packages["termsy"], home)
    assert termsy_checks["shared_modules"] == {
        "ok": True,
        "detail": "no shared modules declared",
        "fix": "",
    }


def test_doctor_detects_stale_shared_modules_in_override_mode(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry_copy = tmp_path / "registry"
    shutil.copytree(FIXTURES, registry_copy)
    monkeypatch.setenv("FACET_REGISTRY", str(registry_copy))
    Home.from_env()
    assert main(["pull", "echo", "--json"]) == 0
    capsys.readouterr()
    assert main(["doctor", "echo", "--json"]) == 0
    assert _shared_entry(capsys)["ok"] is True

    module = registry_copy / "lib" / "textnorm.py"
    module.write_text(module.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    assert main(["doctor", "echo.text", "--json"]) == 0
    data = _data(_envelope(capsys))
    entry = data["packages"]["echo"]["readiness"]["shared_modules"]
    assert isinstance(entry, dict)
    assert entry["ok"] is False
    assert entry["detail"].endswith("the shared modules are stale")
    assert entry["fix"] == "facet pull echo"
    assert data["needs_pull"] is True
    assert data["can_run"] is True

    assert main(["pull", "echo", "--json"]) == 0
    capsys.readouterr()
    assert main(["doctor", "echo.text", "--json"]) == 0
    data = _data(_envelope(capsys))
    entry = data["packages"]["echo"]["readiness"]["shared_modules"]
    assert isinstance(entry, dict)
    assert entry["ok"] is True
    assert data["needs_pull"] is False


def test_doctor_detects_stale_shared_modules_in_pinned_mode(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("FACET_REGISTRY", raising=False)
    home = Home.from_env()
    shutil.copytree(FIXTURES, home.registry_dir(COMMIT))
    shutil.copytree(FIXTURES, home.registry_dir(OTHER_COMMIT))
    home.state_file("registry.json").write_text(
        json.dumps({"commit": COMMIT, "ref": "main"}), encoding="utf-8"
    )
    package = load_registry(home.registry_dir(COMMIT)).packages["echo"]
    assert shared_module.fingerprint(package) == COMMIT
    _fake_env(home, package, COMMIT)

    main(["doctor", "echo", "--json"])
    assert _shared_entry(capsys)["ok"] is True

    home.state_file("registry.json").write_text(
        json.dumps({"commit": OTHER_COMMIT, "ref": "main"}), encoding="utf-8"
    )
    main(["doctor", "echo", "--json"])
    entry = _shared_entry(capsys)
    assert entry["ok"] is False
    assert entry["fix"] == "facet pull echo"


# --- the shared browser check: fixed by pull, never gating it.


@pytest.fixture
def fake_browser(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeBrowser]:
    """Serve the chromium archive locally and point facet at it."""
    server = FakeBrowser().start()
    monkeypatch.setattr(env_module, "PLAYWRIGHT_CDN", server.base)
    try:
        yield server
    finally:
        server.stop()


def _registry_with_browser(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sha256: str) -> Path:
    """A fixture-registry copy whose echo package declares ``[browser]``."""
    registry_copy = tmp_path / "registry"
    shutil.copytree(FIXTURES, registry_copy)
    manifest = registry_copy / "packages" / "echo" / "package.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + f"""
[browser]
channel = "chromium"
revision = "{BROWSER_REVISION}"
sha256 = "{sha256}"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("FACET_REGISTRY", str(registry_copy))
    return registry_copy


def _browser_entry(data: dict[str, Any]) -> dict[str, Any]:
    entry = data["packages"]["echo"]["readiness"]["browser"]
    assert isinstance(entry, dict)
    return entry


def test_browser_check_is_absent_from_can_run_checks() -> None:
    """What a pull fixes never gates the pull that fixes it."""
    assert "browser" in doctor_module.CHECK_ORDER
    assert "browser" not in doctor_module.CAN_RUN_CHECKS
    assert "node" in doctor_module.CHECK_ORDER
    assert "node" not in doctor_module.CAN_RUN_CHECKS
    assert list(doctor_module.CHECK_ORDER)[-3:] == ["browser", "node", "shared_modules"]


def test_browser_check_fails_before_pull_and_passes_after(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_browser: FakeBrowser,
) -> None:
    _registry_with_browser(monkeypatch, tmp_path, fake_browser.sha256)
    monkeypatch.setenv("FACET_HOME", str(tmp_path / "facet-home"))

    code = main(["doctor", "echo", "--json"])
    assert code == EXIT_NOT_INSTALLED
    entry = _browser_entry(_data(_envelope(capsys)))
    assert entry["ok"] is False
    assert entry["fix"] == "facet pull echo"

    assert main(["pull", "echo", "--json"]) == 0
    capsys.readouterr()

    code = main(["doctor", "echo", "--json"])
    assert code == 0
    data = _data(_envelope(capsys))
    entry = _browser_entry(data)
    assert entry["ok"] is True
    assert data["healthy"] is True


# --- The exit code and the envelope verdict are one derivation.


def test_doctor_empty_home_reports_ok_false_and_exits_nonzero(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FACET_REGISTRY", raising=False)

    code = main(["doctor", "--json"])
    envelope = _envelope(capsys)
    data = _data(envelope)
    assert code == EXIT_MACHINE_CANNOT
    assert envelope["ok"] is False
    assert data["healthy"] is False
    assert data["facet"]["registry"]["ok"] is False


def test_doctor_invalid_registry_snapshot_reports_ok_false_and_exits_nonzero(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    broken = tmp_path / "registry"
    broken.mkdir()
    monkeypatch.setenv("FACET_REGISTRY", str(broken))

    code = main(["doctor", "--json"])
    envelope = _envelope(capsys)
    data = _data(envelope)
    assert code == EXIT_MACHINE_CANNOT
    assert envelope["ok"] is False
    assert data["healthy"] is False
    assert data["facet"]["registry"]["ok"] is False


def test_doctor_healthy_reports_ok_true_and_exits_zero(
    capsys: pytest.CaptureFixture[str], facet_home: Path
) -> None:
    assert facet_home.is_dir()

    code = main(["doctor", "--json"])
    envelope = _envelope(capsys)
    data = _data(envelope)
    assert code == 0
    assert envelope["ok"] is True
    assert data["healthy"] is True
