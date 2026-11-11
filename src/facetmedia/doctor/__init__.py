"""``facet doctor``: is facet healthy, and can this machine run each tool?

``facet doctor`` is about the tool and the machine; ``facet check`` is about
one asset's bytes.  The doctor reports, and never repairs: every failing
check comes with a ``fix`` a person or agent can run.  Its sections are

- **facet**: the interpreter, uv, the home, the state files, the lock, the
  registry snapshot, the disk, orphaned environments and models, the GPU;
- **readiness** (FR3, moved here from ``check.py`` unchanged in logic, each
  check gaining a ``fix``): compatible, platform, ram, disk, gpu, host_libs,
  installed, licenses, models;
- **the package doctor**: the package's own ``doctor`` verb at ``quick``
  depth (imports, files, versions; about 10 s, writes nothing) or ``full``
  depth (may run the tool once on a throwaway output; up to 30 s).

``pull`` and ``run`` gate with the same readiness functions plus the quick
package doctor, through :func:`requirement_errors`.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import uv

from facetmedia import __version__, licenses, lock, prune, shared
from facetmedia.contract import EXIT_CODES, FacetError
from facetmedia.doctor import host as doctor_host
from facetmedia.doctor import node as doctor_node
from facetmedia.doctor.package import package_doctor
from facetmedia.home import Home, require_posix
from facetmedia.probes import (
    NO_HOST_LIBS_HINT,
    gpu_info,
    host_lib_error,
    mem_available_gb,
    mem_total_gb,
    total_mib,
    tree_bytes,
)
from facetmedia.registry import (
    Model,
    Package,
    Registry,
    load_registry,
    override_dir,
    snapshot_commit,
    validate_registry,
)
from facetmedia.registry_types import is_backend

__all__ = [
    "GB",
    "NO_HOST_LIBS_HINT",
    "QUICK_TIMEOUT_S",
    "FULL_TIMEOUT_S",
    "execute",
    "facet_checks",
    "gpu_info",
    "host_lib_error",
    "is_known_target",
    "machine_checks",
    "mem_available_gb",
    "mem_total_gb",
    "package_doctor",
    "requirement_errors",
    "run_check",
    "tree_bytes",
]

GB: Final[int] = 1024**3
#: The package doctor's budget per depth; a timeout is a failed check.  The
#: quick budget is 10 s because cold imports of OCP, onnxruntime and
#: CTranslate2 take several seconds on a CPU machine.
QUICK_TIMEOUT_S: Final[float] = 10.0
FULL_TIMEOUT_S: Final[float] = 30.0
UV_TIMEOUT_S: Final[float] = 30.0
DOCTOR_TIMEOUTS: Final[dict[str, float]] = {"quick": QUICK_TIMEOUT_S, "full": FULL_TIMEOUT_S}

#: Every readiness check, in the spec's order; the ``--json`` data follows it.
CHECK_ORDER: Final[tuple[str, ...]] = (
    "compatible",
    "platform",
    "ram",
    "disk",
    "gpu",
    "host_libs",
    "installed",
    "licenses",
    "models",
    "browser",
    "node",
    "shared_modules",
)

#: The checks ``can_run`` needs: everything but ``installed``, ``models``,
#: ``browser`` and ``shared_modules``, which a pull (which ``run`` does by
#: itself) fixes.
CAN_RUN_CHECKS: Final[tuple[str, ...]] = (
    "compatible",
    "platform",
    "ram",
    "disk",
    "gpu",
    "host_libs",
    "licenses",
)

#: The facet section, in the spec's order.
FACET_CHECK_ORDER: Final[tuple[str, ...]] = (
    "python",
    "uv",
    "home",
    "state",
    "lock",
    "registry",
    "disk",
    "orphans",
    "gpu",
)

#: Exit classes in the order FR8 picks them.
EXIT_ORDER: Final[tuple[str, ...]] = (
    "machine_cannot",
    "license_not_accepted",
    "not_installed",
    "runner_failed",
)


@dataclass(frozen=True)
class Check:
    """One named check: whether it passed, what it saw, and how to fix it."""

    ok: bool
    detail: str
    fix: str = ""
    error: FacetError | None = None


@dataclass(frozen=True)
class PackageSection:
    """One package's readiness, its doctor checks, and their gate failures."""

    installed: bool
    readiness: dict[str, dict[str, object]]
    doctor: list[dict[str, object]]
    failures: list[FacetError]
    doctor_failure: FacetError | None
    needs_pull: bool

    def as_data(self) -> dict[str, object]:
        return {"installed": self.installed, "readiness": self.readiness, "doctor": self.doctor}


