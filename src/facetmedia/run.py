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
import logging
import os
import shutil
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from facetmedia import __version__, doctor, env, licenses, prune, record, runner, schema
from facetmedia.contract import FacetError
from facetmedia.home import Home, log_file
from facetmedia.lock import LockMode, hold
from facetmedia.record import RecordSpec
from facetmedia.registry import Capability, Field, Package, load_active_registry
from facetmedia.registry_types import Model
from facetmedia.runner import Invocation, Outcome, RunAsset, VerifyResult

__all__ = ["execute"]

logger = logging.getLogger("facetmedia")


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
    piece_path: Path | None
    input_paths: tuple[Path, ...]
    input_entries: Mapping[str, Mapping[str, str]]
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
    gate_warnings = _require_machine(package, run_id)
    home = Home.from_env()
    ephemeral = _ephemeral(args, package, home)
    pulled, downloaded = _ensure_installed(args, package, run_id)
    try:
        accepted = tuple(licenses.require(package.models))
        inputs = _piece_inputs(capability, piece, piece_dir)
        out_dir = Path(args.out) if args.out else piece_dir
        paths = _input_paths(capability, inputs)
        piece_path = None if args.piece == "-" else Path(os.path.abspath(args.piece))
        input_entries = {
            str(Path(os.path.abspath(path))): record.input_entry(path, out_dir) for path in paths
        }
        job = RunJob(
            capability=capability,
            package=package,
            home=home,
            inputs=inputs,
            piece_dir=piece_dir,
            out_dir=out_dir,
            run_id=run_id,
            timeout_s=args.timeout,
            verify=bool(args.verify),
            piece_path=piece_path,
            input_paths=paths,
            input_entries=input_entries,
            accepted=accepted,
        )
        records, exit_code = _run_locked(args, job, ephemeral)
    except BaseException:
        # The install happened but the run never took the lock (a bad piece,
        # a lock that never came free): that environment is nobody's either.
        # When the lock was taken, ``_run_locked`` already removed it and this
        # second call is a no-op.
        _drop_ephemeral(home, package, ephemeral)
        raise
    data: dict[str, object] = {
        "capability": capability.id,
        "out_dir": str(job.out_dir),
        "pulled": pulled,
        "downloaded_bytes": downloaded,
        "assets": records,
    }
    if gate_warnings:
        data["warnings"] = gate_warnings
    return data, exit_code


def _ephemeral(args: argparse.Namespace, package: Package, home: Home) -> bool:
    """Whether ``--ephemeral`` applies: only to a package this run installs.

    An environment that was already there belongs to the person who pulled it,
    so ``--ephemeral`` changes nothing there and says so.
    """
    if not bool(getattr(args, "ephemeral", False)):
        return False
    if not env.is_installed(package, home):
        return True
    print(
        f"{package.name} {package.version} is already installed; --ephemeral changes nothing",
        file=sys.stderr,
    )
    return False


def _run_locked(
    args: argparse.Namespace, job: RunJob, ephemeral: bool
) -> tuple[list[dict[str, object]], int]:
    """Produce the run under the lock, removing an ephemeral install before it drops.

    The removal shares the lock with the run so no other job can start against
    an environment that is about to disappear.
    """
    exclusive = job.package.requirements.exclusive or ephemeral
    mode: LockMode = "exclusive" if exclusive else "shared"
    wait, wait_timeout = _lock_params(args)
    with hold(mode, "facet run", wait, wait_timeout):
        try:
            return _produce(job)
        finally:
            _drop_ephemeral(job.home, job.package, ephemeral)


def _drop_ephemeral(home: Home, package: Package, ephemeral: bool) -> None:
    """Remove the environment this run installed; a no-op for every other run."""
    if ephemeral:
        prune.rm_env(home, package.name, package.version)


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


def _require_machine(package: Package, run_id: str) -> list[str]:
    """The readiness gate plus the quick doctor, before anything is planned.

    A quick doctor that merely timed out does not block the run: each such
    warning goes to stderr and the run log, and the run carries on with it.
    """
    errors, warnings = doctor.requirement_errors(
        package, Home.from_env(), run_id, timeout_warns=True
    )
    for warning in warnings:
        print(warning, file=sys.stderr)
        logger.info("%s", warning)
    if errors:
        raise errors[0]
    return warnings


