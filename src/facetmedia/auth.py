"""``facet auth``: run a package's interactive sign-in and keep what it returns.

OAuth flows need a browser: the runner prints a URL, opens it, listens on a
loopback port and finally writes the tokens it obtained.  facet runs the
package's registered ``auth`` verb that way and stores the result under the
package's declared secret names — a name the manifest did not declare is
refused, so a flow can never smuggle an undeclared credential into the store.

The verb's only channel back is the result file, so the secrets travel in a
``secrets`` map there.  The temporary directory holding that file is removed
in every case and is never recorded as a kept leftover: a value that reached
disk outside the encrypted vault would survive the mask.  The run log is
scrubbed after storing for the same reason: the output mask predates the
tokens the flow mints, so anything the verb printed reached the log unmasked.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

import questionary

from facetmedia import banner, lock, renewals, runner, secrets, theme
from facetmedia import env as env_module
from facetmedia import installed as installed_module
from facetmedia.contract import FacetError
from facetmedia.home import Home
from facetmedia.registry import Package, load_active_registry
from facetmedia.runner import Invocation
from facetmedia.secrets import has_auth_verb, set_value, vault_file

if TYPE_CHECKING:
    from facetmedia.registry_types import Registry

__all__ = [
    "ALL_SIGNED_IN",
    "AUTH_VERB",
    "LOGIN_VERB",
    "LOGOUT_VERB",
    "NOTHING_CHANGED",
    "NOTHING_OFFERED",
    "NOTHING_SELECTED",
    "NOTHING_STORED",
    "NOTHING_WITH_SECRETS",
    "RESULT_KEY",
    "handle",
    "logout",
    "pick",
    "resolve_wizard",
]

#: The verb a package registers to produce secrets through a browser flow.
AUTH_VERB: Final[str] = "auth"
#: The result-file key the returned secrets travel in.
RESULT_KEY: Final[str] = "secrets"
#: ``facet auth login``: today's sign-in flow, with or without a package.
LOGIN_VERB: Final[str] = "login"
#: ``facet auth logout``: remove a package's stored sign-in secrets, locally.
LOGOUT_VERB: Final[str] = "logout"
#: Printed when no installed package offers a browser sign-in.
NOTHING_OFFERED: Final[str] = (
    "nothing installed offers a browser sign-in; `facet pull <package>` first"
)
#: Printed when the person cancels the choice without picking a package.
NOTHING_SELECTED: Final[str] = "nothing selected"
#: Printed when the wizard ends with nothing picked and nothing stored.
NOTHING_CHANGED: Final[str] = "nothing changed"
#: Printed when no installed package declares a secret to guide through.
NOTHING_WITH_SECRETS: Final[str] = (
    "nothing installed declares a secret; `facet pull <package>` first"
)
#: Printed when a package holds no stored sign-in secret to remove.
NOTHING_STORED: Final[str] = "nothing stored"
_SELECT_MESSAGE: Final[str] = "Choose an account — arrows move, enter picks [default: done]:"
_DONE: Final[object] = object()


def _resolve_base(package_name: str) -> Package:
    """The named package: it must exist and be compatible with this facet."""
    active = load_active_registry()
    package = active.package(package_name)
    if package is None:
        raise FacetError(
            "invalid_input",
            f"unknown package: {package_name}",
            details={"known": sorted(active.packages)},
        )
    package.require_compatible()
    return package


def _require_installed(package: Package, *, allow_outdated: bool = False) -> None:
    """Refuse a package whose environment is not on this machine."""
    home = Home.from_env()
    if env_module.is_installed(package, home):
        return
    if allow_outdated and _is_outdated(package, home):
        return
    raise FacetError(
        "not_installed",
        f"{package.name} {package.version} is not installed",
        hint=f"facet pull {package.name}",
        details={"package": package.name},
    )


def _resolve(package_name: str, *, allow_outdated: bool = False) -> Package:
    """The package to sign in to: it must exist, be compatible and be installed."""
    package = _resolve_base(package_name)
    if not has_auth_verb(package):
        raise FacetError(
            "invalid_input",
            f"{package.name} provides no auth verb",
            hint=f"facet secret set {package.name} <name>",
            details={"package": package.name},
        )
    _require_installed(package, allow_outdated=allow_outdated)
    return package


def resolve_wizard(package_name: str) -> Package:
    """The package to guide: installed and compatible; no auth verb needed.

    Packages without a browser sign-in get the typed prompts and the check
    only, so the wizard resolves them where the verb runner refuses them.
    An outdated package is allowed: the caller decides what to do with it.
    """
    package = _resolve_base(package_name)
    _require_installed(package, allow_outdated=True)
    return package


def _returned(result: Mapping[str, object]) -> dict[str, str]:
    """The result file's ``secrets`` map, every value a non-empty string."""
    returned = result.get(RESULT_KEY)
    if not isinstance(returned, dict):
        raise FacetError(
            "runner_failed",
            f"the auth verb returned no {RESULT_KEY} map",
            details={"returned": sorted(result)},
        )
    values: dict[str, str] = {}
    for name, value in returned.items():
        if not isinstance(name, str) or not isinstance(value, str) or not value:
            raise FacetError(
                "runner_failed",
                f"the auth verb returned an unusable secret {name}",
                details={"name": str(name)},
            )
        values[name] = value
    return values