# --- the readiness checks moved from check.py, each with its fix.


def requirement_errors(
    package: Package, home: Home, run_id: str, *, timeout_warns: bool = False
) -> tuple[list[FacetError], list[str]]:
    """The gate ``pull`` and ``run`` run first: readiness plus the quick doctor.

    Returns the failures in the spec's order plus the gate's warnings; an
    empty error list means the machine can do it.  The package doctor runs
    only when the environment is already installed: a gate that runs before a
    pull has nothing to call yet.  Only the run gate passes ``timeout_warns``:
    then a quick doctor that merely timed out becomes a warning the run
    carries on with instead of a failure.
    """
    _, failures, doctor_failures = run_check(package, home, "", run_id, depth="quick")
    errors: list[FacetError] = []
    warnings: list[str] = []
    for error in failures + doctor_failures:
        warning = _timeout_warning(error) if timeout_warns else None
        if warning is None:
            errors.append(error)
        else:
            warnings.append(warning)
    return errors, warnings


def _timeout_warning(error: FacetError) -> str | None:
    """The run-gate warning for a doctor that timed out; ``None`` for any other error."""
    if error.details.get("doctor_timeout") is not True:
        return None
    return (
        f"{str(error.details['package'])} doctor did not answer within "
        f"{str(error.details['timeout_s'])} s; running anyway"
    )


def run_check(
    package: Package,
    home: Home,
    capability_id: str,
    run_id: str = "",
    depth: str = "quick",
) -> tuple[dict[str, dict[str, object]], list[FacetError], list[FacetError]]:
    """One capability's readiness: the checks, the gate failures, the doctor's.

    ``capability_id`` names the capability the doctor request carries; when
    empty the package's first capability is used.  Never downloads anything.
    """
    checks, failures = machine_checks(package, home)
    doctor_checks, doctor_failure = package_doctor(
        package, home, run_id, depth, capability_id or _first_capability(package)
    )
    return checks, failures, _doctor_errors(package, doctor_checks, doctor_failure)


def machine_checks(
    package: Package, home: Home
) -> tuple[dict[str, dict[str, object]], list[FacetError]]:
    """Every readiness check plus the gate failures, in the spec's order.

    Never downloads anything and never runs the package.
    """
    entries: dict[str, Check] = {
        "compatible": _compatible(package),
        "platform": _platform(package),
        "ram": _ram(package),
        "disk": _disk(package, home),
        "gpu": _gpu(package),
        "host_libs": _host_libs(package),
        "installed": _installed(package, home),
        "licenses": _licenses(package),
        "models": _models(package, home),
        "browser": _browser(package, home),
        "node": doctor_node.readiness(package, home),
        "shared_modules": Check(
            *shared.check(package, home.env_dir(package.name, package.version))
        ),
    }
    checks = {
        name: {"ok": entries[name].ok, "detail": entries[name].detail, "fix": entries[name].fix}
        for name in CHECK_ORDER
    }
    failures: list[FacetError] = []
    for name in CAN_RUN_CHECKS:
        entry = entries[name]
        if not entry.ok and entry.error is not None:
            failures.append(entry.error)
    return checks, failures


# --- one function per readiness check, in the spec's order.