def _ensure_installed(args: argparse.Namespace, package: Package, run_id: str) -> tuple[bool, int]:
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
        report = env.pull(package, home, run_id=run_id)
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


def _input_paths(capability: Capability, values: Mapping[str, Any]) -> tuple[Path, ...]:
    """Every ``path`` input the validated values carry, in schema order."""
    return _field_paths(capability.inputs, values)


def _field_paths(fields: tuple[Field, ...], values: Mapping[str, Any]) -> tuple[Path, ...]:
    """The ``path`` values among ``values``, recursing into array items."""
    found: list[Path] = []
    for field in fields:
        value = values.get(field.name)
        if field.type == "path" and isinstance(value, str):
            found.append(Path(value))
        elif field.type == "array" and isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    found.extend(_field_paths(field.items, item))
    return tuple(found)


def _lock_params(args: argparse.Namespace) -> tuple[bool, float]:
    """``(wait, timeout_s)`` for a lock this verb takes (set by the CLI)."""
    return bool(getattr(args, "wait", True)), float(getattr(args, "wait_timeout", 3600.0))


# --- under the lock: run, verify, place, record.


def _produce(job: RunJob) -> tuple[list[dict[str, object]], int]:
    """One run under the lock: assets out of temp, one record each."""
    temp_dir = job.out_dir / f".facet-tmp-{job.run_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        records, failed = _attempt(job, temp_dir)
    except Exception:
        _keep_leftover(job.home, temp_dir)
        raise
    shutil.rmtree(temp_dir)
    return records, 3 if failed else 0


def _attempt(job: RunJob, temp_dir: Path) -> tuple[list[dict[str, object]], bool]:
    """Run and verify one piece in ``temp_dir``; the caller places the assets."""
    inv = Invocation(job.package, job.home, job.env_dir, temp_dir, job.run_id, job.timeout_s)
    models = runner.model_paths(job.package, job.home)
    request = runner.make_request(
        "run", job.capability.id, job.inputs, temp_dir, job.piece_dir, models
    )
    outcome = runner.call(inv, "run", request)
    tool, assets = _parse_run(outcome, inv)
    verdicts = _verify(inv, job, models, assets) if job.verify else {}
    records = _place(job, temp_dir, assets, tool, verdicts)
    failed = any(
        isinstance(entry["verification"], Mapping)
        and entry["verification"].get("status") == "failed"
        for entry in records
    )
    return records, failed


def _keep_leftover(home: Home, temp_dir: Path) -> None:
    """Record the kept temp directory so ``facet gc`` can remove it later.

    Recording must never mask the run's own failure: a state file that cannot
    be written is logged and the original exception is re-raised by the caller.
    """
    try:
        prune.record_leftover(home, temp_dir)
    except Exception:
        logger.exception("cannot record the kept temporary directory %s", temp_dir)


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
    _refuse_overwrite(job, assets)
    records: list[dict[str, object]] = []
    for asset in assets:
        target = job.out_dir / asset.file
        os.replace(temp_dir / asset.file, target)
        built = record.build_record(target, _record_spec(job, asset, tool, verdicts))
        record.write_record(target, built)
        records.append(built)
    return records


def _refuse_overwrite(job: RunJob, assets: Sequence[RunAsset]) -> None:
    """Refuse an asset that would land on the run's own input (FR2).

    The check runs before anything is moved, so a refusal never loses the
    produced files: they stay in the temp directory the caller keeps.
    """
    for asset in assets:
        target = job.out_dir / asset.file
        for protected in _protected_paths(job):
            if _same_file(target, protected):
                raise FacetError(
                    "invalid_input",
                    f"asset {asset.file} would overwrite input {protected}",
                    hint="give the piece a name that differs from its inputs, or pass --out",
                    details={"asset": asset.file, "input": str(protected)},
                )


def _protected_paths(job: RunJob) -> tuple[Path, ...]:
    """The files this run must never overwrite: its inputs and the piece."""
    if job.piece_path is None:
        return job.input_paths
    return (*job.input_paths, job.piece_path)


def _same_file(target: Path, protected: Path) -> bool:
    """Whether two paths name one file: same resolved path, or same file."""
    if target.resolve() == protected.resolve():
        return True
    try:
        return os.path.samefile(target, protected)
    except OSError:
        return False


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
        input_entries=job.input_entries,
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
