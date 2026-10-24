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
  depth (imports, files, versions; about 2 s, writes nothing) or ``full``
  depth (may run the tool once on a throwaway output; up to 30 s).

``pull`` and ``run`` gate with the same readiness functions plus the quick
package doctor, through :func:`requirement_errors`.
"""

from __future__ import annotations

import ctypes
import fcntl
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import uv

from facetmedia import __version__, licenses, lock, prune, runner
from facetmedia.contract import EXIT_CODES, FacetError
from facetmedia.home import Home, require_posix
from facetmedia.registry import (
    Model,
    Package,
    Registry,
    load_registry,
    override_dir,
    snapshot_commit,
    validate_registry,
)

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
NO_HOST_LIBS_HINT: Final[str] = "facet never installs host libraries"
NVIDIA_SMI_TIMEOUT_S: Final[float] = 2.0
#: The package doctor's budget per depth; a timeout is a failed check.
QUICK_TIMEOUT_S: Final[float] = 2.0
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
)

#: The checks ``can_run`` needs: everything but ``installed`` and ``models``,
#: which a pull (which ``run`` does by itself) fixes.
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


def requirement_errors(package: Package, home: Home, run_id: str) -> list[FacetError]:
    """The gate ``pull`` and ``run`` run first: readiness plus the quick doctor.

    Returns the failures in the spec's order; an empty list means the machine
    can do it.  The package doctor runs only when the environment is already
    installed: a gate that runs before a pull has nothing to call yet.
    """
    _, failures, doctor_failures = run_check(package, home, "", run_id, depth="quick")
    return failures + doctor_failures


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
    return Check(True, info)


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
    if env_module.is_installed(package, home):
        return Check(True, f"installed at {env_dir}")
    return Check(False, f"{env_dir}: the environment is not complete", f"facet pull {package.name}")


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

    missing = [model.id for model in package.models if not env_module.model_present(model, home)]
    if missing:
        return Check(False, f"missing: {', '.join(missing)}", f"facet pull {package.name}")
    if package.models:
        return Check(True, f"{len(package.models)} model(s) present")
    return Check(True, "the package has no models")


# --- the shared machine probes (env.py imports these; never copy them).


def mem_total_gb() -> float | None:
    """Total RAM from ``/proc/meminfo`` in GB, or ``None`` when unreadable."""
    return _meminfo_gb("MemTotal")


def mem_available_gb() -> float | None:
    """Available RAM from ``/proc/meminfo`` in GB, or ``None`` when unknown."""
    return _meminfo_gb("MemAvailable")


def _meminfo_gb(key: str) -> float | None:
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name != key:
            continue
        try:
            return int(rest.split()[0]) / (1024 * 1024)
        except (IndexError, ValueError):
            return None
    return None


def tree_bytes(path: Path) -> int:
    """Bytes used by the files under ``path``; 0 when it does not exist."""
    if not path.exists():
        return 0
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def gpu_info() -> str | None:
    """One line per GPU from ``nvidia-smi``, or ``None`` when there is none."""
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=NVIDIA_SMI_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    text = completed.stdout.strip()
    return text or None


def host_lib_error(package: Package) -> FacetError | None:
    """The first soname that does not load, or ``None`` when all load."""
    for soname in package.requirements.host_libs:
        try:
            ctypes.CDLL(soname)
        except OSError as exc:
            return FacetError(
                "machine_cannot",
                f"cannot load {soname}: {exc}; {NO_HOST_LIBS_HINT}",
                hint=package.requirements.host_hint or None,
                details={"soname": soname},
            )
    return None


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
        return Check(True, "no orphaned environments or models")
    listed = ", ".join(str(item.path) for item in items[:ORPHAN_LISTED])
    more = "" if len(items) <= ORPHAN_LISTED else f" (+{len(items) - ORPHAN_LISTED} more)"
    return Check(False, f"{len(items)} orphaned: {listed}{more}", "facet gc")


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


# --- the package doctor: the package's own doctor verb, one depth deep.


def package_doctor(
    package: Package, home: Home, run_id: str, depth: str, capability: str
) -> tuple[list[dict[str, object]], FacetError | None]:
    """One package's ``doctor`` verb: its checks plus a failure to classify.

    An uninstalled environment is skipped with an ``ok`` check.  A timeout is
    a failed check of class ``machine_cannot``; a crash or a broken protocol
    is ``runner_failed``.  The temporary directory is always removed: a
    doctor leaves nothing behind.
    """
    from facetmedia import env as env_module  # noqa: PLC0415

    if not env_module.is_installed(package, home):
        return [_doctor_skip(package, home)], None
    temp_dir = home.tmp_dir / f".facet-doctor-{run_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        return _doctor_call(package, home, run_id, depth, capability, temp_dir)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _doctor_skip(package: Package, home: Home) -> dict[str, object]:
    env_dir = home.env_dir(package.name, package.version)
    return {
        "name": "doctor",
        "ok": True,
        "detail": f"skipped: {package.name} {package.version} is not installed at {env_dir}",
        "fix": "",
    }


def _doctor_call(
    package: Package, home: Home, run_id: str, depth: str, capability: str, temp_dir: Path
) -> tuple[list[dict[str, object]], FacetError | None]:
    timeout_s = DOCTOR_TIMEOUTS.get(depth, QUICK_TIMEOUT_S)
    try:
        invocation = runner.Invocation(
            package,
            home,
            home.env_dir(package.name, package.version),
            temp_dir,
            run_id,
            timeout_s,
        )
        request = runner.make_request(
            "doctor", capability, {}, temp_dir, Path.cwd(), runner.model_paths(package, home)
        )
        request["depth"] = depth
        outcome = runner.call(invocation, "doctor", request)
        return _doctor_result(package, outcome.result, temp_dir)
    except FacetError as exc:
        return [_failed_doctor_check(package, exc)], _classify_doctor_error(package, exc)


def _failed_doctor_check(package: Package, error: FacetError) -> dict[str, object]:
    return {
        "name": "doctor",
        "ok": False,
        "detail": f"{package.name} doctor: {error.message}",
        "fix": "",
    }


def _classify_doctor_error(package: Package, error: FacetError) -> FacetError:
    """A timeout is ``machine_cannot``; a crash or bad protocol stays as it is."""
    timed_out = error.code == "runner_failed" and error.message.startswith(
        "the runner did not finish within"
    )
    if timed_out:
        return FacetError(
            "machine_cannot",
            f"{package.name} doctor: {error.message}",
            details={"package": package.name},
        )
    return error


def _doctor_result(
    package: Package, result: Any, temp_dir: Path
) -> tuple[list[dict[str, object]], FacetError | None]:
    entries = _doctor_entries(result.get("checks") if isinstance(result, dict) else None)
    if entries:
        return entries, None
    if isinstance(result, dict) and result.get("ok") is False:
        detail = str(result.get("detail") or "this package has no doctor")
        entry = {
            "name": "doctor",
            "ok": False,
            "detail": detail,
            "fix": str(result.get("fix") or ""),
        }
        error = FacetError(
            "machine_cannot", f"{package.name}: {detail}", details={"package": package.name}
        )
        return [entry], error
    raise FacetError(
        "runner_failed",
        f"{package.name}: the doctor result has no checks",
        details={"package": package.name, "temp": str(temp_dir)},
    )


def _doctor_entries(value: Any) -> list[dict[str, object]]:
    """The runner's checks, or ``[]`` when the list is missing or malformed."""
    if not isinstance(value, list) or not value:
        return []
    entries: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            return []
        if type(item.get("ok")) is not bool:
            return []
        detail = item.get("detail")
        fix = item.get("fix")
        entries.append(
            {
                "name": item["name"],
                "ok": item["ok"],
                "detail": detail if isinstance(detail, str) else "",
                "fix": fix if isinstance(fix, str) else "",
            }
        )
    return entries


