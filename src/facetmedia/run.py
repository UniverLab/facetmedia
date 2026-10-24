"""``facet run``: a piece file in, verified assets and their records out.

One command turns a declarative piece into assets: resolve the capability,
pull the package if needed, check the licence, validate the piece, take the
lock, run the package's runner in its own environment, verify every asset,
then move the assets out of the temporary directory and write each record.

Pieces are data: nothing here evaluates code from a piece. The runner is the
only code that runs, and it runs behind the protocol in ``facetmedia.runner``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from facetmedia import __version__, env, licenses, record, runner, schema
from facetmedia.contract import FacetError
from facetmedia.home import Home, log_file
from facetmedia.lock import LockMode, hold
from facetmedia.record import RecordSpec
from facetmedia.registry import Capability, Package, load_active_registry
from facetmedia.registry_types import Model
from facetmedia.runner import Invocation, Outcome, RunAsset, VerifyResult

__all__ = ["execute"]


@dataclass(frozen=True)
class RunJob:
    """One run's resolved inputs; what every later step needs to know."""

    capability: Capability
    package: Package
    home: Home
    inputs: dict[str, Any]
    piece_dir: Path
    out_dir: Path
    run_id: str
    timeout_s: float | None
    verify: bool
    accepted: tuple[Mapping[str, object], ...] = ()

    @property
    def env_dir(self) -> Path:
        return self.home.env_dir(self.package.name, self.package.version)

    @property
    def models_provenance(self) -> tuple[dict[str, object], ...]:
        return _provenance_models(self.package.models)

    @property
    def license_section(self) -> dict[str, object]:
        return _license_section(self.package, self.accepted)


def execute(args: argparse.Namespace, run_id: str) -> tuple[dict[str, object], int]:
    """Run one piece; returns the ``--json`` data and the process exit code."""
    piece, piece_dir = _read_piece(args.piece)
    capability, package = _resolve_capability(args.capability)
    pulled, downloaded = _ensure_installed(args, package)
    accepted = tuple(licenses.require(package.models))
    job = RunJob(
        capability=capability,
        package=package,
        home=Home.from_env(),
        inputs=_piece_inputs(capability, piece, piece_dir),
        piece_dir=piece_dir,
        out_dir=Path(args.out) if args.out else piece_dir,
        run_id=run_id,
        timeout_s=args.timeout,
        verify=bool(args.verify),
        accepted=accepted,
    )
    mode: LockMode = "exclusive" if package.requirements.exclusive else "shared"
    wait, wait_timeout = _lock_params(args)
    with hold(mode, "facet run", wait, wait_timeout):
        records, exit_code = _produce(job)
    data: dict[str, object] = {
        "capability": capability.id,
        "out_dir": str(job.out_dir),
        "pulled": pulled,
        "downloaded_bytes": downloaded,
        "assets": records,
    }
    return data, exit_code


# --- before the lock: the piece, the capability, the package, the licence.


def _read_piece(argument: str) -> tuple[dict[str, Any], Path]:
    """The piece as a dict plus its directory; ``-`` is JSON from stdin."""
    if argument == "-":
        try:
            loaded: object = json.load(sys.stdin)
        except ValueError as exc:
            raise FacetError("invalid_input", f"the piece on stdin is not JSON: {exc}") from exc
        return _as_table(loaded), Path.cwd()
    path = Path(argument)
    try:
        with path.open("rb") as handle:
            loaded = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise FacetError("invalid_input", f"{path}: no such piece file") from exc
    except tomllib.TOMLDecodeError as exc:
        raise FacetError("invalid_input", f"{path}: not valid TOML: {exc}") from exc
    except OSError as exc:
        raise FacetError("invalid_input", f"{path}: cannot read the piece: {exc}") from exc
    return _as_table(loaded), path.parent


def _as_table(loaded: object) -> dict[str, Any]:
    if not isinstance(loaded, dict):
        raise FacetError("invalid_input", "a piece must be a table")
    return loaded


def _resolve_capability(capability_id: str) -> tuple[Capability, Package]:
    """The capability and its package; unknown id lists the known ids."""
    active = load_active_registry()
    capability = active.capability(capability_id)
    package = active.package(capability.package)
    if package is None:
        raise FacetError(
            "internal",
            f"{capability_id}: its package {capability.package} is not in the registry",
        )
    package.require_compatible()
    return capability, package


def _ensure_installed(args: argparse.Namespace, package: Package) -> tuple[bool, int]:
    """Pull the package when needed; ``--no-pull`` refuses an absent env."""
    home = Home.from_env()
    if env.is_installed(package, home):
        return False, 0
    if not args.pull:
        raise FacetError(
            "not_installed",
            f"{package.name} {package.version} is not installed",
            hint="run facet pull, or drop --no-pull",
            details={"package": package.name, "version": package.version},
        )
    wait, wait_timeout = _lock_params(args)
    with hold("exclusive", "facet run pull", wait, wait_timeout):
        report = env.pull(package, home)
    downloaded = report.get("downloaded_bytes", 0)
    return True, downloaded if isinstance(downloaded, int) else 0


def _piece_inputs(
    capability: Capability, piece: Mapping[str, Any], piece_dir: Path
) -> dict[str, Any]:
    raw = piece.get("inputs", {})
    if not isinstance(raw, dict):
        raise FacetError(
            "invalid_input",
            "the piece's inputs must be a table",
            details={"capability": capability.id},
        )
    return schema.validate(capability, raw, piece_dir)


