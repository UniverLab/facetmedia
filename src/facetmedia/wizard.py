"""The interactive ``facet pull`` wizard for people at a terminal.

``facet pull`` with no facet and no ``--package`` starts here when both
standard input and output are TTYs (and ``--json`` is not given): the
banner, the detected machine, and the facets to choose from.  The wizard
builds the same machine plan ``facet pull <facet>`` would and installs it
through the same confirmation and progress output.  Agents keep the flag
form, which never enters this module.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING, Final

from facetmedia import config, plan, probes
from facetmedia.registry_types import FACETS

if TYPE_CHECKING:
    import argparse

    from facetmedia.home import Home
    from facetmedia.plan import PlanEntry
    from facetmedia.registry_types import Registry

PROMPT: Final[str] = "[numbers / a / Enter / q] "
RETRY: Final[str] = "type facet numbers, a, or q"
NOTHING_INSTALLED: Final[str] = "nothing installed"
NOTHING_SELECTED: Final[str] = "nothing selected"


@dataclass
class Choice:
    """One facet row: its machine plan and whether it is currently picked."""

    name: str
    entries: list[PlanEntry]
    selected: bool


def offered(args: argparse.Namespace) -> bool:
    """True when ``facet pull`` with no target should open the wizard."""
    if bool(getattr(args, "json", False)):
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()


def choose(registry: Registry, home: Home) -> list[str] | None:
    """Run the wizard; the chosen facet names, or ``None`` to install nothing."""
    print(_banner_text(), end="")
    _machine(registry, home)
    choices: list[Choice] = []
    for name in FACETS:
        entries = plan.build_plan(plan._from_name(registry, name), home, cpu=False, tier=None)
        choices.append(Choice(name, entries, selected=any(entry.install for entry in entries)))
    _show(choices)
    return _ask(choices)


def _banner_text() -> str:
    """The core README's ASCII banner, stored once as a package resource."""
    return (resources.files("facetmedia") / "banner.txt").read_text(encoding="utf-8")


def _machine(registry: Registry, home: Home) -> None:
    """The detected machine in one block: GPU, RAM, free disk, host apps."""
    print(_gpu_line())
    print(_ram_line())
    print(_disk_line(home))
    for line in _app_lines(registry, home):
        print(line)


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


def _show(choices: list[Choice]) -> None:
    """Every facet as a numbered line with its mark, count and packages."""
    for index, choice in enumerate(choices, start=1):
        mark = "x" if choice.selected else " "
        detail = ", ".join(_package_note(entry) for entry in choice.entries)
        suffix = f": {detail}" if detail else ""
        print(f"{index}) {choice.name} [{mark}] {len(choice.entries)} packages{suffix}")


def _package_note(entry: PlanEntry) -> str:
    """One package with its plan verdict: fits, installed, or its skip reason."""
    if entry.noop or entry.reason == plan.ALREADY:
        return f"{entry.name} (installed)"
    if entry.install:
        return f"{entry.name} (fits)"
    return f"{entry.name} ({entry.reason})"


def _ask(choices: list[Choice]) -> list[str] | None:
    """Toggle facets until Enter accepts, ``a`` picks all, ``q`` quits."""
    while True:
        try:
            answer = input(PROMPT)
        except EOFError:
            print(NOTHING_INSTALLED)
            return None
        word = answer.strip().lower()
        if word == "":
            return _accept(choices)
        if word == "a":
            for choice in choices:
                choice.selected = True
            _show(choices)
            continue
        if word == "q":
            print(NOTHING_INSTALLED)
            return None
        if not _toggle(choices, word):
            print(RETRY)
            continue
        _show(choices)


def _accept(choices: list[Choice]) -> list[str] | None:
    """The picked facet names, or ``None`` with ``nothing selected`` when empty."""
    names = [choice.name for choice in choices if choice.selected]
    if not names:
        print(NOTHING_SELECTED)
        return None
    return names


def _toggle(choices: list[Choice], word: str) -> bool:
    """Flip the numbered facets; False when ``word`` is not facet numbers."""
    indices: list[int] = []
    for token in word.split():
        if not token.isdigit():
            return False
        index = int(token)
        if index < 1 or index > len(choices):
            return False
        indices.append(index)
    if not indices:
        return False
    for index in indices:
        choices[index - 1].selected = not choices[index - 1].selected
    return True
