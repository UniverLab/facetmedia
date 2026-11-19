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

from facetmedia import banner, renewals, runner, theme
from facetmedia import env as env_module
from facetmedia.contract import FacetError
from facetmedia.home import Home
from facetmedia.registry import Package, load_active_registry
from facetmedia.runner import Invocation
from facetmedia.secrets import auth_hint, has_auth_verb, set_value, vault_file

if TYPE_CHECKING:
    from facetmedia.registry_types import Registry

__all__ = ["AUTH_VERB", "NOTHING_OFFERED", "NOTHING_SELECTED", "RESULT_KEY", "handle", "pick"]

#: The verb a package registers to produce secrets through a browser flow.
AUTH_VERB: Final[str] = "auth"
#: The result-file key the returned secrets travel in.
RESULT_KEY: Final[str] = "secrets"
#: Printed when no installed package offers a browser sign-in.
NOTHING_OFFERED: Final[str] = (
    "nothing installed offers a browser sign-in; `facet pull <package>` first"
)
#: Printed when the person cancels the choice without picking a package.
NOTHING_SELECTED: Final[str] = "nothing selected"
_SELECT_MESSAGE: Final[str] = "Choose an account — arrows move, enter picks [default: done]:"
_DONE: Final[object] = object()


def _resolve(package_name: str) -> Package:
    """The package to sign in to: it must exist, be compatible and be installed."""
    active = load_active_registry()
    package = active.package(package_name)
    if package is None:
        raise FacetError(
            "invalid_input",
            f"unknown package: {package_name}",
            details={"known": sorted(active.packages)},
        )
    package.require_compatible()
    if not has_auth_verb(package):
        raise FacetError(
            "invalid_input",
            f"{package.name} provides no auth verb",
            hint=f"facet secret set {package.name} <name>",
            details={"package": package.name},
        )
    home = Home.from_env()
    if not env_module.is_installed(package, home):
        raise FacetError(
            "not_installed",
            f"{package.name} {package.version} is not installed",
            hint=f"facet pull {package.name}",
            details={"package": package.name},
        )
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


def _refused(args: argparse.Namespace, known: list[str]) -> None:
    """Refuse the wizard when it cannot ask: ``--json`` or no terminal."""
    if bool(getattr(args, "json", False)):
        raise FacetError("invalid_input", "the following arguments are required: <package>")
    if not _terminal():
        raise FacetError(
            "confirmation_required",
            "picking what to sign in to needs a terminal",
            hint="facet auth <package>",
            details={"packages": known},
        )


def _installed(registry: Registry, home: Home) -> list[Package]:
    """Every installed package, sorted by name."""
    return [
        registry.packages[name]
        for name in sorted(registry.packages)
        if env_module.is_installed(registry.packages[name], home)
    ]


def _line(package: Package) -> str:
    """One offered package: name, version and the secret names it declares."""
    names = ", ".join(secret.name for secret in package.secrets)
    return f"{package.name} {package.version} ({names})"


def _excluded_line(package: Package) -> str:
    """One installed package with no auth verb, and the command that fits it."""
    first = package.secrets[0].name if package.secrets else "<name>"
    return f"not offered: {package.name} — no browser sign-in; use `{auth_hint(package, first)}`"


def _select_styled(packages: list[Package]) -> str | None:
    """The styled questionary choice, or ``None`` on done."""
    prompts = [questionary.Choice(title=_line(package), value=package.name) for package in packages]
    prompts.append(questionary.Choice(title="done", value=_DONE))
    picked = questionary.select(_SELECT_MESSAGE, choices=prompts).unsafe_ask()
    return None if picked is _DONE else str(picked)


def _select_plain(names: list[str]) -> str | None:
    """The numbered-list choice, or ``None`` on done, blank, EOF, or a miss."""
    for index, name in enumerate(names, 1):
        print(f"{index}. {name}")
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


def _select(packages: list[Package]) -> str | None:
    """The chosen package name, or ``None`` on done, blank, or EOF."""
    if theme.styled():
        return _select_styled(packages)
    return _select_plain([package.name for package in packages])


def pick(args: argparse.Namespace) -> str | None:
    """The package to sign in to, chosen at the terminal, or ``None`` for nothing."""
    registry = load_active_registry()
    home = Home.from_env()
    _refused(args, sorted(registry.packages))
    banner.print_text()
    installed = _installed(registry, home)
    offered = [package for package in installed if has_auth_verb(package)]
    for package in installed:
        if not has_auth_verb(package):
            print(_excluded_line(package))
    if not offered:
        print(NOTHING_OFFERED)
        return None
    chosen = _select(offered)
    if chosen is None:
        print(NOTHING_SELECTED)
        return None
    return _resolve(chosen).name


def handle(package_name: str, run_id: str) -> dict[str, object]:
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