def _compatible(package: Package) -> Check:
    try:
        package.require_compatible()
    except FacetError as exc:
        return Check(False, exc.message, exc.hint or "", exc)
    return Check(
        True,
        f"requires_facet {package.requires_facet} satisfied by facet {__version__}, "
        f"protocol {package.protocol}",
    )


def _platform(package: Package) -> Check:
    if sys.platform in package.requirements.platforms:
        return Check(True, f"{sys.platform} in [{', '.join(package.requirements.platforms)}]")
    supported = ", ".join(package.requirements.platforms)
    error = FacetError(
        "machine_cannot",
        f"{package.name} does not support this platform ({sys.platform})",
        details={"platform": sys.platform, "platforms": list(package.requirements.platforms)},
    )
    return Check(
        False,
        f"{error.message}; supports [{supported}]",
        f"{package.name} runs on [{supported}]",
        error,
    )


def _ram(package: Package) -> Check:
    needed = package.requirements.ram_gb
    if needed <= 0:
        return Check(True, "the package sets no RAM requirement")
    total = mem_total_gb()
    if total is None:
        error = FacetError("machine_cannot", "cannot read MemTotal from /proc/meminfo")
        return Check(False, error.message, f"{package.name} needs {needed} GB of RAM", error)
    available = mem_available_gb()
    if available is None:
        detail = f"total {total:.2f} GB, need {needed} GB"
    else:
        detail = f"total {total:.2f} GB, available {available:.2f} GB, need {needed} GB"
    if total < needed:
        error = FacetError(
            "machine_cannot",
            f"{package.name} needs {needed} GB of RAM, this machine has {total:.2f} GB",
            details={"required_gb": needed, "available_gb": round(total, 3)},
        )
        return Check(
            False, f"{detail}: {error.message}", f"{package.name} needs {needed} GB of RAM", error
        )
    return Check(True, detail)


def _disk(package: Package, home: Home) -> Check:
    installed = tree_bytes(home.envs_dir) + tree_bytes(home.models_dir)
    free = shutil.disk_usage(home.root).free
    need = max(0, int(package.requirements.disk_gb * 1.1 * GB) - installed)
    detail = (
        f"free {free / GB:.2f} GB, need {need / GB:.2f} GB "
        f"({installed / GB:.2f} GB already installed)"
    )
    fix = f"{package.name} needs {need / GB:.2f} GB free at {home.root}"
    if free < need:
        error = FacetError(
            "machine_cannot",
            f"{fix}: {free / GB:.2f} GB free, {installed / GB:.2f} GB already installed",
            details={"free_bytes": free, "need_bytes": need, "installed_bytes": installed},
        )
        return Check(False, f"{detail}: {error.message}", fix, error)
    return Check(True, detail)


def _gpu(package: Package) -> Check:
    info = gpu_info()
    if not package.requirements.gpu:
        return Check(True, info if info is not None else "none")
    if info is None:
        error = FacetError(
            "machine_cannot",
            f"{package.name} needs a GPU but nvidia-smi reports none",
            details={"gpu": "none"},
        )
        return Check(
            False,
            error.message,
            f"{package.name} needs an NVIDIA GPU with drivers",
            error,
        )
    needs = package.requirements.vram_gb
    if needs <= 0:
        return Check(True, info)
    total = total_mib(info)
    if total is None:
        error = FacetError(
            "machine_cannot",
            f"{package.name} needs {needs} GB of VRAM but the GPU total is unreadable: {info}",
            details={"gpu": info},
        )
        return Check(
            False,
            f"{info}: {error.message}",
            f"{package.name} needs {needs} GB of VRAM",
            error,
        )
    if total < needs * 1024:
        available = round(total / 1024, 3)
        error = FacetError(
            "machine_cannot",
            f"{package.name} needs {needs} GB of VRAM, this GPU has {total / 1024:.1f} GB",
            details={"required_gb": needs, "available_gb": available},
        )
        return Check(
            False,
            f"total {total / 1024:.1f} GB, need {needs} GB: {error.message}",
            f"{package.name} needs {needs} GB of VRAM",
            error,
        )
    return Check(True, f"total {total / 1024:.1f} GB, need {needs} GB")


