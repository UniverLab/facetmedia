"""``facet auth login`` as a guided wizard: typed secrets, browser flow, check.

Bare ``facet auth`` offers every installed package that declares a secret
and is not fully signed in, one at a time with nothing preselected;
with a package it guides that one.  For each package the wizard asks for
every typed secret that is not already stored (one the ``auth`` verb does
not produce), telling the person what it is and where to get it, runs the
package's ``auth`` verb when it has one, and checks the result with the
full doctor.  The run ends with one summary line per package:
``signed in``, ``partly set``, ``failed`` or ``skipped``.

A value is never printed, logged or passed on: typed answers arrive with
echo off and travel only through the existing secret store.
"""

from __future__ import annotations

import getpass
from typing import Final

from rich.markup import escape

from facetmedia import auth, doctor, lock, secrets, theme
from facetmedia.contract import FacetError
from facetmedia.home import Home
from facetmedia.registry_types import Package, Secret

__all__ = [
    "FAILED",
    "PARTLY_SET",
    "SIGNED_IN",
    "SKIPPED",
    "run_package",
    "run_packages",
]

#: The full doctor passed: the package is signed in.
SIGNED_IN: Final[str] = "signed in"
#: The doctor's secrets check fails: a required secret is still unset.
PARTLY_SET: Final[str] = "partly set"
#: The secrets check passes but the doctor does not: the package fails.
FAILED: Final[str] = "failed"
#: The person skipped or quit the package: no check ran.
SKIPPED: Final[str] = "skipped"


def run_packages(
    names: list[str],
    *,
    run_id: str,
    wait: bool = True,
    timeout_s: float = 3600.0,
) -> dict[str, str]:
    """Guide each named package in order; returns one label per package.

    Cancelling (Ctrl-C, EOF, or the auth retry's ``quit``) stops cleanly:
    ``nothing changed`` when nothing was stored in this run, otherwise the
    names stored.  Either way the process exit code stays 0.
    """
    home = Home.from_env()
    stored: list[str] = []
    labels: dict[str, str] = {}
    cancelled = False
    try:
        for name in names:
            package = auth.resolve_wizard(name)
            label = run_package(
                package,
                home=home,
                run_id=run_id,
                wait=wait,
                timeout_s=timeout_s,
                stored=stored,
            )
            if label is None:
                cancelled = True
                break
            labels[package.name] = label
    except (KeyboardInterrupt, EOFError):
        cancelled = True
    if cancelled:
        if stored:
            print(f"stored {', '.join(stored)}")
        else:
            print(auth.NOTHING_CHANGED)
    return labels


def run_package(
    package: Package,
    *,
    home: Home,
    run_id: str,
    wait: bool = True,
    timeout_s: float = 3600.0,
    stored: list[str] | None = None,
) -> str | None:
    """Guide one package; ``None`` when the person quit the whole wizard."""
    kept: list[str] = stored if stored is not None else []
    _typed_phase(package, home, wait, timeout_s, kept)
    continued = _auth_phase(package, run_id, wait, timeout_s, kept)
    if continued is None:
        return None
    label = _check_phase(package, run_id, wait, timeout_s) if continued else SKIPPED
    style = "ok" if label == SIGNED_IN else "bold"
    theme.console().print(f"[{style}]{escape(package.name)}: {escape(label)}[/]", soft_wrap=True)
    return label


def _track(stored: list[str], name: str) -> None:
    """Remember a stored secret name once, for the cancelling report."""
    if name not in stored:
        stored.append(name)


def _typed_phase(
    package: Package, home: Home, wait: bool, timeout_s: float, stored: list[str]
) -> None:
    """Ask for every unstored secret the ``auth`` verb does not produce, in order."""
    typed = [secret for secret in package.secrets if secret.produced_by != "auth"]
    known = {str(row["name"]): row.get("set") is True for row in secrets.rows(home, [package])}
    for secret in typed:
        if known.get(secret.name) is True:
            continue
        _announce_secret(package, secret)
        value = _read_value(package, secret)
        if not value:
            continue
        with lock.hold("exclusive", "facet auth", wait, timeout_s):
            secrets.set_value(home, package, secret.name, value)
        known[secret.name] = True
        _track(stored, secret.name)
        print(f"{package.name} {secret.name} set")


