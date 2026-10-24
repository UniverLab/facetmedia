"""``facet check``: can this machine run it, and does this asset still pass?

The first form, ``facet check <capability-id>``, answers the question a
client asks before making anything: it reports, without installing or
downloading anything, whether the package behind the capability is
compatible, the platform, RAM and disk suffice, a GPU is present when one is
required, every host library loads, the environment is installed, the
licences are accepted, the models are present, and (when installed) the
runner's ``probe`` verb answers.

The second form, ``facet check <capability-id> <asset-or-record>``,
re-verifies an asset facet made earlier: it reads the asset's record, fails
with ``verification_failed`` when the bytes changed, then calls the runner's
``verify`` verb with the record's params and reports the fresh metrics next
to the recorded ones. The record file is never modified.

``pull`` and ``run`` run the same gate (the machine checks, without the
probe) before doing anything, through :func:`requirement_errors`.
"""

from __future__ import annotations

import ctypes
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from facetmedia import __version__, licenses, record, runner
from facetmedia.contract import FacetError
from facetmedia.home import Home
from facetmedia.registry import Capability, Model, Package, load_active_registry

__all__ = [
    "GB",
    "NO_HOST_LIBS_HINT",
    "gpu_info",
    "host_lib_error",
    "machine_checks",
    "mem_available_gb",
    "mem_total_gb",
    "reverify",
    "requirement_errors",
    "resolve",
    "run_check",
    "tree_bytes",
]

GB: Final[int] = 1024**3
NO_HOST_LIBS_HINT: Final[str] = "facet never installs host libraries"
NVIDIA_SMI_TIMEOUT_S: Final[float] = 2.0
PROBE_TIMEOUT_S: Final[float] = 60.0

#: Every check, in the spec's order; the ``--json`` data follows it.
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
    "probe",
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
    "probe",
)


@dataclass(frozen=True)
class Check:
    """One named check: whether it passed, what it saw, and its failure."""

    ok: bool
    detail: str
    error: FacetError | None = None


def resolve(capability_id: str) -> tuple[Capability, Package]:
    """The capability and its package; an unknown id lists the known ids."""
    active = load_active_registry()
    capability = active.capability(capability_id)
    package = active.package(capability.package)
    if package is None:
        raise FacetError(
            "internal",
            f"{capability_id}: its package {capability.package} is not in the registry",
        )
    return capability, package


def requirement_errors(package: Package, home: Home) -> list[FacetError]:
    """The gate ``pull`` and ``run`` run first: the machine checks, no probe.

    Returns the failures in the spec's order; an empty list means the machine
    can do it. The probe is a ``check``-only check: it needs an installed
    environment and a working runner.
    """
    entries = (
        _compatible(package),
        _platform(package),
        _ram(package),
        _disk(package, home),
        _gpu(package),
        _host_libs(package),
        _licenses(package),
    )
    return [entry.error for entry in entries if not entry.ok and entry.error is not None]


