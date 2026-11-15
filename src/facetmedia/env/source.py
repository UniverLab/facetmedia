"""The pinned source checkout: importable code with no server.

Some tools ship Python code that is not pip-installable and serves no API.
A package like that declares ``[engine.source]`` with the repo and the exact
commit; ``facet pull`` clones it once per home at
``<home>/servers/<package>/<version>/<shortrev>/`` — a shallow, branchless
fetch of exactly ``rev``, skipped when that directory already holds a
verified checkout.  The runner receives the path in ``FACET_SOURCE_DIR`` and
puts it on ``sys.path`` itself.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from facetmedia.env.package_marker import COMPLETE
from facetmedia.home import Home, atomic_write_text, uv_env
from facetmedia.registry_types import Package


def source_dir(package: Package, home: Home) -> Path:
    """The checkout itself: ``<home>/servers/<package>/<version>/<shortrev>``."""
    from facetmedia import env as env_module  # noqa: PLC0415

    source = package.source
    assert source is not None
    return home.servers_dir / package.name / package.version / source.rev[: env_module.SHORT_REV]


def source_present(package: Package, home: Home) -> bool:
    """Whether the verified checkout holds this source's rev.

    A package without ``[engine.source]`` is always present: there is
    nothing to fetch.  The marker is written last, after the checkout
    lands, so a crash mid-pull is redone by the next pull.
    """
    if package.source is None:
        return True
    try:
        marker = (source_dir(package, home) / COMPLETE).read_text(encoding="utf-8")
    except OSError:
        return False
    return marker.strip() == package.source.rev


def fetch_source(package: Package, home: Home) -> None:
    """Clone the declared source, unless its marker verifies."""
    from facetmedia import env as env_module  # noqa: PLC0415

    source = package.source
    if source is None or source_present(package, home):
        return
    root = home.servers_dir / package.name / package.version
    if root.exists():
        shutil.rmtree(root)
    target = source_dir(package, home)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        env_module.git_checkout(target, source.repo, source.rev, uv_env(home))
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    atomic_write_text(target / COMPLETE, source.rev, home.tmp_dir)
