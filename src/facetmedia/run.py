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
import time
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import questionary
from rich.markup import escape

from facetmedia import (
    doctor,
    env,
    licenses,
    placement,
    post,
    prune,
    record,
    runner,
    schema,
    secrets,
    theme,
)
from facetmedia.contract import FacetError
from facetmedia.home import Home, log_file
from facetmedia.lock import LockMode, hold
from facetmedia.progress import Progress, one_line
from facetmedia.pull_summary import PullSummary
from facetmedia.registry import Capability, Field, Package, load_active_registry
from facetmedia.registry_types import Model, capability_models
from facetmedia.runner import Invocation, Outcome, RunAsset, VerifyResult

__all__ = ["execute", "verification_error"]

logger = logging.getLogger("facetmedia")

#: The question a real outward run asks at a terminal; anything but yes cancels.
SEND_PROMPT = "send this? [default: no]"


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
    receipt_name: str = ""
    dry_run: bool = False
    yes: bool = False

    @property
    def env_dir(self) -> Path:
        return self.home.env_dir(self.package.name, self.package.version)


def execute(args: argparse.Namespace, run_id: str) -> tuple[dict[str, object], int]:
    """Run one piece; returns the ``--json`` data and the process exit code."""
    piece, piece_dir = _read_piece(args.piece)
    capability, package = _resolve_capability(args.capability)
    _require_outward_flags(capability, args)
    out_dir = Path(args.out) if args.out else piece_dir
    piece_path = None if args.piece == "-" else Path(os.path.abspath(args.piece))
    receipt_name = _receipt_name(capability, piece_path)
    # The receipt-exists rule names only ``out_dir`` and the name, so it runs
    # first of all: a piece already sent is refused before the doctor, the
    # pull or anything else that would run a package.
    if capability.outward and not bool(getattr(args, "dry_run", False)):
        post.refuse_existing(out_dir, receipt_name)
    gate_warnings = _require_machine(package, run_id)
    home = Home.from_env()
    # A capability that needs a required secret fails here, before any pull and
    # before the runner starts; ``facet auth`` is never gated, it makes them.
    secrets.require_ready(home, package)
    ephemeral = _ephemeral(args, package, home)
    pulled, downloaded = _ensure_installed(args, package, run_id)
    try:
        inputs = _piece_inputs(capability, piece, piece_dir)
        accepted = tuple(licenses.require(_required_models(package, capability, inputs)))
        paths = _input_paths(capability, inputs)
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
            receipt_name=receipt_name,
            dry_run=bool(getattr(args, "dry_run", False)),
            yes=bool(getattr(args, "yes", False)),
        )
        records, exit_code, data = _run_locked(args, job, ephemeral)
    except BaseException:
        # The install happened but the run never took the lock (a bad piece,
        # a lock that never came free): that environment is nobody's either.
        # When the lock was taken, ``_run_locked`` already removed it and this
        # second call is a no-op.
        _drop_ephemeral(home, package, ephemeral)
        raise
    data.update(
        {
            "capability": capability.id,
            "out_dir": str(job.out_dir),
            "pulled": pulled,
            "downloaded_bytes": downloaded,
        }
    )
    if records is not None:
        data["assets"] = records
    if gate_warnings:
        data["warnings"] = gate_warnings
    return data, exit_code


def _require_outward_flags(capability: Capability, args: argparse.Namespace) -> None:
    """The outward-only flags: a dry run, yes and no-verify belong to post alone."""
    dry_run = bool(getattr(args, "dry_run", False))
    yes = bool(getattr(args, "yes", False))
    for flag, given in (("--dry-run", dry_run), ("--yes", yes)):
        if given and not capability.outward:
            raise FacetError(
                "invalid_input",
                f"{capability.id}: {flag} is only for an outward capability",
                hint=f"run it without {flag}",
                details={"capability": capability.id},
            )
    if capability.outward and not bool(args.verify):
        raise FacetError(
            "invalid_input",
            f"{capability.id}: --no-verify is not allowed for an outward capability",
            hint="an outward run always reads the post back",
            details={"capability": capability.id},
        )


def _receipt_name(capability: Capability, piece_path: Path | None) -> str:
    """The receipt's file name for this run, empty for a file capability."""
    if not capability.outward:
        return ""
    return post.receipt_name(piece_path, capability.id)