def _refuse_undeclared(package: Package, names: list[str]) -> None:
    """Refuse the whole result when it names a secret the manifest did not declare."""
    declared = {secret.name for secret in package.secrets}
    unknown = sorted(set(names) - declared)
    if unknown:
        raise FacetError(
            "invalid_input",
            f"{package.name} did not declare {', '.join(unknown)}",
            hint=f"declare it under [[secrets]] in {package.name}'s manifest",
            details={"package": package.name, "undeclared": unknown, "declared": sorted(declared)},
        )


def _scrub_log(run_id: str, values: Mapping[str, str]) -> None:
    """Replace freshly stored values in this run's log with the fixed mask."""
    renewals.scrub(run_id, values)


def _terminal() -> bool:
    """Whether both standard streams are terminals, so prompting is safe."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _manual_hint(prefix: str | None = None) -> str | None:
    """The commands that set each unstored typed secret by hand.

    ``None`` when nothing is missing, so the refusal keeps its bare hint;
    a locked vault reads as nothing missing rather than a second failure.
    """
    try:
        registry = load_active_registry()
        home = Home.from_env()
        declared = sorted(
            (package for package in registry.packages.values() if package.secrets),
            key=lambda package: package.name,
        )
        found = secrets.rows(home, declared)
    except FacetError:
        found = []
    commands = [
        f"facet secret set {row['package']} {row['name']}"
        for row in found
        if row.get("set") is not True and row.get("produced_by") != "auth"
    ]
    parts = ([prefix] if prefix else []) + commands
    return "; ".join(parts) if parts else None


def _refused(args: argparse.Namespace, known: list[str]) -> None:
    """Refuse the wizard when it cannot ask: ``--json`` or no terminal."""
    if bool(getattr(args, "json", False)):
        raise FacetError(
            "invalid_input",
            "the following arguments are required: <package>",
            hint=_manual_hint(),
        )
    if not _terminal():
        raise FacetError(
            "confirmation_required",
            "picking what to sign in to needs a terminal",
            hint=_manual_hint("facet auth <package>"),
            details={"packages": known},
        )


def _version_present(home: Home, name: str, version: str) -> bool:
    """Whether an environment for ``name`` at ``version`` is on disk."""
    return (home.envs_dir / name / version).is_dir() or (home.bin_dir / name / version).is_dir()


def _is_outdated(package: Package, home: Home) -> bool:
    """Installed at some version, but not the registry's current one."""
    if env_module.is_installed(package, home):
        return False
    installed_version = installed_module.version_of(home, package.name)
    if installed_version is None or installed_version == package.version:
        return False
    return _version_present(home, package.name, installed_version)


def _installed_any_version(registry: Registry, home: Home) -> list[tuple[Package, bool]]:
    """Installed packages with their outdated flag, sorted by name."""
    found: list[tuple[Package, bool]] = []
    for name in sorted(registry.packages):
        package = registry.packages[name]
        if env_module.is_installed(package, home):
            found.append((package, False))
        elif _is_outdated(package, home):
            found.append((package, True))
    return found


