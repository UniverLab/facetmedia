"""Interactive wizard for ``facet secret set`` with no arguments.

When invoked at a terminal with no package or secret name, facet walks the
person through choosing an installed package, then a credential, then its
value, repeating until every required credential is set or the person quits.
The value is always read through the existing secret-set path so the manifest
gate and the value channel stay in one place.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final, TypeVar, cast

import questionary

from facetmedia import env, secrets, theme
from facetmedia.contract import FacetError
from facetmedia.home import Home
from facetmedia.registry import load_active_registry

if TYPE_CHECKING:
    from facetmedia.registry_types import Package

_DONE: Final[object] = object()
_NOTHING_SELECTED: Final[str] = "nothing selected"
_NO_SECRETS: Final[str] = "no secrets declared"
_T = TypeVar("_T")


def _is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def run(args: argparse.Namespace) -> int:
    """Run the bare ``facet secret set`` wizard."""
    if bool(getattr(args, "json", False)):
        raise FacetError("invalid_input", "the following arguments are required: <package> <name>")
    if not _is_tty():
        raise FacetError(
            "confirmation_required",
            "storing credentials needs a terminal",
            hint="facet secret set <package> <name>",
        )

    home = Home.from_env()
    active = load_active_registry()
    packages = [pkg for name, pkg in sorted(active.packages.items()) if env.is_installed(pkg, home)]
    if not packages:
        print(_NOTHING_SELECTED)
        return 0

    package = _pick_package(packages)
    if package is None:
        print(_NOTHING_SELECTED)
        return 0
    if not package.secrets:
        print(_NO_SECRETS)
        return 0

    while True:
        missing = {secret.name for secret in secrets.missing_required(home, package)}
        choice = _pick_secret(package, home, missing)
        if choice is None:
            print(_NOTHING_SELECTED)
            return 0
        args.package = package.name
        args.name = choice
        from facetmedia import cli as cli_module  # noqa: PLC0415

        cli_module._handle_secret_set(args)
        if not secrets.missing_required(home, package):
            return 0


def _pick_package(packages: list[Package]) -> Package | None:
    if theme.styled():
        return _select_styled_package(packages)
    return _select_plain_package(packages)


def _select_styled_package(packages: list[Package]) -> Package | None:
    choices = [
        questionary.Choice(
            title=[
                ("class:accent", package.name),
                ("", f" {package.version}"),
            ],
            value=package,
        )
        for package in packages
    ]
    choices.append(questionary.Choice(title="done", value=_DONE))
    try:
        picked = questionary.select(
            "Choose a package — arrows move, enter picks [default: done]:",
            choices=choices,
            style=theme.menu_style(),
        ).unsafe_ask()
    except (EOFError, KeyboardInterrupt):
        return None
    if picked is _DONE or picked is None:
        return None
    return cast("Package", picked)


def _select_plain_package(packages: list[Package]) -> Package | None:
    names = [package.name for package in packages]
    for index, package in enumerate(packages, 1):
        print(f"{index}. {package.name} {package.version}")
    try:
        answer = input(f"Choose a package [1-{len(names)}, default: done]: ").strip()
    except EOFError:
        return None
    return _match(names, packages, answer)


def _pick_secret(package: Package, home: Home, missing: set[str]) -> str | None:
    rows = secrets.rows(home, [package])
    ordered = sorted(rows, key=lambda row: (str(row["name"]) not in missing, str(row["name"])))
    if theme.styled():
        return _select_styled_secret(ordered)
    return _select_plain_secret(ordered)


def _secret_line(row: dict[str, object]) -> str:
    state = "set" if row.get("set") is True else "not set"
    required = " required" if row.get("required") is True else ""
    return f"{row['name']}{required} {state}: {row['description']}"


def _select_styled_secret(rows: list[dict[str, object]]) -> str | None:
    choices = [questionary.Choice(title=_secret_line(row), value=str(row["name"])) for row in rows]
    choices.append(questionary.Choice(title="done", value=_DONE))
    try:
        picked = questionary.select(
            "Choose a credential — arrows move, enter picks [default: done]:",
            choices=choices,
            style=theme.menu_style(),
        ).unsafe_ask()
    except (EOFError, KeyboardInterrupt):
        return None
    if picked is _DONE or picked is None:
        return None
    return str(picked)


def _select_plain_secret(rows: list[dict[str, object]]) -> str | None:
    names = [str(row["name"]) for row in rows]
    for index, row in enumerate(rows, 1):
        print(f"{index}. {_secret_line(row)}")
    try:
        answer = input(f"Choose a credential [1-{len(names)}, default: done]: ").strip()
    except EOFError:
        return None
    return _match(names, names, answer)


def _match(labels: list[str], values: Sequence[_T], answer: str) -> _T | None:
    if not answer:
        return None
    if answer in labels:
        return values[labels.index(answer)]
    try:
        index = int(answer)
    except ValueError:
        return None
    if 1 <= index <= len(values):
        return values[index - 1]
    return None