def verification_error(data: Mapping[str, object]) -> FacetError | None:
    """The error a run's data implies when an asset failed verification.

    The caller keeps the data and reports this error, so the envelope and the
    exit code agree. A record stores the bare file name relative to the run's
    ``out_dir``; it is joined back so ``facet check`` resolves it.
    """
    assets = data.get("assets")
    entries = (
        [entry for entry in assets if isinstance(entry, dict)] if isinstance(assets, list) else []
    )
    failed: list[str] = []
    for entry in entries:
        ref = _failed_ref(entry, data)
        if ref is not None:
            failed.append(ref)
    if not failed:
        return None
    return FacetError(
        "verification_failed",
        f"{len(failed)} of {len(entries)} assets failed verification: {', '.join(failed)}",
        hint=f"facet check {failed[0]}",
        details={"assets": failed},
    )


def _failed_ref(entry: Mapping[str, object], data: Mapping[str, object]) -> str | None:
    """The failed asset's path, as ``facet check`` resolves it, or ``None``."""
    verification = entry.get("verification")
    if not isinstance(verification, Mapping) or verification.get("status") != "failed":
        return None
    path = entry.get("path")
    if not isinstance(path, str):
        return None
    if Path(path).is_absolute():
        return path
    out_dir = data.get("out_dir")
    if isinstance(out_dir, str) and out_dir:
        return str(Path(out_dir) / path)
    return path


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
) -> tuple[list[dict[str, object]] | None, int, dict[str, object]]:
    """Produce the run under the lock, removing an ephemeral install before it drops.

    The removal shares the lock with the run so no other job can start against
    an environment that is about to disappear.
    """
    exclusive = job.package.requirements.exclusive or ephemeral
    mode: LockMode = "exclusive" if exclusive else "shared"
    wait, wait_timeout = _lock_params(args)
    with hold(mode, "facet run", wait, wait_timeout):
        try:
            if job.capability.outward:
                return _produce_outward(args, job)
            records, exit_code = _produce(job)
            return records, exit_code, {}
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
    Licence acceptance is not a machine-readiness question: the run gates it
    per piece after the inputs are read (see ``_required_models``), so the
    package-wide licence error is left out here.
    """
    errors, warnings = doctor.requirement_errors(
        package, Home.from_env(), run_id, timeout_warns=True
    )
    errors = [error for error in errors if error.code != "license_not_accepted"]
    for warning in warnings:
        _warn(warning)
        logger.info("%s", warning)
    if errors:
        raise errors[0]
    return warnings


def _warn(text: str) -> None:
    """One gate warning on stderr: marked at a terminal, bare in a pipe or log."""
    if theme.styled(sys.stderr):
        theme.console(sys.stderr).print(f"[warn]! {escape(text)}[/]", soft_wrap=True)
        return
    print(text, file=sys.stderr)


def _ensure_installed(args: argparse.Namespace, package: Package, run_id: str) -> tuple[bool, int]:
    """Pull the package when needed; ``--no-pull`` refuses an absent or stale env.

    The install draws ``facet pull``'s lines: the package's header, its live
    phases, then the one line with its name, version, size and time.
    """
    home = Home.from_env()
    if env.is_installed(package, home) and not env.package_stale(package, home):
        return False, 0
    if not args.pull:
        raise _not_installed_error(package, home)
    wait, wait_timeout = _lock_params(args)
    reporter = _reporter(args)
    summary = PullSummary(reporter)
    with hold("exclusive", "facet run pull", wait, wait_timeout):
        began = time.monotonic()
        summary.begin(package.name, package.version)
        try:
            report = env.pull(package, home, run_id=run_id, progress=reporter)
        except FacetError as error:
            summary.failed(package.name, package.version, time.monotonic() - began, one_line(error))
            raise
        downloaded = report.get("downloaded_bytes", 0)
        size = downloaded if isinstance(downloaded, int) else 0
        summary.collapse(package.name, package.version, size, time.monotonic() - began)
    return True, size


def _reporter(args: argparse.Namespace) -> Progress | None:
    """The progress line the CLI configured, or ``None`` for a direct call."""
    reporter = getattr(args, "progress", None)
    return reporter if isinstance(reporter, Progress) else None


def _not_installed_error(package: Package, home: Home) -> FacetError:
    """The ``--no-pull`` refusal: absent, or changed since it was installed."""
    if env.package_stale(package, home):
        return FacetError(
            "not_installed",
            f"{package.name} {package.version} changed in the registry since it was installed",
            hint=f"run facet pull {package.name}, or drop --no-pull",
            details={"package": package.name, "version": package.version},
        )
    return FacetError(
        "not_installed",
        f"{package.name} {package.version} is not installed",
        hint="run facet pull, or drop --no-pull",
        details={"package": package.name, "version": package.version},
    )


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


def _required_models(
    package: Package, capability: Capability, inputs: Mapping[str, Any]
) -> list[Model]:
    """The gated models this piece requires acceptance for.

    A capability with a ``model`` input gates the model the piece selects by
    exact id equality; a capability declaring ``models`` gates exactly those,
    so an empty declaration requires nothing; otherwise the package-wide
    gate applies.  A ``model`` value naming no manifest model requires
    nothing.
    """
    if capability.input("model") is not None:
        value = inputs.get("model")
        model = next((item for item in package.models if item.id == value), None)
        return [model] if model is not None else []
    return capability_models(package, capability)


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
        placement.keep_leftover(job.home, temp_dir)
        raise
    shutil.rmtree(temp_dir)
    return records, 3 if failed else 0


# --- outward capabilities: dry run, confirmation, send, receipt, read-back.


def _produce_outward(
    args: argparse.Namespace, job: RunJob
) -> tuple[list[dict[str, object]] | None, int, dict[str, object]]:
    """A dry run, or a confirmed send that leaves a receipt and verifies it.

    A dry run sends nothing and writes nothing into the output folder: the
    preview lives in the home's temporary directory and is returned as data.
    A declined confirmation exits 0 with nothing sent.

    The receipt-exists rule is repeated here, under the lock, before the first
    runner call: the pull may have taken long enough for another run to write
    the receipt the check before it saw absent.  A disagreement before
    anything left is not a leftover worth keeping; a failure after it is.
    """
    if not job.dry_run:
        post.refuse_existing(job.out_dir, job.receipt_name)
    temp_dir = job.home.tmp_dir / f".facet-post-{job.run_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        records, exit_code, data = _outward_step(args, job, temp_dir)
    except FacetError as failure:
        if failure.code == "confirmation_required":
            shutil.rmtree(temp_dir, ignore_errors=True)
        else:
            placement.keep_leftover(job.home, temp_dir)
        raise
    except Exception:
        placement.keep_leftover(job.home, temp_dir)
        raise
    shutil.rmtree(temp_dir, ignore_errors=True)
    return records, exit_code, data


def _outward_step(
    args: argparse.Namespace, job: RunJob, temp_dir: Path
) -> tuple[list[dict[str, object]] | None, int, dict[str, object]]:
    """The dry run and the confirmation, then the one send."""
    preview = _dry_preview(job, temp_dir)
    if job.dry_run:
        return None, 0, {"dry_run": True, "preview": preview}
    if not _confirmed(args, job, preview):
        return None, 0, {"dry_run": False, "sent": False}
    records = _send_and_place(job, temp_dir, preview)
    return records, 0, {"dry_run": False, "sent": True}


def _dry_preview(job: RunJob, temp_dir: Path) -> dict[str, object]:
    """What the run would send, straight from the runner's ``dry-run`` verb.

    ``out_dir`` is the home's temporary directory, never the output folder:
    a dry run writes nothing into the output folder, and the receipt-exists
    rule's answer rides along as a warning instead of a failure.
    """
    inv = Invocation(job.package, job.home, job.env_dir, temp_dir, job.run_id, job.timeout_s)
    models = runner.model_paths(job.package, job.home)
    request = runner.with_sources(
        runner.make_request(
            post.DRY_RUN_VERB,
            job.capability.id,
            job.inputs,
            temp_dir,
            job.piece_dir,
            models,
        ),
        post.sources(job.input_paths, job.input_entries),
    )
    outcome = runner.call(inv, post.DRY_RUN_VERB, request)
    try:
        preview = post.parse_dry_run(outcome.result, temp_dir)
    except FacetError as exc:
        raise _enriched(exc, outcome, inv) from None
    if post.exists(job.out_dir, job.receipt_name):
        warnings = preview.get("warnings")
        existing = list(warnings) if isinstance(warnings, list) else []
        preview["warnings"] = [
            *existing,
            f"a receipt already exists at {job.out_dir / job.receipt_name}; a real run "
            "would refuse it until that receipt is removed",
        ]
    return preview


def _confirmed(args: argparse.Namespace, job: RunJob, preview: Mapping[str, object]) -> bool:
    """Ask a person at a terminal; refuse with ``confirmation_required`` elsewhere.

    ``--yes`` is the only thing that skips the question: no other flag or
    setting implies it.  Anything but yes cancels with nothing sent, as a
    declined ``facet pull`` does.
    """
    if job.yes:
        return True
    if bool(getattr(args, "json", False)) or not sys.stdin.isatty():
        raise FacetError(
            "confirmation_required",
            f"sending {len(_posts(preview))} post(s) needs an explicit yes",
            hint="add --yes once the preview is what you meant to send",
            details={"preview": dict(preview)},
        )
    post.announce(preview)
    if theme.styled():
        agreed = bool(questionary.confirm(SEND_PROMPT, default=False).unsafe_ask())
    else:
        try:
            answer = input("send this? [y/N] ")
        except EOFError:
            answer = ""
        agreed = answer.strip().lower() == "y"
    return agreed


def _posts(preview: Mapping[str, object]) -> list[Mapping[str, object]]:
    entries = preview.get("posts")
    if not isinstance(entries, list):
        return []
    return [entry if isinstance(entry, Mapping) else {} for entry in entries]


def _send_and_place(
    job: RunJob, temp_dir: Path, preview: Mapping[str, object]
) -> list[dict[str, object]]:
    """Send once, keep the receipt, read the post back and judge it honestly.

    The request is never retried.  Once the runner reports that a request
    left, the receipt is kept even when a later step fails, then sealed to
    ``unknown``: a post that may exist is never reported as more than that.
    A ``run`` verb that exits 0 but answers outside the protocol is one of
    those later steps: the receipt facet writes then says only ``unknown``.
    """
    inv = Invocation(job.package, job.home, job.env_dir, temp_dir, job.run_id, job.timeout_s)
    models = runner.model_paths(job.package, job.home)
    carried = post.sources(job.input_paths, job.input_entries)
    request = runner.with_sources(
        runner.make_request(
            "run",
            job.capability.id,
            job.inputs,
            temp_dir,
            job.piece_dir,
            models,
        ),
        carried,
    )
    outcome = runner.call(inv, "run", request)
    try:
        facts = post.parse_post(outcome.result, temp_dir)
    except FacetError as exc:
        enriched = _enriched(exc, outcome, inv)
        raise _salvaged(job, temp_dir, preview, outcome, carried, enriched) from None
    sent_at = post.now()
    verdict, failure = _read_back(inv, job, models, facts, carried)
    receipt = _receipt(facts, carried, sent_at, pass_read_back=verdict.passed)
    records = _place_receipt(job, temp_dir, outcome, receipt, verdict)
    if failure is not None:
        raise _may_exist(facts, _receipt_file(job), failure)
    if not verdict.passed:
        raise _state_mismatch(facts, _receipt_file(job), verdict)
    return records


def _salvaged(
    job: RunJob,
    temp_dir: Path,
    preview: Mapping[str, object],
    outcome: Outcome,
    carried: Sequence[Mapping[str, object]],
    failure: FacetError,
) -> FacetError:
    """Keep a receipt for a send whose result facet could not read.

    The ``run`` verb exited 0, so a request may have left even though its
    answer broke the protocol; the receipt says only ``unknown``, and the
    receipt-exists rule stops a second accidental send.
    """
    receipt = post.salvage(
        outcome.result,
        carried,
        network=job.package.name,
        account=_preview_account(preview),
        sent_at=post.now(),
    )
    _place_receipt(job, temp_dir, outcome, receipt, _failed_verdict(job, receipt))
    return _may_exist(receipt, _receipt_file(job), failure)


def _preview_account(preview: Mapping[str, object]) -> str:
    """The account the dry run named for the first post, or ``unknown``."""
    for entry in _posts(preview):
        account = entry.get("account")
        if isinstance(account, str) and account:
            return account
    return "unknown"


def _place_receipt(
    job: RunJob,
    temp_dir: Path,
    outcome: Outcome,
    receipt: Mapping[str, object],
    verdict: VerifyResult,
) -> list[dict[str, object]]:
    """Write the receipt beside the runner's files, then move and record it."""
    asset = post.run_asset(job.receipt_name, receipt, job.input_paths)
    post.write_receipt(temp_dir / job.receipt_name, receipt)
    _receipt_file(job).parent.mkdir(parents=True, exist_ok=True)
    return placement.place(
        job, temp_dir, [asset], _tool_of(outcome.result, job), {asset.file: verdict}
    )


