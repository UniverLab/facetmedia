"""The ``binary`` engine's share of ``facet doctor``, split out for file size.

``doctor`` stays the owner of the check ordering and the
pull-fixable/machine-cannot split; this module renders the binary engine's
``installed`` readiness entry and its package-doctor entry.  ``Check`` is
imported lazily because ``doctor`` imports this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from facetmedia.env import binary as env_binary
from facetmedia.home import Home
from facetmedia.registry_types import Package

if TYPE_CHECKING:
    from facetmedia.doctor import Check


def installed_check(package: Package, home: Home) -> Check | None:
    """The ``installed`` readiness check for a binary package, else ``None``."""
    from facetmedia.doctor import Check  # noqa: PLC0415

    binary = package.binary
    if binary is None:
        return None
    problem = env_binary.binary_problem(package, home)
    if problem is None:
        detail = f"binary {binary.name} {binary.version} verified and runnable"
        return Check(True, detail)
    # Verified extractions that cannot run are machine_cannot, not pull-fixable.
    fix = "" if env_binary.binary_present(binary, home) else f"facet pull {package.name}"
    return Check(False, problem, fix)


def package_doctor(package: Package, home: Home) -> tuple[list[dict[str, object]], None] | None:
    """The package-doctor entry for a binary engine that is present or broken.

    A binary package has no runner verb of its own to call: the probe facet
    runs is ``<bin> <version_arg>`` itself.  Only the cannot-run case
    produces a fix other than ``facet pull`` — a missing extraction is
    pull-fixable and never reaches the gate.
    """
    binary = package.binary
    if binary is None:
        return None
    problem = env_binary.binary_problem(package, home)
    if problem is None:
        detail = f"binary {binary.name} {binary.version} verified and runnable"
        return [{"name": "binary", "ok": True, "detail": detail, "fix": ""}], None
    fix = "" if env_binary.binary_present(binary, home) else f"facet pull {package.name}"
    return [{"name": "binary", "ok": False, "detail": problem, "fix": fix}], None