def _installed(registry: Registry, home: Home) -> list[Package]:
    """Every installed package, sorted by name."""
    return [
        registry.packages[name]
        for name in sorted(registry.packages)
        if env_module.is_installed(registry.packages[name], home)
    ]


#: Printed when every package declaring a secret is already fully signed in.
ALL_SIGNED_IN: Final[str] = "every package is signed in; sign out with facet auth logout <package>"


def _package_state(home: Home, package: Package) -> str:
    """The package's stored state: ``set``, ``partly set`` or ``not set``."""
    found = secrets.rows(home, [package])
    flags = [row.get("set") is True for row in found]
    if all(flags):
        return "set"
    if not any(flags):
        return "not set"
    return "partly set"


def _outdated_suffix(name: str, outdated: bool) -> str:
    return f", outdated: facet pull {name}" if outdated else ""


def _select_styled(packages: list[tuple[Package, bool]]) -> str | None:
    """The styled questionary choice, or ``None`` on done."""
    prompts = [
        questionary.Choice(
            title=[
                ("class:accent", package.name),
                ("", f" {package.version}{_outdated_suffix(package.name, outdated)}"),
            ],
            value=package.name,
        )
        for package, outdated in packages
    ]
    prompts.append(questionary.Choice(title="done", value=_DONE))
    picked = questionary.select(
        _SELECT_MESSAGE, choices=prompts, style=theme.menu_style()
    ).unsafe_ask()
    return None if picked is _DONE else str(picked)


def _select_plain(packages: list[tuple[Package, bool]]) -> str | None:
    """The numbered-list choice, or ``None`` on done, blank, EOF, or a miss."""
    names = [package.name for package, _ in packages]
    for index, (package, outdated) in enumerate(packages, 1):
        print(f"{index}. {package.name}{_outdated_suffix(package.name, outdated)}")
    try:
        answer = input(f"Choose an account [1-{len(names)}, default: done]: ").strip()
    except EOFError:
        return None
    return _match_answer(answer, names)


def _match_answer(answer: str, names: list[str]) -> str | None:
    """A name, a 1-based row number, or ``None`` for anything else."""
    if not answer:
        return None
    if answer in names:
        return answer
    try:
        index = int(answer)
    except ValueError:
        return None
    return names[index - 1] if 1 <= index <= len(names) else None


def _select(packages: list[tuple[Package, bool]]) -> str | None:
    """The chosen package name, or ``None`` on done, blank, or EOF."""
    if theme.styled():
        return _select_styled(packages)
    return _select_plain(packages)


def pick(args: argparse.Namespace) -> str | None:
    """The package to guide, chosen at the terminal, or ``None`` for nothing.

    Only installed packages that declare a secret and are not fully signed
    in are offered, one at a time with nothing preselected; the ``done``
    row leaves.  An outdated package is listed with a pull hint; choosing it
    prints the hint and returns to the menu.  An empty answer, ``done``, or
    ``EOF`` prints ``nothing changed``; when nothing is offered because every
    package is signed in, that is said instead.
    """
    registry = load_active_registry()
    home = Home.from_env()
    _refused(args, sorted(registry.packages))
    banner.print_text()
    with_secrets = [
        (package, outdated)
        for package, outdated in _installed_any_version(registry, home)
        if package.secrets
    ]
    if not with_secrets:
        print(NOTHING_WITH_SECRETS)
        return None
    offered = [
        (package, outdated)
        for package, outdated in with_secrets
        if _package_state(home, package) in ("not set", "partly set")
    ]
    if not offered:
        print(ALL_SIGNED_IN)
        return None
    outdated_by_name = {package.name: outdated for package, outdated in offered}
    while True:
        try:
            chosen = _select(offered)
        except KeyboardInterrupt:
            print(NOTHING_CHANGED)
            return None
        if chosen is None:
            print(NOTHING_CHANGED)
            return None
        if outdated_by_name.get(chosen):
            print(f"{chosen} is outdated; run facet pull {chosen}, then try again")
            continue
        return resolve_wizard(chosen).name