def _host_libs(package: Package) -> Check:
    error = host_lib_error(package)
    if error is not None:
        return Check(False, error.message, package.requirements.host_hint, error)
    if package.requirements.host_libs:
        return Check(True, f"loaded: {', '.join(package.requirements.host_libs)}")
    return Check(True, "the package needs no host libraries")


def _installed(package: Package, home: Home) -> Check:
    from facetmedia import env as env_module  # noqa: PLC0415

    env_dir = home.env_dir(package.name, package.version)
    if env_module.package_stale(package, home):
        return Check(
            False,
            f"{env_dir}: the package's files changed in the registry since it was installed",
            f"facet pull {package.name}",
        )
    node_check = doctor_host.engine_installed_check(package, home)
    if node_check is not None:
        return node_check
    if env_module.is_installed(package, home):
        return Check(True, f"installed at {env_dir}")
    detail = f"{env_dir}: the environment is not complete"
    if package.server is not None and env_module.client_env_present(package, home):
        detail = f"{env_dir}: the server checkout is not verified"
    return Check(False, detail, f"facet pull {package.name}")


def _licenses(package: Package) -> Check:
    wanted = [model for model in package.models if model.requires_acceptance]
    if not wanted:
        return Check(True, "no model requires acceptance")
    try:
        licenses.require(package.models)
    except FacetError as exc:
        missing = _missing_models(exc)
        commands = "; ".join(f"facet license accept {model}" for model in sorted(missing))
        return Check(False, _license_detail(wanted, missing), commands, exc)
    return Check(True, _license_detail(wanted, set()))


def _missing_models(error: FacetError) -> set[str]:
    """The model ids ``licenses.require`` reported as not accepted."""
    entries = error.details.get("models")
    if not isinstance(entries, list):
        return set()
    return {str(entry["id"]) for entry in entries if isinstance(entry, dict) and "id" in entry}


def _license_detail(wanted: list[Model], missing: set[str]) -> str:
    """One ``id (spdx): accepted|not accepted`` fragment per model."""
    return "; ".join(
        f"{model.id} ({model.spdx}): {'not accepted' if model.id in missing else 'accepted'}"
        for model in wanted
    )


def _models(package: Package, home: Home) -> Check:
    from facetmedia import env as env_module  # noqa: PLC0415

    missing = [
        model.id
        for model in package.models
        if not is_backend(model) and not env_module.model_present(model, home)
    ]
    if missing:
        return Check(False, f"missing: {', '.join(missing)}", f"facet pull {package.name}")
    if package.models:
        return Check(True, _models_detail(package))
    return Check(True, "the package has no models")


def _models_detail(package: Package) -> str:
    """``N model(s) present`` plus one ``backend-owned: <id>`` per owned model."""
    detail = f"{len(package.models)} model(s) present"
    owned = [f"backend-owned: {model.id}" for model in package.models if is_backend(model)]
    if owned:
        detail += f"; {', '.join(owned)}"
    return detail


def _browser(package: Package, home: Home) -> Check:
    from facetmedia import env as env_module  # noqa: PLC0415

    browser = package.browser
    if browser is None:
        return Check(True, "no browser declared")
    target = env_module.browser_dir(browser, home)
    if env_module.browser_present(browser, home):
        return Check(True, f"{browser.channel}-{browser.revision} verified at {target}")
    return Check(False, f"{target}: the extraction is not verified", f"facet pull {package.name}")


# --- the facet section: facet itself, its state, and this machine.


