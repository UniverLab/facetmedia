"""The ``host-app`` engine's share of ``facet doctor``, split out for file size.

``doctor`` stays the owner of the check ordering and the
pull-fixable/machine-cannot split; this module renders the host-app
``installed`` readiness entry and the package-doctor entry that fails with the
exact ``facet config set`` fix.  ``Check`` is imported lazily because
``doctor`` imports this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from facetmedia import config, doctor_node
from facetmedia.contract import FacetError
from facetmedia.home import Home
from facetmedia.registry_types import Package

if TYPE_CHECKING:
    from facetmedia.doctor import Check


def engine_installed_check(package: Package, home: Home) -> Check | None:
    """The ``installed`` check for either engine without a python environment.

    A host-app package reports its application's resolution and version; every
    other package keeps the node engine's check.
    """
    from facetmedia.doctor import Check  # noqa: PLC0415

    app = package.host_app
    if app is None:
        return doctor_node.installed_check(package, home)
    problem = config.host_app_problem(app, home)
    if problem is None:
        return Check(True, f"{app.app} {app.min_version}+ found")
    return Check(False, problem.message, problem.fix)


def app_doctor(package: Package, home: Home) -> tuple[list[dict[str, object]], FacetError | None]:
    """The package-doctor entry and the classified failure, if the app is unusable.

    A missing or too-old host application is not pull-fixable: no pull changes
    it, so the failure reaches the gate as ``machine_cannot`` carrying the
    config fix as its hint.
    """
    app = package.host_app
    assert app is not None
    problem = config.host_app_problem(app, home)
    if problem is None:
        return [_entry(True, f"{app.app} {app.min_version}+ found", "")], None
    error = FacetError("machine_cannot", problem.message, hint=problem.fix, details=problem.details)
    return [_entry(False, problem.message, problem.fix)], error


def _entry(ok: bool, detail: str, fix: str) -> dict[str, object]:
    return {"name": "host_app", "ok": ok, "detail": detail, "fix": fix}