def _tool_of(result: Mapping[str, object], job: RunJob) -> dict[str, str]:
    """The tool the run result names, or the package's own name and version."""
    tool = result.get("tool")
    if isinstance(tool, Mapping) and isinstance(tool.get("name"), str):
        version = tool.get("version")
        return {"name": str(tool["name"]), "version": version if isinstance(version, str) else ""}
    return {"name": job.package.tool_name, "version": job.package.version}


def _receipt_file(job: RunJob) -> Path:
    """The receipt's target in the output folder."""
    return job.out_dir / job.receipt_name


def _read_back(
    inv: Invocation,
    job: RunJob,
    models: Mapping[str, Path],
    facts: Mapping[str, object],
    carried: Sequence[Mapping[str, object]],
) -> tuple[VerifyResult, FacetError | None]:
    """The post re-read from the network; a failed call is a failed verdict.

    The receipt is never written from a read-back that did not happen: a
    FacetError here seals the receipt and is reported as "the post may exist".
    """
    request = runner.with_post(
        runner.with_sources(
            runner.make_request(
                "verify",
                job.capability.id,
                job.inputs,
                inv.temp_dir,
                job.piece_dir,
                models,
            ),
            carried,
        ),
        post.post_section(facts),
    )
    try:
        outcome = runner.call(inv, "verify", request)
        measured = post.parse_read_back(outcome.result, inv.temp_dir)
    except FacetError as exc:
        return _failed_verdict(job, facts), exc
    verdict = post.verdict(
        {**facts, "path": job.receipt_name, "sources": carried},
        measured,
        job.capability.thresholds,
    )
    return verdict, None