def facet_checks(home: Home) -> list[dict[str, object]]:
    """The facet checks of FR2, in order, as ``{"name", "ok", "detail", "fix"}``."""
    entries = [
        _python_check(),
        _uv_check(),
        _home_check(home),
        _state_check(home),
        _lock_check(home),
        _registry_check(home),
        _disk_free_check(home),
        _orphans_check(home),
        _gpu_present_check(),
    ]
    return [
        {"name": name, "ok": entry.ok, "detail": entry.detail, "fix": entry.fix}
        for name, entry in zip(FACET_CHECK_ORDER, entries, strict=True)
    ]


def _python_check() -> Check:
    return Check(True, f"python {sys.version.split()[0]}")


def _uv_check() -> Check:
    try:
        binary = uv.find_uv_bin()
        completed = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, check=False, timeout=UV_TIMEOUT_S
        )
    except Exception as exc:  # noqa: PLW0718 - uv errors are not facet's contract
        return Check(False, f"uv cannot run: {exc}", "reinstall facet; uv ships with it")
    if completed.returncode != 0:
        output = (completed.stderr or completed.stdout).strip()
        return Check(
            False,
            f"uv exited {completed.returncode}: {output}",
            "reinstall facet; uv ships with it",
        )
    return Check(True, completed.stdout.strip() or "uv")


def _home_check(home: Home) -> Check:
    try:
        require_posix()
    except FacetError as exc:
        return Check(False, exc.message, "use WSL; native Windows is not supported")
    if not home.root.is_dir():
        return Check(False, f"{home.root} does not exist", f"create {home.root}, or set FACET_HOME")
    if not os.access(home.root, os.W_OK):
        return Check(
            False, f"{home.root} is not writable", f"give your user write access to {home.root}"
        )
    return Check(True, f"{home.root} exists, is writable, and is POSIX")


def _state_check(home: Home) -> Check:
    problems = []
    for path in sorted((home.root / "state").glob("*.json")):
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"{path.name} does not parse ({exc})")
    if problems:
        return Check(
            False, "; ".join(problems), f"fix or remove the named file under {home.root / 'state'}"
        )
    return Check(True, "every state file parses")