def _lock_params(args: argparse.Namespace) -> tuple[bool, float]:
    """``(wait, timeout_s)`` for a lock this verb takes (set by the CLI)."""
    return bool(getattr(args, "wait", True)), float(getattr(args, "wait_timeout", 3600.0))


# --- under the lock: run, verify, place, record.


def _produce(job: RunJob) -> tuple[list[dict[str, object]], int]:
    """One run under the lock: assets out of temp, one record each."""
    temp_dir = job.out_dir / f".facet-tmp-{job.run_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    inv = Invocation(job.package, job.home, job.env_dir, temp_dir, job.run_id, job.timeout_s)
    models = runner.model_paths(job.package, job.home)
    request = runner.make_request(
        "run", job.capability.id, job.inputs, temp_dir, job.piece_dir, models
    )
    outcome = runner.call(inv, "run", request)
    tool, assets = _parse_run(outcome, inv)
    verdicts = _verify(inv, job, models, assets) if job.verify else {}
    records = _place(job, temp_dir, assets, tool, verdicts)
    shutil.rmtree(temp_dir)
    failed = any(
        isinstance(entry["verification"], Mapping)
        and entry["verification"].get("status") == "failed"
        for entry in records
    )
    return records, 3 if failed else 0


def _parse_run(outcome: Outcome, inv: Invocation) -> tuple[dict[str, str], list[RunAsset]]:
    try:
        return runner.parse_run_result(outcome.result, inv.temp_dir)
    except FacetError as exc:
        raise _enriched(exc, outcome, inv) from None


def _verify(
    inv: Invocation, job: RunJob, models: Mapping[str, Path], assets: Sequence[RunAsset]
) -> dict[str, VerifyResult]:
    paths = [inv.temp_dir / asset.file for asset in assets]
    request = runner.make_request(
        "verify", job.capability.id, job.inputs, inv.temp_dir, job.piece_dir, models, paths
    )
    outcome = runner.call(inv, "verify", request)
    try:
        return runner.parse_verify_result(outcome.result, assets, inv.temp_dir)
    except FacetError as exc:
        raise _enriched(exc, outcome, inv) from None


def _enriched(exc: FacetError, outcome: Outcome, inv: Invocation) -> FacetError:
    """A parse failure keeps the runner's output, log path and temp dir."""
    return FacetError(
        exc.code,
        exc.message,
        exc.hint,
        {
            **exc.details,
            "output": outcome.output,
            "log": str(log_file(inv.run_id)),
            "temp": str(inv.temp_dir),
        },
    )


def _place(
    job: RunJob,
    temp_dir: Path,
    assets: Sequence[RunAsset],
    tool: Mapping[str, str],
    verdicts: Mapping[str, VerifyResult],
) -> list[dict[str, object]]:
    """Move every asset onto ``<out>/<file>`` and write its record beside it."""
    records: list[dict[str, object]] = []
    for asset in assets:
        target = job.out_dir / asset.file
        os.replace(temp_dir / asset.file, target)
        built = record.build_record(target, _record_spec(job, asset, tool, verdicts))
        record.write_record(target, built)
        records.append(built)
    return records


def _record_spec(
    job: RunJob,
    asset: RunAsset,
    tool: Mapping[str, str],
    verdicts: Mapping[str, VerifyResult],
) -> RecordSpec:
    return RecordSpec(
        name=asset.name,
        kind=asset.kind,
        mime=asset.mime,
        facet=__version__,
        capability=job.capability.id,
        package={
            "name": job.package.name,
            "version": job.package.version,
            "lock_sha256": job.package.lock_sha256,
        },
        tool=tool,
        models=job.models_provenance,
        params=asset.params,
        properties=asset.properties,
        seed=asset.seed,
        inputs=tuple(asset.inputs),
        verification=_verification(verdicts.get(asset.file)),
        license=job.license_section,
    )


def _verification(verdict: VerifyResult | None) -> dict[str, object] | None:
    """The record's verification section; ``None`` means ``--no-verify``."""
    if verdict is None:
        return None
    return {
        "status": "passed" if verdict.passed else "failed",
        "metrics": dict(verdict.metrics),
        "thresholds": dict(verdict.thresholds),
        "verifier": verdict.verifier,
    }


# --- the record's provenance and licence sections.


def _provenance_models(models: Sequence[Model]) -> tuple[dict[str, object], ...]:
    entries: list[dict[str, object]] = []
    for model in models:
        entry: dict[str, object] = {"id": model.id, "bytes": model.bytes}
        if model.sha256:
            entry["sha256"] = model.sha256
        if model.hf_repo:
            entry["hf_repo"] = model.hf_repo
            entry["revision"] = model.revision
        entries.append(entry)
    return tuple(entries)


def _license_section(
    package: Package, accepted: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    return {
        "tools": [{"subject": lic.subject, "spdx": lic.spdx} for lic in package.licenses],
        "models": [_model_license(model) for model in package.models],
        "accepted": [dict(entry) for entry in accepted],
    }


def _model_license(model: Model) -> dict[str, object]:
    return {
        "id": model.id,
        "spdx": model.spdx,
        "commercial_use": model.commercial_use,
        "requires_acceptance": model.requires_acceptance,
        "terms_url": model.terms_url,
    }
