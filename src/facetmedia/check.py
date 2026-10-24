"""``facet check``: does this asset still pass its record?

``facet check <asset-or-record>`` re-verifies an asset facet made earlier:
it reads the asset's record, fails with ``verification_failed`` when the
bytes changed, then calls the runner's ``verify`` verb with the record's
params and reports the fresh metrics next to the recorded ones. The record
file is never modified. The capability is read from the record itself, so
the old ``<capability-id>`` argument is gone: readiness questions are
``facet doctor``'s (see ``facetmedia.doctor``).

``pull`` and ``run`` gate with the doctor's readiness functions plus the
package's quick doctor, through ``doctor.requirement_errors``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from facetmedia import record, runner
from facetmedia.contract import FacetError
from facetmedia.home import Home
from facetmedia.registry import Capability, Package, load_active_registry

__all__ = [
    "VERIFY_TIMEOUT_S",
    "reverify",
    "record_capability",
    "resolve",
]

#: How long ``facet check`` waits for the runner's ``verify`` verb.
VERIFY_TIMEOUT_S: float = 60.0


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


def record_capability(asset: str) -> str:
    """The capability id the asset's record names."""
    stored = record.read_record(asset)
    provenance = stored.get("provenance")
    if not isinstance(provenance, dict):
        raise FacetError("invalid_input", f"{asset}: the record has no provenance")
    capability = provenance.get("capability")
    if not isinstance(capability, str) or not capability:
        raise FacetError("invalid_input", f"{asset}: the record names no capability")
    return capability


def reverify(asset: str, home: Home, run_id: str) -> tuple[dict[str, object], int]:
    """Re-verify an existing asset against its record; the record is untouched."""
    capability_id = record_capability(asset)
    stored = record.read_record(asset)
    provenance = stored["provenance"]
    assert isinstance(provenance, dict)
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
        invocation = runner.Invocation(package, home, env_dir, temp_dir, run_id, VERIFY_TIMEOUT_S)
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
