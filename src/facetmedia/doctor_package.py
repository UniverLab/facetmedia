"""One package's ``doctor`` verb, split out for file size.

``doctor`` stays the owner of the check ordering and the
pull-fixable/machine-cannot split; this module runs the package's own
``doctor`` verb at ``quick`` depth and renders the readiness entries for a
package whose environment, browser checkout or server checkout is missing.
The ``env`` module is imported lazily because it imports ``doctor`` back.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from facetmedia import doctor_host, doctor_node, runner
from facetmedia.contract import FacetError
from facetmedia.home import Home
from facetmedia.registry_types import Package


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

    if (engine := doctor_host.engine_package_doctor(package, home)) is not None:
        return engine
    if not env_module.client_env_present(package, home):
        return [_doctor_skip(package, home)], None
    node_entry = doctor_node.doctor_entry(package, home)
    if node_entry is not None:
        return [node_entry], None
    if package.browser is not None and not env_module.browser_present(package.browser, home):
        return [_browser_doctor_check(package, home)], None
    if not env_module.server_present(package, home):
        return [_server_doctor_check(package, home)], None
    temp_dir = home.tmp_dir / f".facet-doctor-{run_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        return _doctor_call(package, home, run_id, depth, capability, temp_dir)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _browser_doctor_check(package: Package, home: Home) -> dict[str, object]:
    """The doctor entry for a declared browser whose extraction is not verified."""
    from facetmedia import env as env_module  # noqa: PLC0415

    assert package.browser is not None
    target = env_module.browser_dir(package.browser, home)
    return {
        "name": "browser",
        "ok": False,
        "detail": f"{target}: the extraction is not verified",
        "fix": f"facet pull {package.name}",
    }


def _server_doctor_check(package: Package, home: Home) -> dict[str, object]:
    """The doctor entry for a declared server whose checkout is not verified.

    The server is probed, never started: a doctor reports, the pull repairs.
    """
    from facetmedia import env as env_module  # noqa: PLC0415

    target = env_module.server_dir(package, home)
    return {
        "name": "server",
        "ok": False,
        "detail": f"{target}: the checkout is not verified",
        "fix": f"facet pull {package.name}",
    }


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
    from facetmedia.doctor import DOCTOR_TIMEOUTS, QUICK_TIMEOUT_S  # noqa: PLC0415

    timeout_s = DOCTOR_TIMEOUTS.get(depth, QUICK_TIMEOUT_S)
    try:
        invocation = runner.Invocation(
            package,
            home,
            home.env_dir(package.name, package.version),
            temp_dir,
            run_id,
            timeout_s,
            depth != "quick",
        )
        request = runner.make_request(
            "doctor", capability, {}, temp_dir, Path.cwd(), runner.model_paths(package, home)
        )
        request["depth"] = depth
        outcome = runner.call(invocation, "doctor", request)
        return _doctor_result(package, outcome.result, temp_dir)
    except FacetError as exc:
        return [_failed_doctor_check(package, exc)], _classify_doctor_error(package, exc, timeout_s)


def _failed_doctor_check(package: Package, error: FacetError) -> dict[str, object]:
    return {
        "name": "doctor",
        "ok": False,
        "detail": f"{package.name} doctor: {error.message}",
        "fix": "",
    }


def _classify_doctor_error(package: Package, error: FacetError, timeout_s: float) -> FacetError:
    """A timeout is ``machine_cannot``; a crash or bad protocol stays as it is."""
    timed_out = error.code == "runner_failed" and error.message.startswith(
        "the runner did not finish within"
    )
    if timed_out:
        return FacetError(
            "machine_cannot",
            f"{package.name} doctor: {error.message}",
            details={"package": package.name, "doctor_timeout": True, "timeout_s": timeout_s},
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
