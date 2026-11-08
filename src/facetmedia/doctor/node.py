"""The ``node`` engine's share of ``facet doctor``, split out for file size.

``doctor`` stays the owner of the check ordering and the
pull-fixable/machine-cannot split; this module only renders the node
engine's readiness entry, its ``installed`` entry and its package-doctor
entry.  ``Check`` is imported lazily because ``doctor`` imports this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from facetmedia.home import Home
from facetmedia.registry_types import Package

if TYPE_CHECKING:
    from facetmedia.doctor import Check


def readiness(package: Package, home: Home) -> Check:
    """The ``node`` readiness check: verified extractions, and a runnable binary."""
    from facetmedia import env as env_module  # noqa: PLC0415
    from facetmedia.doctor import Check  # noqa: PLC0415

    node = package.node
    if node is None:
        return Check(True, "no node engine declared")
    problem = env_module.node_problem(package, home)
    if problem is None:
        return Check(True, f"node {node.runtime} verified and runnable")
    # Verified extractions that cannot run are machine_cannot, not pull-fixable.
    fix = "" if env_module.node_present(package, home) else f"facet pull {package.name}"
    return Check(False, problem, fix)


def installed_check(package: Package, home: Home) -> Check | None:
    """The ``installed`` readiness check for a node package, else ``None``."""
    from facetmedia import env as env_module  # noqa: PLC0415
    from facetmedia.doctor import Check  # noqa: PLC0415

    if package.node is None:
        return None
    if env_module.node_present(package, home):
        detail = f"node {package.node.runtime} verified under {home.runtimes_dir}"
        return Check(True, detail)
    return Check(
        False,
        "the node runtime or a package extraction is not verified",
        f"facet pull {package.name}",
    )


def doctor_entry(package: Package, home: Home) -> dict[str, object] | None:
    """The package-doctor entry for a node engine that is present but unusable.

    Only the cannot-run case reaches a non-``None`` result — a missing
    extraction already fails ``client_env_present`` — so the fix is never
    ``facet pull`` in practice and the gate classifies it ``machine_cannot``.
    """
    from facetmedia import env as env_module  # noqa: PLC0415

    problem = env_module.node_problem(package, home)
    if problem is None:
        return None
    fix = "" if env_module.node_present(package, home) else f"facet pull {package.name}"
    return {"name": "node", "ok": False, "detail": problem, "fix": fix}
