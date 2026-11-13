"""``facet pull``'s plan: which packages this machine can run, and why.

A pull names facets (``facet pull image``) or packages, optionally with
``--package``.  Every package of the request is decided one by one from its
manifest and the machine: platform, GPU, VRAM, RAM and host application first,
then the tier of its ``tier_group``.  Nothing is downloaded until the plan is
confirmed, refused for disk, or printed with ``--plan``.  A request that
resolves to one package named on its own (not a facet, and without
``--plan``, ``--tier`` or ``--cpu``) keeps the one-package pull as it was.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

from facetmedia import config, env, lock, probes
from facetmedia.contract import FacetError, emit, ok
from facetmedia.home import Home
from facetmedia.progress import Progress, announce_failure
from facetmedia.registry import load_active_registry
from facetmedia.registry_types import FACETS, Package, Registry

if TYPE_CHECKING:
    import argparse

GB: Final[int] = 1024**3
MIB_PER_GB: Final[float] = 1024.0
FITS: Final[str] = "fits this machine"
ALREADY: Final[str] = "already installed"


@dataclass(frozen=True)
class Machine:
    """What the planner measured once: CPU-only, GPU MiB and total RAM."""

    cpu: bool
    gpu_mib: int | None
    ram_gb: float | None


@dataclass(frozen=True)
class PlanEntry:
    """One package's decision: include or skip, with a one-line reason."""

    package: Package
    install: bool
    reason: str
    noop: bool = False
    model_download_bytes: int = 0
    forced: bool = False

    @property
    def name(self) -> str:
        return self.package.name

    @property
    def version(self) -> str:
        return self.package.version

    @property
    def disk_gb(self) -> float:
        return self.package.requirements.disk_gb

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "disk_gb": self.disk_gb,
            "model_download_bytes": self.model_download_bytes,
            "install": self.install,
            "forced": self.forced,
            "reason": self.reason,
        }


def resolve(registry: Registry, names: list[str], packages: list[str]) -> list[Package]:
    """The packages a pull names: facets or packages, ``--package`` explicit."""
    if names and packages:
        raise FacetError(
            "invalid_input",
            "name facets or packages, or use --package, never both",
            hint="drop --package or drop the positional names",
        )
    if not names and not packages:
        facets = ", ".join(FACETS)
        raise FacetError(
            "invalid_input",
            "name a facet or a package to pull: "
            "facet pull <facet> or facet pull --package <name> "
            f"(facets: {facets})",
            details=_known(registry),
        )
    selected: list[Package] = []
    for name in names:
        selected.extend(_from_name(registry, name))
    for name in packages:
        package = registry.package(name)
        if package is None:
            raise FacetError(
                "invalid_input",
                f"unknown package: {name}",
                hint=_unknown_hint(registry),
                details=_known(registry),
            )
        selected.append(package)
    return _unique(selected)


def _known(registry: Registry) -> dict[str, object]:
    """Every facet and every package in the registry snapshot."""
    return {"facets": list(FACETS), "packages": sorted(registry.packages)}


def _unknown_hint(registry: Registry) -> str:
    """The valid names, one line each, for the text error of a typo."""
    facets = ", ".join(sorted(FACETS))
    packages = ", ".join(sorted(registry.packages))
    return f"facets: {facets}\npackages: {packages}"


def _from_name(registry: Registry, name: str) -> list[Package]:
    """A facet's packages when ``name`` is a facet (even if a package shares it)."""
    if name in FACETS:
        return [package for package in registry.packages.values() if package.facet == name]
    package = registry.package(name)
    if package is None:
        raise FacetError(
            "invalid_input",
            f"unknown facet or package: {name}",
            hint=_unknown_hint(registry),
            details=_known(registry),
        )
    return [package]


def _unique(packages: list[Package]) -> list[Package]:
    seen: set[str] = set()
    found: list[Package] = []
    for package in packages:
        if package.name in seen:
            continue
        seen.add(package.name)
        found.append(package)
    return found


def build_plan(packages: list[Package], home: Home, cpu: bool, tier: str | None) -> list[PlanEntry]:
    """Decide every package, then settle each ``tier_group`` to one member."""
    machine = Machine(cpu=cpu, gpu_mib=probes.gpu_total_mib(), ram_gb=probes.mem_total_gb())
    entries = [decide(package, home, machine) for package in packages]
    return apply_tiers(entries, tier or "")