def _failed_verdict(job: RunJob, facts: Mapping[str, object]) -> VerifyResult:
    return VerifyResult(
        file=job.receipt_name,
        passed=False,
        metrics={"state": facts.get("state")},
        thresholds=dict(job.capability.thresholds),
        verifier="post-read-back",
    )


def _receipt(
    facts: Mapping[str, object],
    carried: Sequence[Mapping[str, object]],
    sent_at: str,
    *,
    pass_read_back: bool,
) -> dict[str, object]:
    """The receipt, sealed to ``unknown`` when the read-back did not pass."""
    verified_at = post.now() if pass_read_back else None
    receipt = post.build_receipt(facts, carried, sent_at=sent_at, verified_at=verified_at)
    return receipt if pass_read_back else post.seal(receipt)


def _may_exist(facts: Mapping[str, object], receipt: Path, failure: FacetError) -> FacetError:
    """The post may exist: keep the receipt, name it and where to check."""
    urls = [entry["url"] for entry in _post_refs(facts)]
    hint = f"check the post at {urls[0]}" if urls else "check the account before sending again"
    return FacetError(
        failure.code,
        f"{failure.message}; the post may exist",
        hint=hint,
        details={
            **failure.details,
            "receipt": str(receipt),
            "network": facts.get("network"),
            "account": facts.get("account"),
            "urls": urls,
        },
    )


def _state_mismatch(
    facts: Mapping[str, object], receipt: Path, verdict: VerifyResult
) -> FacetError:
    """The read-back contradicts what was sent: the receipt says ``unknown``."""
    urls = [entry["url"] for entry in _post_refs(facts)]
    hint = f"check the post at {urls[0]}" if urls else f"facet check {receipt}"
    return FacetError(
        "verification_failed",
        "the post went out, but the read-back does not match what was sent: "
        f"the network holds it as {verdict.metrics.get('state')} with "
        f"{verdict.metrics.get('media')} media",
        hint=hint,
        details={
            "receipt": str(receipt),
            "state": verdict.metrics.get("state"),
            "media": verdict.metrics.get("media"),
            "urls": urls,
        },
    )


def _post_refs(facts: Mapping[str, object]) -> list[Mapping[str, object]]:
    entries = facts.get("posts")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, Mapping) and entry.get("url")]


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
    records = placement.place(job, temp_dir, assets, tool, verdicts)
    failed = any(
        isinstance(entry["verification"], Mapping)
        and entry["verification"].get("status") == "failed"
        for entry in records
    )
    return records, failed


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