def _announce_secret(package: Package, secret: Secret) -> None:
    """What the secret is and where the person gets it; never a value."""
    console = theme.console()
    console.print(
        f"[accent]{escape(package.name)} {escape(secret.name)}:[/] {escape(secret.description)}",
        soft_wrap=True,
    )
    if secret.obtain:
        console.print(f"  [muted]where to get it:[/] {escape(secret.obtain)}", soft_wrap=True)


def _read_value(package: Package, secret: Secret) -> str:
    """The typed value with echo off, stripped; empty is a skip only when optional."""
    while True:
        prompt = theme.render(f"[accent]{escape(package.name)} {escape(secret.name)}:[/] ")
        value = getpass.getpass(prompt).strip()
        if value:
            return value
        if not secret.required:
            print(f"{package.name} {secret.name} skipped")
            return ""
        print(f"{package.name} {secret.name} is required; enter a value.")


def _auth_phase(
    package: Package, run_id: str, wait: bool, timeout_s: float, stored: list[str]
) -> bool | None:
    """Run the package's ``auth`` verb: True to check, False on skip, None on quit."""
    if not secrets.has_auth_verb(package):
        return True
    while True:
        try:
            with lock.hold("exclusive", "facet auth", wait, timeout_s):
                data = auth.handle(package.name, run_id)
        except FacetError as exc:
            print(f"{package.name}: sign-in failed: {exc.message.splitlines()[0]}")
            choice = _ask_auth_choice(package)
            if choice == "retry":
                continue
            if choice == "skip":
                return False
            return None
        names = data.get("secrets")
        if not isinstance(names, list):
            print(f"{package.name}: stored ")
            return True
        for name in names:
            if isinstance(name, str):
                _track(stored, name)
        print(f"{package.name}: stored {', '.join(sorted(names))}")
        return True


def _ask_auth_choice(package: Package) -> str:
    """The failure loop: ``retry`` (the default), ``skip`` or ``quit``."""
    while True:
        prompt = theme.render(
            f"[accent]{escape(package.name)}:[/] retry, skip or quit? [retry/skip/quit]: "
        )
        answer = input(prompt)
        choice = answer.strip().lower() or "retry"
        if choice in ("retry", "skip", "quit"):
            return choice
        print("answer retry, skip or quit.")


def _check_phase(package: Package, run_id: str, wait: bool, timeout_s: float) -> str:
    """Map the full doctor to one label; a failure prints detail and fix."""
    data, code = doctor.execute(package.name, "full", run_id, wait=wait, timeout_s=timeout_s)
    if code == 0:
        return SIGNED_IN
    section = data.get("packages", {})
    readiness: dict[str, object] = {}
    doctor_rows: list[object] = []
    if isinstance(section, dict):
        found = section.get(package.name)
        if isinstance(found, dict):
            maybe = found.get("readiness")
            readiness = maybe if isinstance(maybe, dict) else {}
            maybe_rows = found.get("doctor")
            doctor_rows = maybe_rows if isinstance(maybe_rows, list) else []
    secrets_check = readiness.get("secrets")
    if not (isinstance(secrets_check, dict) and secrets_check.get("ok") is True):
        _print_start_over(package)
        return PARTLY_SET
    detail, fix = _first_failing(readiness, doctor_rows)
    _print_start_over(package)
    if detail:
        print(detail)
    if fix:
        print(fix)
    return FAILED


def _print_start_over(package: Package) -> None:
    """How to redo the sign-in from scratch after the check did not pass."""
    theme.console().print(
        f"[muted]to start over: facet auth logout {escape(package.name)}[/]", soft_wrap=True
    )


def _first_failing(readiness: dict[str, object], doctor_rows: list[object]) -> tuple[str, str]:
    """The detail and fix of the first failing check, readiness first."""
    for name in doctor.CHECK_ORDER:
        entry = readiness.get(name)
        if isinstance(entry, dict) and entry.get("ok") is not True:
            return str(entry.get("detail", "")), str(entry.get("fix", ""))
    for entry in doctor_rows:
        if isinstance(entry, dict) and entry.get("ok") is not True:
            return str(entry.get("detail", "")), str(entry.get("fix", ""))
    return "", ""