def handle(package_name: str, run_id: str, allow_open: bool = True) -> dict[str, object]:
    """Run the package's ``auth`` verb once, store what it returns, and forget the rest."""
    package = _resolve(package_name)
    home = Home.from_env()
    temp_dir = home.tmp_dir / f".facet-auth-{run_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    inv = Invocation(
        package,
        home,
        home.env_dir(package.name, package.version),
        temp_dir,
        run_id,
        timeout_s=None,
        allow_open=allow_open,
    )
    request = runner.make_request(
        AUTH_VERB,
        package.capabilities[0].id if package.capabilities else package.name,
        {},
        temp_dir,
        package.directory,
        {},
    )
    try:
        outcome = runner.call(inv, AUTH_VERB, request)
        values = _returned(outcome.result)
        _refuse_undeclared(package, list(values))
        for name, value in values.items():
            set_value(home, package, name, value)
        _scrub_log(run_id, values)
    finally:
        # Always: a result file holding a token must never outlive this call, and
        # the directory is never recorded as a kept leftover.
        shutil.rmtree(temp_dir, ignore_errors=True)
    return {
        "package": package.name,
        "secrets": sorted(values),
        "path": str(vault_file(home)),
    }


def _logout_refused(args: argparse.Namespace, known: list[str]) -> None:
    """Refuse the logout wizard when it cannot ask: ``--json`` or no terminal."""
    if bool(getattr(args, "json", False)):
        raise FacetError("invalid_input", "the following arguments are required: <package>")
    if not _terminal():
        raise FacetError(
            "confirmation_required",
            "picking what to sign out of needs a terminal",
            hint="facet auth logout <package>",
            details={"packages": known},
        )


def _logout_target(args: argparse.Namespace, target: str | None) -> Package | None:
    """The package to sign out of: the named one, or one chosen at the terminal.

    Without a name only packages holding at least one stored secret are
    offered, one at a time with nothing preselected; the ``done`` row leaves.
    An outdated package is listed with a pull hint and can still be signed out.
    """
    if target is not None:
        return _resolve(target, allow_outdated=True)
    registry = load_active_registry()
    _logout_refused(args, sorted(registry.packages))
    banner.print_text()
    home = Home.from_env()
    offered = [
        (package, outdated)
        for package, outdated in _installed_any_version(registry, home)
        if _stored(home, package)
    ]
    if not offered:
        print(NOTHING_SELECTED)
        return None
    chosen = _select(offered)
    if chosen is None:
        print(NOTHING_SELECTED)
        return None
    return _resolve(chosen, allow_outdated=True)


def _stored(home: Home, package: Package) -> list[str]:
    """The package's declared secret names that currently hold a value."""
    rows = secrets.rows(home, [package])
    return [str(row["name"]) for row in rows if row.get("set") is True]


def _remove_stored(
    home: Home,
    package: Package,
    names: list[str],
    args: argparse.Namespace,
) -> list[str]:
    """Remove each chosen name through ``secret rm``'s path, under one exclusive lock."""
    wait = bool(getattr(args, "wait", True))
    timeout_s = float(getattr(args, "wait_timeout", 3600.0))
    removed: list[str] = []
    with lock.hold("exclusive", "facet auth logout", wait, timeout_s):
        for name in names:
            if secrets.remove(home, package, name)["removed"] is True:
                removed.append(name)
    return removed


def logout(args: argparse.Namespace, target: str | None) -> dict[str, object] | None:
    """Remove every stored sign-in secret of the package at once, locally.

    ``None`` only when no package was chosen.  Nothing leaves this machine:
    no network call, no revocation.  Re-authenticating with
    ``facet auth login <package>`` is the way back.  A package with nothing
    stored is a no-op that still reports its empty ``removed`` list.
    """
    package = _logout_target(args, target)
    if package is None:
        return None
    home = Home.from_env()
    names = _stored(home, package)
    done: dict[str, object] = {
        "package": package.name,
        "removed": [],
        "path": str(vault_file(home)),
    }
    if not names:
        return done
    done["removed"] = _remove_stored(home, package, names, args)
    return done
