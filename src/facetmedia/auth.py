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

import shutil
from collections.abc import Mapping
from typing import Final

from facetmedia import env as env_module
from facetmedia import renewals, runner
from facetmedia.contract import FacetError
from facetmedia.home import Home
from facetmedia.registry import Package, load_active_registry
from facetmedia.runner import Invocation
from facetmedia.secrets import has_auth_verb, set_value, vault_file

__all__ = ["AUTH_VERB", "RESULT_KEY", "handle"]

#: The verb a package registers to produce secrets through a browser flow.
AUTH_VERB: Final[str] = "auth"
#: The result-file key the returned secrets travel in.
RESULT_KEY: Final[str] = "secrets"


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
