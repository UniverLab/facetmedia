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

import json
import shutil
from collections.abc import Mapping
from pathlib import Path

from facetmedia import env as env_module
from facetmedia import post, record, runner
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

    env_dir = home.env_dir(package.name, package.version)
    _require_installed(package, home)
    params_value = provenance.get("params")
    if not isinstance(params_value, dict):
        raise FacetError("invalid_input", f"{asset}: the record has no params")
    params: dict[str, object] = dict(params_value)
    asset_path = _asset_path(asset)
    params = _relative_to_asset(params, asset_path.parent)
    verdict = _post_verdict(
        package, home, capability_id, stored, asset_path, run_id
    ) or _verify_asset(package, home, env_dir, run_id, capability_id, params, asset_path)
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


def _post_verdict(
    package: Package,
    home: Home,
    capability_id: str,
    stored: Mapping[str, object],
    asset_path: Path,
    run_id: str,
) -> runner.VerifyResult | None:
    """A ``post`` asset's verdict: the network read back and judged honestly.

    ``None`` for any other kind, so a file capability keeps today's path.  A
    ``post`` is never compared to its own bytes alone: the network holds the
    truth, so facet reads it back and passes only when the receipt matches.
    """
    if stored.get("kind") != post.RECEIPT_KIND:
        return None
    receipt = _receipt_of(asset_path)
    _require_installed(package, home)
    return _read_back_post(package, home, run_id, capability_id, receipt, asset_path)


def _receipt_of(asset_path: Path) -> dict[str, object]:
    try:
        loaded: object = json.loads(asset_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FacetError("invalid_input", f"{asset_path}: the receipt is not JSON") from exc
    if not isinstance(loaded, dict):
        raise FacetError("invalid_input", f"{asset_path}: the receipt is not a JSON object")
    return {str(key): value for key, value in loaded.items()}


def _read_back_post(
    package: Package,
    home: Home,
    run_id: str,
    capability_id: str,
    receipt: Mapping[str, object],
    asset_path: Path,
) -> runner.VerifyResult:
    """The runner's read-back of one post, judged against its receipt."""
    temp_dir = home.tmp_dir / f".facet-check-post-{run_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        capability = next((item for item in package.capabilities if item.id == capability_id), None)
        invocation = runner.Invocation(
            package,
            home,
            home.env_dir(package.name, package.version),
            temp_dir,
            run_id,
            VERIFY_TIMEOUT_S,
            capability is not None and capability.verify_uses_server,
        )
        models = runner.model_paths(package, home)
        request = runner.with_post(
            runner.with_sources(
                runner.make_request(
                    "verify",
                    capability_id,
                    {},
                    temp_dir,
                    asset_path.parent,
                    models,
                ),
                _carried(receipt),
            ),
            post.post_section(receipt),
        )
        outcome = runner.call(invocation, "verify", request)
        measured = post.parse_read_back(outcome.result, temp_dir)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    thresholds = capability.thresholds if capability is not None else {}
    return post.verdict(receipt, measured, thresholds)


def _carried(receipt: Mapping[str, object]) -> list[dict[str, object]]:
    """The receipt's source assets, which the read-back request repeats."""
    sources = receipt.get("sources")
    if not isinstance(sources, list):
        return []
    return [dict(item) for item in sources if isinstance(item, Mapping)]


def _require_installed(package: Package, home: Home) -> None:
    """Refuse a package that is absent or changed since it was installed.

    ``check`` never verifies with stale code: a package whose files changed
    in the registry reads exactly like one that was never installed, except
    the message names the change.
    """
    if env_module.is_installed(package, home) and not env_module.package_stale(package, home):
        return
    if env_module.package_stale(package, home):
        message = f"{package.name} {package.version} changed in the registry since it was installed"
    else:
        message = f"{package.name} {package.version} is not installed"
    raise FacetError(
        "not_installed",
        message,
        hint=f"run facet pull {package.name}",
        details={"package": package.name, "version": package.version},
    )


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


def _relative_to_asset(params: dict[str, object], asset_dir: Path) -> dict[str, object]:
    """Resolve record params that name files next to the asset, at any depth.

    A record stores an absolute input path relative to its own directory
    (``record._relative_value``), so the stored form only means something when
    read from there. The runner inherits ``facet``'s working directory, so the
    paths are made absolute again here: every relative string naming an
    existing file next to the asset becomes that file's absolute path, while
    absolute strings, text that is not a path and missing files stay as they
    are. ``check`` works from any directory that way.
    """
    return {str(key): _resolve_relative(value, asset_dir) for key, value in params.items()}


def _resolve_relative(value: object, asset_dir: Path) -> object:
    """The walk behind ``_relative_to_asset``, mirroring the record's walk."""
    if isinstance(value, Mapping):
        return {str(key): _resolve_relative(item, asset_dir) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_resolve_relative(item, asset_dir) for item in value]
    if isinstance(value, str) and not Path(value).is_absolute():
        candidate = asset_dir / value
        if record.safe_is_file(candidate):
            return str(candidate.resolve())
    return value


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
        capability = next((item for item in package.capabilities if item.id == capability_id), None)
        invocation = runner.Invocation(
            package,
            home,
            env_dir,
            temp_dir,
            run_id,
            VERIFY_TIMEOUT_S,
            capability is not None and capability.verify_uses_server,
        )
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