def decide(package: Package, home: Home, machine: Machine) -> PlanEntry:
    """One package against the machine: skip reason first, then installed state."""
    entry = PlanEntry(
        package,
        install=True,
        reason=FITS,
        model_download_bytes=_missing_model_bytes(package, home),
    )
    reason = requirement_reason(package, machine, home)
    if reason:
        return replace(entry, install=False, reason=reason)
    if env.is_installed(package, home) and not env.package_stale(package, home):
        return replace(entry, reason=ALREADY, noop=True)
    return entry


def _missing_model_bytes(package: Package, home: Home) -> int:
    """The declared ``bytes`` of the models not yet present; never fetched to size.

    A backend-owned model is always present, so its declared bytes never count.
    """
    return sum(model.bytes for model in package.models if not env.model_present(model, home, False))


def requirement_reason(package: Package, machine: Machine, home: Home) -> str:
    """The first rule that skips ``package``, or an empty string when it fits."""
    requirements = package.requirements
    if sys.platform not in requirements.platforms:
        return f"does not support this platform ({sys.platform})"
    if machine.cpu and requirements.gpu:
        return "needs a GPU and --cpu was given"
    if requirements.gpu and machine.gpu_mib is None:
        return "needs an NVIDIA GPU"
    have_vram = (machine.gpu_mib or 0) / MIB_PER_GB
    if requirements.vram_gb > have_vram:
        return f"needs {requirements.vram_gb:g} GB VRAM, this machine has {have_vram:.2f} GB"
    if machine.ram_gb is not None and requirements.ram_gb > machine.ram_gb:
        return f"needs {requirements.ram_gb:g} GB RAM, this machine has {machine.ram_gb:.2f} GB"
    return _host_app_problem(package, home)


def _host_app_problem(package: Package, home: Home) -> str:
    """The exact ``facet config set`` fix when the declared app is unusable."""
    app = package.host_app
    if app is None:
        return ""
    problem = config.host_app_problem(app, home)
    return "" if problem is None else problem.fix


def apply_tiers(entries: list[PlanEntry], tier: str) -> list[PlanEntry]:
    """Exactly one member of each ``tier_group`` is included; the rest get a reason."""
    groups: dict[str, list[PlanEntry]] = {}
    for entry in entries:
        group = entry.package.tier_group
        if group:
            groups.setdefault(group, []).append(entry)
    by_name = {entry.name: entry for entry in entries}
    for members in groups.values():
        _settle_group(members, tier, by_name)
    return [by_name[entry.name] for entry in entries]


def _settle_group(members: list[PlanEntry], tier: str, by_name: dict[str, PlanEntry]) -> None:
    winner, forced, unmatched = _pick(members, tier)
    for member in members:
        if member.name == winner.name:
            by_name[member.name] = _winner_entry(member, tier, forced, unmatched)
        else:
            by_name[member.name] = replace(
                member, install=False, noop=False, reason=f"tier: chose {winner.name}"
            )


def _pick(members: list[PlanEntry], tier: str) -> tuple[PlanEntry, bool, bool]:
    """The group's member, whether ``--tier`` forced it, whether that tier is absent."""
    if tier:
        matching = [member for member in members if member.package.tier == tier]
        if matching:
            return matching[0], True, False
        return _automatic(members), False, True
    return _automatic(members), False, False


def _automatic(members: list[PlanEntry]) -> PlanEntry:
    """Highest-VRAM fitting member, else the lowest-VRAM member still skipped."""
    fitting = [member for member in members if member.install]
    if fitting:
        return max(fitting, key=lambda member: member.package.requirements.vram_gb)
    return min(members, key=lambda member: member.package.requirements.vram_gb)


def _winner_entry(entry: PlanEntry, tier: str, forced: bool, unmatched: bool) -> PlanEntry:
    if forced and not entry.install:
        return replace(
            entry,
            install=True,
            noop=False,
            forced=True,
            reason=f"warning: --tier {tier} forced it: {entry.reason}",
        )
    if not unmatched:
        return entry
    if entry.install:
        return replace(entry, reason=f"no {tier} tier; chose {entry.name}")
    return replace(entry, reason=f"{entry.reason}; no {tier} tier; chose {entry.name}")


