"""Moving a run's assets out of its temp directory and recording each one.

Both paths a ``facet run`` may take end here: the file capability's assets and
an outward capability's receipt are placed the same way, refused by the same
rules, and recorded by the same builder.  Nothing in this module calls a
runner, so ``run`` and ``outward`` can share it without a cycle.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from facetmedia import __version__, prune, record
from facetmedia.contract import FacetError
from facetmedia.home import Home
from facetmedia.record import RecordSpec
from facetmedia.registry_types import Model, Package
from facetmedia.runner import RunAsset, VerifyResult

if TYPE_CHECKING:
    from facetmedia.run import RunJob

__all__ = [
    "keep_leftover",
    "license_section",
    "place",
    "provenance_models",
]

logger = logging.getLogger("facetmedia")


def place(
    job: RunJob,
    temp_dir: Path,
    assets: Sequence[RunAsset],
    tool: Mapping[str, str],
    verdicts: Mapping[str, VerifyResult],
) -> list[dict[str, object]]:
    """Move every asset onto ``<out>/<file>`` and write its record beside it."""
    refuse_overwrite(job, assets)
    records: list[dict[str, object]] = []
    for asset in assets:
        target = job.out_dir / asset.file
        os.replace(temp_dir / asset.file, target)
        built = record.build_record(target, record_spec(job, asset, tool, verdicts))
        record.write_record(target, built)
        records.append(built)
    return records


def refuse_overwrite(job: RunJob, assets: Sequence[RunAsset]) -> None:
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


def record_spec(
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
        models=provenance_models(job.package.models),
        params=asset.params,
        properties=asset.properties,
        seed=asset.seed,
        inputs=tuple(asset.inputs),
        verification=_verification(verdicts.get(asset.file)),
        license=license_section(job.package, job.accepted),
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


def keep_leftover(home: Home, temp_dir: Path) -> None:
    """Record the kept temp directory so ``facet clean`` can remove it later.

    Recording must never mask the run's own failure: a state file that cannot
    be written is logged and the original exception is re-raised by the caller.
    """
    try:
        prune.record_leftover(home, temp_dir)
    except Exception:
        logger.exception("cannot record the kept temporary directory %s", temp_dir)


def provenance_models(models: Sequence[Model]) -> tuple[dict[str, object], ...]:
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


def license_section(
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