def _lock_check(home: Home) -> Check:
    """The lock is free, held by a live process, or carries stale information.

    The probe is a non-blocking exclusive ``flock`` on a second descriptor;
    the kernel is the truth, ``lock.json`` only names the holder.  A stale
    holder is reported and never removed.
    """
    fd = os.open(home.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _held_lock_check(home)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    return _free_lock_check(home)


def _free_lock_check(home: Home) -> Check:
    holder, stale = lock.holder_info(home)
    if holder is not None:
        return Check(
            False, f"free, but state/lock.json still names {holder}", f"remove {home.lock_json}"
        )
    if stale is not None:
        return Check(False, f"free, but {stale}", f"remove {home.lock_json}")
    return Check(True, "free")


def _held_lock_check(home: Home) -> Check:
    holder, stale = lock.holder_info(home)
    if holder is not None:
        return Check(True, f"held by {holder}")
    note = stale or "held by another process"
    return Check(True, f"busy: {note}; the flock is the truth")


def _registry_check(home: Home) -> Check:
    path = _registry_path(home)
    if path is None:
        return Check(False, "no registry snapshot is pinned", "run facet update")
    problems = validate_registry(path)
    if problems:
        first = problems[0]
        return Check(
            False,
            f"{path.name} does not validate: {first['file']}: {first['key']}: {first['problem']}",
            _registry_fix(),
        )
    return Check(True, f"{path.name}: at the pinned commit, validates")


def _disk_free_check(home: Home) -> Check:
    try:
        free = shutil.disk_usage(home.root).free
    except OSError as exc:
        return Check(False, f"cannot measure free disk at {home.root}: {exc}", f"check {home.root}")
    return Check(True, f"free {free / GB:.2f} GB at {home.root}")


#: How many orphaned paths the facet section names in its detail line.
ORPHAN_LISTED: Final[int] = 3


def _orphans_check(home: Home) -> Check:
    path = _registry_path(home)
    if path is None:
        return Check(True, "skipped: no registry snapshot is in use")
    try:
        registry = load_registry(path)
    except FacetError as exc:
        return Check(False, f"cannot list orphans: {exc.message}", _registry_fix())
    items = prune.orphans(home, registry)
    if not items:
        return Check(True, "no orphaned environments, servers or models")
    listed = ", ".join(str(item.path) for item in items[:ORPHAN_LISTED])
    more = "" if len(items) <= ORPHAN_LISTED else f" (+{len(items) - ORPHAN_LISTED} more)"
    return Check(False, f"{len(items)} orphaned: {listed}{more}", "facet clean")


def _gpu_present_check() -> Check:
    info = gpu_info()
    return Check(True, info if info is not None else "none")


def _registry_path(home: Home) -> Path | None:
    """The registry directory in use, without ever fetching one.

    ``FACET_REGISTRY`` wins; otherwise the snapshot ``state/registry.json``
    pinned.  ``registry_dir`` is never called: it would download the registry
    on first use, and the doctor never downloads.
    """
    override = override_dir()
    if override is not None:
        return override
    commit = snapshot_commit()
    if commit is None:
        return None
    return home.registry_dir(commit)


def _registry_fix() -> str:
    return "fix the FACET_REGISTRY checkout" if override_dir() is not None else "run facet update"


def _doctor_errors(
    package: Package, checks: list[dict[str, object]], failure: FacetError | None
) -> list[FacetError]:
    """The doctor as gate failures: the classified failure, or each failed check.

    A check whose fix is ``facet pull <package>`` never gates: the pull it
    names is the very thing the gate runs before, so refusing here would
    refuse the fix (the browser check works the same way).
    """
    if failure is not None:
        return [failure]
    errors: list[FacetError] = []
    for entry in checks:
        if entry["ok"] is True:
            continue
        fix = entry.get("fix")
        if fix == f"facet pull {package.name}":
            continue
        errors.append(
            FacetError(
                "machine_cannot",
                f"{package.name} doctor: {entry['name']}: {entry['detail']}",
                hint=fix if isinstance(fix, str) and fix else None,
                details={"package": package.name, "check": str(entry["name"])},
            )
        )
    return errors


# --- the doctor command itself.


def execute(
    target: str,
    depth: str,
    run_id: str,
    wait: bool = True,
    timeout_s: float = 3600.0,
) -> tuple[dict[str, object], int]:
    """Run the doctor; returns the ``--json`` data and the process exit code."""
    if depth not in DOCTOR_TIMEOUTS:
        raise FacetError("invalid_input", f"unknown doctor depth: {depth}")
    home = Home.from_env()
    facet_entries = facet_checks(home)
    selected, capability_id = _select(target, home)
    sections: dict[str, PackageSection] = {}
    classes: list[str] = _facet_classes(facet_entries)
    with lock.hold("shared", "facet doctor", wait, timeout_s):
        for package, capability in selected:
            section = _package_section(package, home, run_id, depth, capability)
            sections[package.name] = section
            classes.extend(_section_classes(section, named=bool(target)))
    healthy = not classes
    data: dict[str, object] = {
        "healthy": healthy,
        "facet": _facet_data(facet_entries),
        "packages": {name: section.as_data() for name, section in sections.items()},
    }
    if capability_id is not None and len(selected) == 1:
        data["capability"] = capability_id
        data.update(_capability_report(sections[selected[0][0].name], capability_id))
    return data, 0 if healthy else _exit_code(classes)


def _select(target: str, home: Home) -> tuple[list[tuple[Package, str]], str | None]:
    """The ``(package, capability)`` pairs to examine, plus the capability id.

    The registry is read from the snapshot in use and never fetched: a doctor
    that had to download the registry to run would not be a doctor.
    """
    if target:
        registry = _loaded_registry(home)
        package = registry.package(target)
        if package is not None:
            return [(package, _first_capability(package))], None
        capability = next((cap for cap in registry.capabilities() if cap.id == target), None)
        if capability is None:
            raise FacetError(
                "invalid_input",
                f"unknown target: {target}",
                details={"known": sorted(registry.packages)},
            )
        owner = registry.package(capability.package)
        if owner is None:
            raise FacetError(
                "machine_cannot",
                f"{target}: its package {capability.package} is not in the registry",
            )
        return [(owner, target)], target
    return _installed_pairs(home), None


def _installed_pairs(home: Home) -> list[tuple[Package, str]]:
    path = _registry_path(home)
    if path is None:
        return []
    try:
        registry = load_registry(path)
    except FacetError:
        return []  # the facet registry check already reports this
    return [
        (package, _first_capability(package))
        for package in registry.packages.values()
        if _is_installed(package, home)
    ]


def is_known_target(target: str, home: Home) -> bool:
    """Whether ``target`` names a package or a capability id, never fetching."""
    try:
        registry = _loaded_registry(home)
    except FacetError:
        return False
    if registry.package(target) is not None:
        return True
    return any(capability.id == target for capability in registry.capabilities())


def _loaded_registry(home: Home) -> Registry:
    path = _registry_path(home)
    if path is None:
        raise FacetError(
            "machine_cannot",
            "no registry snapshot is pinned, so the target cannot be resolved",
            hint="run facet update",
        )
    try:
        return load_registry(path)
    except FacetError as exc:
        raise FacetError(
            "machine_cannot",
            f"the registry at {path} does not load: {exc.message}",
            hint=_registry_fix(),
        ) from exc


def _is_installed(package: Package, home: Home) -> bool:
    from facetmedia import env as env_module  # noqa: PLC0415

    return env_module.is_installed(package, home)


def _package_section(
    package: Package, home: Home, run_id: str, depth: str, capability: str
) -> PackageSection:
    """One package's readiness and its doctor checks, with their classifications."""
    readiness, failures = machine_checks(package, home)
    checks, doctor_failure = package_doctor(package, home, run_id, depth, capability)
    return PackageSection(
        installed=readiness["installed"]["ok"] is True,
        readiness=readiness,
        doctor=checks,
        failures=failures,
        doctor_failure=doctor_failure,
        needs_pull=readiness["installed"]["ok"] is not True
        or readiness["models"]["ok"] is not True
        or readiness["browser"]["ok"] is not True
        or readiness["node"]["ok"] is not True
        or readiness["shared_modules"]["ok"] is not True,
    )


def _facet_data(entries: list[dict[str, object]]) -> dict[str, object]:
    """The facet section keyed by check name, each check `{ok, detail, fix}`."""
    return {
        str(entry["name"]): {"ok": entry["ok"], "detail": entry["detail"], "fix": entry["fix"]}
        for entry in entries
    }


def _facet_classes(entries: list[dict[str, object]]) -> list[str]:
    return ["machine_cannot"] if any(entry["ok"] is not True for entry in entries) else []


def _section_classes(section: PackageSection, named: bool) -> list[str]:
    classes = [error.code for error in section.failures]
    if section.doctor_failure is not None:
        classes.append(section.doctor_failure.code)
    elif any(entry["ok"] is not True for entry in section.doctor):
        classes.append("machine_cannot")
    if named and not section.installed:
        classes.append("not_installed")
    return classes


def _capability_report(section: PackageSection, capability_id: str) -> dict[str, object]:
    """``capability``, ``can_run`` and ``needs_pull`` for a capability target."""
    doctor_failures = [entry for entry in section.doctor if entry["ok"] is not True]
    return {
        "capability": capability_id,
        "can_run": not section.failures and not doctor_failures,
        "needs_pull": section.needs_pull,
    }


def _exit_code(classes: list[str]) -> int:
    for code in EXIT_ORDER:
        if code in classes:
            return EXIT_CODES[code]
    if classes:
        return EXIT_CODES.get(classes[0], 1)
    return 0


def _first_capability(package: Package) -> str:
    return package.capabilities[0].id if package.capabilities else package.name
