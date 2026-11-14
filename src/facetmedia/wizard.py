"""The interactive ``facet pull`` wizard for people at a terminal.

``facet pull`` with no facet and no ``--package`` starts here when both
standard input and output are TTYs (and ``--json`` is not given): the
banner, the detected machine, and the facets to choose from.  The wizard
builds the same machine plan ``facet pull <facet>`` would and confirms it
through the same confirmation logic.  Agents keep the flag form, which
never enters this module.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import questionary
from rich.panel import Panel

from facetmedia import banner, config, plan, probes, theme
from facetmedia.registry_types import FACETS

if TYPE_CHECKING:
    import argparse

    from facetmedia.home import Home
    from facetmedia.plan import PlanEntry
    from facetmedia.registry_types import Registry

NOTHING_INSTALLED: Final[str] = "nothing installed"
NOTHING_SELECTED: Final[str] = "nothing selected"


@dataclass
class Choice:
    """One facet row: its machine plan and whether it starts picked."""

    name: str
    entries: list[PlanEntry]
    checked: bool


def offered(args: argparse.Namespace) -> bool:
    """True when ``facet pull`` with no target should open the wizard."""
    if bool(getattr(args, "json", False)):
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()


def choose(registry: Registry, home: Home, args: argparse.Namespace) -> list[str] | None:
    """Run the wizard; the chosen facet names, or ``None`` to install nothing."""
    console = theme.console()
    console.print("Step 1 of 4: this machine")
    banner.print_text()
    console.print(Panel(_machine_text(registry, home), title="This machine"))
    console.print("Step 2 of 4: choose facets — arrows move, space toggles, enter confirms")
    names = _pick(registry, home, args)
    if not names:
        print(NOTHING_SELECTED)
        return None
    if bool(getattr(args, "plan", False)):
        return names
    if not _confirm(registry, home, args, names):
        return None
    console.print("Step 4 of 4: install")
    return names


def _machine_text(registry: Registry, home: Home) -> str:
    """The detected machine in one block: GPU, RAM, free disk, host apps."""
    lines = [_gpu_line(), _ram_line(), _disk_line(home)]
    lines.extend(_app_lines(registry, home))
    return "\n".join(lines)


def _gpu_line() -> str:
    """``GPU: NAME (12.0 GB)`` from the first ``gpu_info()`` line, else none."""
    name, mib = _gpu_name_mib(probes.gpu_info())
    if name is None or mib is None:
        return "GPU: no NVIDIA GPU"
    return f"GPU: {name} ({mib / 1024:.1f} GB)"


def _gpu_name_mib(info: str | None) -> tuple[str | None, int | None]:
    """The first GPU's name and total MiB, or ``(None, None)`` when unreadable."""
    if not info:
        return None, None
    fields = info.splitlines()[0].split(",")
    if len(fields) <= probes.TOTAL_COLUMN:
        return None, None
    try:
        return fields[0].strip(), int(fields[1].split()[0])
    except (IndexError, ValueError):
        return None, None


def _ram_line() -> str:
    """Total RAM in GB, or ``unknown`` when ``/proc/meminfo`` is unreadable."""
    ram_gb = probes.mem_total_gb()
    if ram_gb is None:
        return "RAM: unknown"
    return f"RAM: {ram_gb:.2f} GB"


def _disk_line(home: Home) -> str:
    """Free disk under the facet home, the pull's disk check reads."""
    free_gb = shutil.disk_usage(home.root).free / plan.GB
    return f"free disk at {home.root}: {free_gb:.2f} GB free"


def _app_lines(registry: Registry, home: Home) -> list[str]:
    """One line per configured or found host app: ready, or its problem."""
    apps = config.known_apps(registry)
    lines: list[str] = []
    for key in sorted(apps):
        problem = config.host_app_problem(apps[key], home)
        if problem is None:
            lines.append(f"host app {key}: ready")
        else:
            lines.append(f"host app {key}: {problem.message}")
    return lines


def _pick(registry: Registry, home: Home, args: argparse.Namespace) -> list[str]:
    """The facets the person checked; arrows move, space toggles."""
    cpu = bool(getattr(args, "cpu", False))
    tier = getattr(args, "tier", None)
    prompts = [
        questionary.Choice(title=_facet_line(choice), value=choice.name, checked=choice.checked)
        for choice in _choices(registry, home, cpu, tier)
    ]
    return list(questionary.checkbox("Facets to install:", choices=prompts).unsafe_ask())


def _choices(registry: Registry, home: Home, cpu: bool, tier: str | None) -> list[Choice]:
    """Every facet with its machine plan; installable facets start checked."""
    choices: list[Choice] = []
    for name in FACETS:
        entries = plan.build_plan(plan._from_name(registry, name), home, cpu=cpu, tier=tier)
        checked = any(entry.install and not entry.noop for entry in entries)
        choices.append(Choice(name, entries, checked))
    return choices


def _facet_line(choice: Choice) -> str:
    """One facet with its packages, their disk, and their total disk."""
    notes = [_package_note(entry) for entry in choice.entries]
    total = sum(entry.disk_gb for entry in choice.entries if entry.install and not entry.noop)
    detail = ", ".join(notes) if notes else "no packages"
    return f"{choice.name}: {detail} [total {total:.1f} GB]"


def _package_note(entry: PlanEntry) -> str:
    """One package with its plan verdict: fits, installed, or its skip reason."""
    if entry.noop or entry.reason == plan.ALREADY:
        return f"{entry.name} (installed)"
    if entry.install:
        return f"{entry.name} ({entry.disk_gb:.1f} GB)"
    return f"{entry.name} ({entry.reason})"


def _confirm(registry: Registry, home: Home, args: argparse.Namespace, names: list[str]) -> bool:
    """Step 3: the plan table, then the same confirmation the flags use."""
    theme.console().print("Step 3 of 4: review the plan")
    selected = plan.resolve(registry, names, [])
    entries = plan.build_plan(
        selected,
        home,
        cpu=bool(getattr(args, "cpu", False)),
        tier=getattr(args, "tier", None),
    )
    refusal = plan.disk_refusal(entries, home)
    if refusal is not None:
        raise refusal
    plan_data = [entry.as_dict() for entry in entries]
    return plan._confirmed(args, entries, plan_data)