def disk_refusal(entries: list[PlanEntry], home: Home) -> FacetError | None:
    """``machine_cannot`` when the installs need more disk than is free."""
    to_install = [entry for entry in entries if entry.install and not entry.noop]
    needed = sum(entry.disk_gb for entry in to_install)
    free = shutil.disk_usage(home.root).free / GB
    if needed <= free:
        return None
    return FacetError(
        "machine_cannot",
        f"not enough disk at {home.root}: need {needed:.2f} GB, {free:.2f} GB free",
        hint="facet clean",
        details={
            "reason": "disk",
            "needed_gb": round(needed, 3),
            "free_gb": round(free, 3),
            "packages": [{"name": entry.name, "disk_gb": entry.disk_gb} for entry in to_install],
        },
    )


def handle_pull(args: argparse.Namespace) -> int:
    """``facet pull``: one package as today, or a planned facet."""
    registry = load_active_registry()
    home = Home.from_env()
    names = list(getattr(args, "names", ()) or ())
    packages = list(getattr(args, "package", ()) or ())
    if not names and not packages:
        from facetmedia import wizard  # noqa: PLC0415

        if wizard.offered(args):
            chosen = wizard.choose(registry, home)
            if chosen is None:
                return 0
            names = chosen
    selected = resolve(registry, names, packages)
    legacy = legacy_package(args, selected, names)
    reporter = _reporter(args)
    wait = bool(getattr(args, "wait", True))
    timeout_s = float(getattr(args, "wait_timeout", 3600.0))
    with lock.hold("exclusive", "facet pull", wait, timeout_s):
        if legacy is not None:
            return _legacy_pull(args, home, legacy, reporter)
        return _planned_pull(args, home, selected, reporter)


def _reporter(args: argparse.Namespace) -> Progress | None:
    """The progress line the CLI configured, or ``None`` for a direct call."""
    reporter = getattr(args, "progress", None)
    return reporter if isinstance(reporter, Progress) else None


def legacy_package(
    args: argparse.Namespace, selected: list[Package], names: list[str]
) -> Package | None:
    """The single-package pull that keeps today's behaviour and data keys.

    One package chosen by name, positionally or with ``--package``, installs
    as it always has: no plan, no question, no new data keys. A facet name
    keeps the plan even when the facet holds one package, and ``--plan``,
    ``--tier`` or ``--cpu`` ask the plan for an answer. ``--yes`` changes
    nothing for a single package, so it stays on this path.
    """
    if len(selected) != 1:
        return None
    if any(name in FACETS for name in names):
        return None
    if any(bool(getattr(args, flag, False)) for flag in ("plan", "tier", "cpu")):
        return None
    return selected[0]


def _legacy_pull(
    args: argparse.Namespace, home: Home, package: Package, reporter: Progress | None = None
) -> int:
    try:
        report = env.pull(
            package,
            home,
            verify_models=args.verify_models,
            run_id=getattr(args, "run_id", ""),
            progress=reporter,
        )
    except FacetError:
        announce_failure(getattr(args, "run_id", ""))
        raise
    emit(ok(report), args.json)
    if not args.json:
        _announce_pull(report, bool(package.models))
    return 0


def _announce_pull(report: dict[str, object], has_models: bool) -> None:
    name = f"{report['package']} {report['version']}"
    if report["already_installed"] is True:
        print(f"{name} already installed at {report['env']}")
        return
    if not has_models:
        print(f"{name} installed at {report['env']} (no model downloads in {report['seconds']} s)")
        return
    print(
        f"{name} installed at {report['env']} "
        f"({report['downloaded_bytes']} bytes downloaded in {report['seconds']} s)"
    )


def _planned_pull(
    args: argparse.Namespace,
    home: Home,
    selected: list[Package],
    reporter: Progress | None = None,
) -> int:
    entries = build_plan(
        selected,
        home,
        cpu=bool(getattr(args, "cpu", False)),
        tier=getattr(args, "tier", None),
    )
    plan_data = [entry.as_dict() for entry in entries]
    if bool(getattr(args, "plan", False)):
        if not args.json:
            _print_plan(entries)
        emit(ok({"plan": plan_data}), args.json)
        return 0
    refusal = disk_refusal(entries, home)
    if refusal is not None:
        raise refusal
    if not _confirmed(args, entries, plan_data):
        return 0
    return _install_all(args, home, entries, reporter)