def _doctor_errors(
    package: Package, checks: list[dict[str, object]], failure: FacetError | None
) -> list[FacetError]:
    """The doctor as gate failures: the classified failure, or each failed check."""
    if failure is not None:
        return [failure]
    errors: list[FacetError] = []
    for entry in checks:
        if entry["ok"] is True:
            continue
        fix = entry.get("fix")
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
    quick: bool,
    run_id: str,
    wait: bool = True,
    timeout_s: float = 3600.0,
) -> tuple[dict[str, object], int]:
    """Run the doctor; returns the ``--json`` data and the process exit code."""
    home = Home.from_env()
    facet_entries = facet_checks(home)
    selected, capability_id = _select(target, home)
    depth = "quick" if quick else "full"
    sections: dict[str, PackageSection] = {}
    classes: list[str] = _facet_classes(facet_entries)
    with lock.hold("shared", "facet doctor", wait, timeout_s):
        for package, capability in selected:
            section = _package_section(package, home, run_id, depth, capability)
            sections[package.name] = section
            classes.extend(_section_classes(section, named=bool(target)))
    data: dict[str, object] = {
        "healthy": not classes,
        "facet": _facet_data(facet_entries),
        "packages": {name: section.as_data() for name, section in sections.items()},
    }
    if capability_id is not None and len(selected) == 1:
        data["capability"] = capability_id
        data.update(_capability_report(sections[selected[0][0].name], capability_id))
    return data, _exit_code(classes)


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
        or readiness["models"]["ok"] is not True,
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
    return 0


def _first_capability(package: Package) -> str:
    if package.capabilities:
        return package.capabilities[0].id
    return package.name