def machine_checks(
    package: Package,
    home: Home,
    run_id: str,
    capability: str = "",
) -> tuple[dict[str, dict[str, object]], list[FacetError]]:
    """Every check plus the gate failures, in the spec's order.

    ``capability`` names the capability the probe request carries; when empty
    the package's first capability is used. Never downloads anything.
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
    probe_capability = capability or _first_capability(package)
    entries["probe"] = _probe(package, home, run_id, probe_capability, entries["installed"].ok)
    checks = {
        name: {"ok": entries[name].ok, "detail": entries[name].detail} for name in CHECK_ORDER
    }
    failures: list[FacetError] = []
    for name in CAN_RUN_CHECKS:
        entry = entries[name]
        if not entry.ok and entry.error is not None:
            failures.append(entry.error)
    return checks, failures


def run_check(capability_id: str, home: Home, run_id: str) -> tuple[dict[str, object], int]:
    """The machine-only form: report readiness without installing anything."""
    _capability, package = resolve(capability_id)
    checks, failures = machine_checks(package, home, run_id, capability_id)
    can_run = not failures
    needs_pull = not checks["installed"]["ok"] or not checks["models"]["ok"]
    data: dict[str, object] = {
        "capability": capability_id,
        "can_run": can_run,
        "needs_pull": needs_pull,
        "checks": checks,
    }
    code = 0 if can_run else failures[0].exit_code
    return data, code


def reverify(
    capability_id: str, asset: str, home: Home, run_id: str
) -> tuple[dict[str, object], int]:
    """Re-verify an existing asset against its record; the record is untouched."""
    stored = record.read_record(asset)
    provenance = stored.get("provenance")
    if not isinstance(provenance, dict):
        raise FacetError("invalid_input", f"{asset}: the record has no provenance")
    recorded_capability = provenance.get("capability")
    if recorded_capability != capability_id:
        raise FacetError(
            "invalid_input",
            f"{asset}: its record names capability {recorded_capability}, not {capability_id}",
            details={"capability": capability_id, "recorded": recorded_capability},
        )
    if not record.verify_integrity(asset):
        raise FacetError(
            "verification_failed",
            f"{asset}: the bytes changed since the record was written",
            details={"asset": asset, "record": str(record.record_path(asset))},
        )
    _capability, package = resolve(capability_id)
    package.require_compatible()
    from facetmedia import env as env_module  # noqa: PLC0415

    env_dir = home.env_dir(package.name, package.version)
    if not env_module.is_installed(package, home):
        raise FacetError(
            "not_installed",
            f"{package.name} {package.version} is not installed",
            hint=f"run facet pull {package.name}",
            details={"package": package.name, "version": package.version},
        )
    params_value = provenance.get("params")
    if not isinstance(params_value, dict):
        raise FacetError("invalid_input", f"{asset}: the record has no params")
    params: dict[str, object] = dict(params_value)
    asset_path = _asset_path(asset)
    verdict = _verify_asset(package, home, env_dir, run_id, capability_id, params, asset_path)
    recorded = stored.get("verification")
    recorded_metrics: dict[str, object] = {}
    if isinstance(recorded, dict):
        metrics = recorded.get("metrics")
        if isinstance(metrics, dict):
            recorded_metrics = dict(metrics)
    data: dict[str, object] = {
        "capability": capability_id,
        "asset": str(asset_path),
        "passed": verdict.passed,
        "recorded": {"status": _recorded_status(recorded), "metrics": recorded_metrics},
        "fresh": {
            "passed": verdict.passed,
            "metrics": dict(verdict.metrics),
            "thresholds": dict(verdict.thresholds),
            "verifier": verdict.verifier,
        },
    }
    return data, 0 if verdict.passed else 3


def _recorded_status(recorded: object) -> object:
    if isinstance(recorded, dict):
        return recorded.get("status")
    return None


# --- the shared machine probes (env.py imports these; never copy them).


def mem_total_gb() -> float | None:
    """Total RAM from ``/proc/meminfo`` in GB, or ``None`` when unreadable."""
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key != "MemTotal":
            continue
        try:
            return int(rest.split()[0]) / (1024 * 1024)
        except (IndexError, ValueError):
            return None
    return None


def mem_available_gb() -> float | None:
    """Available RAM from ``/proc/meminfo`` in GB, or ``None`` when unknown."""
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key != "MemAvailable":
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


# --- one function per check, in the spec's order.


def _compatible(package: Package) -> Check:
    try:
        package.require_compatible()
    except FacetError as exc:
        return Check(False, exc.message, exc)
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
    return Check(False, f"{error.message}; supports [{supported}]", error)


def _ram(package: Package) -> Check:
    needed = package.requirements.ram_gb
    if needed <= 0:
        return Check(True, "the package sets no RAM requirement")
    total = mem_total_gb()
    if total is None:
        error = FacetError("machine_cannot", "cannot read MemTotal from /proc/meminfo")
        return Check(False, error.message, error)
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
        return Check(False, f"{detail}: {error.message}", error)
    return Check(True, detail)


def _disk(package: Package, home: Home) -> Check:
    installed = tree_bytes(home.envs_dir) + tree_bytes(home.models_dir)
    free = shutil.disk_usage(home.root).free
    need = max(0, int(package.requirements.disk_gb * 1.1 * GB) - installed)
    detail = (
        f"free {free / GB:.2f} GB, need {need / GB:.2f} GB "
        f"({installed / GB:.2f} GB already installed)"
    )
    if free < need:
        error = FacetError(
            "machine_cannot",
            f"{package.name} needs {need / GB:.2f} GB free at {home.root}: "
            f"{free / GB:.2f} GB free, {installed / GB:.2f} GB already installed",
            details={"free_bytes": free, "need_bytes": need, "installed_bytes": installed},
        )
        return Check(False, detail, error)
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
        return Check(False, error.message, error)
    return Check(True, info)


def _host_libs(package: Package) -> Check:
    error = host_lib_error(package)
    if error is not None:
        return Check(False, error.message, error)
    if package.requirements.host_libs:
        return Check(True, f"loaded: {', '.join(package.requirements.host_libs)}")
    return Check(True, "the package needs no host libraries")


def _installed(package: Package, home: Home) -> Check:
    from facetmedia import env as env_module  # noqa: PLC0415

    env_dir = home.env_dir(package.name, package.version)
    if env_module.is_installed(package, home):
        return Check(True, f"installed at {env_dir}")
    return Check(False, f"{env_dir}: the environment is not complete")


def _licenses(package: Package) -> Check:
    wanted = [model for model in package.models if model.requires_acceptance]
    if not wanted:
        return Check(True, "no model requires acceptance")
    try:
        licenses.require(package.models)
    except FacetError as exc:
        missing = _missing_models(exc)
        return Check(False, _license_detail(wanted, missing), exc)
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
        return Check(False, f"missing: {', '.join(missing)}")
    if package.models:
        return Check(True, f"{len(package.models)} model(s) present")
    return Check(True, "the package has no models")


def _probe(package: Package, home: Home, run_id: str, capability: str, installed: bool) -> Check:
    if not installed:
        return Check(True, f"skipped: {package.name} {package.version} is not installed")
    temp_dir = home.tmp_dir / f".facet-check-{run_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        invocation = runner.Invocation(
            package,
            home,
            home.env_dir(package.name, package.version),
            temp_dir,
            run_id,
            PROBE_TIMEOUT_S,
        )
        request = runner.make_request(
            "probe", capability, {}, temp_dir, Path.cwd(), runner.model_paths(package, home)
        )
        outcome = runner.call(invocation, "probe", request)
    except FacetError as exc:
        error = FacetError(
            "machine_cannot",
            f"probe failed: {exc.message}",
            details={"error": exc.code, **exc.details},
        )
        return Check(False, error.message, error)
    result = _probe_result(outcome.result, temp_dir)
    if result.ok:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return result


def _probe_result(result: Any, temp_dir: Path) -> Check:
    if not isinstance(result, dict):
        error = FacetError(
            "machine_cannot",
            "probe failed: the result is not a JSON object",
            details={"temp": str(temp_dir)},
        )
        return Check(False, error.message, error)
    if result.get("ok") is True:
        detail = result.get("detail")
        return Check(True, str(detail) if isinstance(detail, str) else "probe ok")
    error = FacetError(
        "machine_cannot",
        f"probe failed: {result!r}",
        details={"temp": str(temp_dir)},
    )
    return Check(False, error.message, error)


def _first_capability(package: Package) -> str:
    if package.capabilities:
        return package.capabilities[0].id
    return package.name


def _asset_path(asset: str) -> Path:
    """The asset a ``check`` argument names, whether it is the asset or record."""
    given = Path(asset)
    if given.name.endswith(record.RECORD_SUFFIX):
        given = Path(str(given)[: -len(record.RECORD_SUFFIX)])
    if not given.is_absolute():
        given = Path.cwd() / given
    return given


def _verify_asset(
    package: Package,
    home: Home,
    env_dir: Path,
    run_id: str,
    capability_id: str,
    params: dict[str, object],
    asset_path: Path,
) -> runner.VerifyResult:
    """Run the runner's ``verify`` verb on one asset with the record's params."""
    temp_dir = home.tmp_dir / f".facet-check-verify-{run_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        invocation = runner.Invocation(package, home, env_dir, temp_dir, run_id, PROBE_TIMEOUT_S)
        models = runner.model_paths(package, home)
        request = runner.make_request(
            "verify",
            capability_id,
            params,
            temp_dir,
            asset_path.parent,
            models,
            [asset_path],
        )
        outcome = runner.call(invocation, "verify", request)
        stub = runner.RunAsset(
            file=asset_path.name,
            name=asset_path.name,
            kind="asset",
            mime="application/octet-stream",
        )
        verdicts = runner.parse_verify_result(outcome.result, [stub], temp_dir)
    except FacetError:
        raise
    else:
        shutil.rmtree(temp_dir)
        return verdicts[asset_path.name]