def _confirmed(
    args: argparse.Namespace, entries: list[PlanEntry], plan_data: list[dict[str, object]]
) -> bool:
    """Ask at a TTY; refuse with ``confirmation_required`` off a TTY or with --json."""
    to_install = [entry for entry in entries if entry.install and not entry.noop]
    if len(to_install) <= 1 or bool(getattr(args, "yes", False)):
        return True
    if args.json or not sys.stdin.isatty():
        raise FacetError(
            "confirmation_required",
            f"{len(to_install)} packages would be installed",
            hint="add --yes",
            details={"plan": plan_data},
        )
    _print_plan(entries)
    try:
        answer = input(f"install {len(to_install)} packages? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() != "y":
        print("nothing installed")
        return False
    return True


def _print_plan(entries: list[PlanEntry]) -> None:
    for entry in entries:
        state = _state_text(entry)
        print(
            f"{entry.name} {entry.version}  disk {entry.disk_gb:.1f} GB  "
            f"models {_models_text(entry.model_download_bytes)}  "
            f"{state}: {entry.reason}"
        )


def _state_text(entry: PlanEntry) -> str:
    """The row's state, with forced tiers spelled out so nobody misses them."""
    if entry.forced:
        return "install (forced)"
    return "install" if entry.install else "skip"


def _models_text(missing_bytes: int) -> str:
    """``0 GB`` for nothing missing, else the download size in GB."""
    return "0 GB" if missing_bytes == 0 else f"{missing_bytes / GB:.1f} GB"


def _install_all(
    args: argparse.Namespace,
    home: Home,
    entries: list[PlanEntry],
    reporter: Progress | None = None,
) -> int:
    installed: list[dict[str, object]] = []
    downloaded = 0
    skipped = _skipped(entries)
    for entry in entries:
        if not entry.install:
            continue
        if entry.noop:
            installed.append({"name": entry.name, "version": entry.version})
            continue
        try:
            report = env.pull(
                entry.package,
                home,
                verify_models=args.verify_models,
                run_id=getattr(args, "run_id", ""),
                progress=reporter,
            )
        except FacetError as error:
            announce_failure(getattr(args, "run_id", ""))
            if not args.json:
                _announce_summary(installed, skipped, downloaded)
            raise _with_partial(error, entries, installed, skipped, downloaded) from error
        downloaded += _bytes(report)
        installed.append({"name": entry.name, "version": entry.version})
    data = {
        "plan": [entry.as_dict() for entry in entries],
        "installed": installed,
        "skipped": skipped,
        "downloaded_bytes": downloaded,
    }
    emit(ok(data), args.json)
    if not args.json:
        _announce_summary(installed, skipped, downloaded)
    return 0


def _skipped(entries: list[PlanEntry]) -> list[dict[str, object]]:
    return [{"name": entry.name, "reason": entry.reason} for entry in entries if not entry.install]


def _bytes(report: dict[str, object]) -> int:
    downloaded = report.get("downloaded_bytes")
    return downloaded if isinstance(downloaded, int) else 0


def _with_partial(
    error: FacetError,
    entries: list[PlanEntry],
    installed: list[dict[str, object]],
    skipped: list[dict[str, object]],
    downloaded: int,
) -> FacetError:
    details = dict(error.details)
    details.setdefault("plan", [entry.as_dict() for entry in entries])
    details.setdefault("installed", installed)
    details.setdefault("skipped", skipped)
    details.setdefault("downloaded_bytes", downloaded)
    return FacetError(error.code, error.message, hint=error.hint, details=details)


def _announce_summary(
    installed: list[dict[str, object]],
    skipped: list[dict[str, object]],
    downloaded: int,
) -> None:
    for item in installed:
        print(f"installed {item['name']} {item['version']}")
    for item in skipped:
        print(f"skipped {item['name']}: {item['reason']}")
    print(f"{downloaded} bytes downloaded")
