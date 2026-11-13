"""Build one asset's preview through its package runner's ``preview`` verb.

This is the code the single-asset form (``facet preview <asset>``) and the
gallery's ``/open/<n>`` share: resolve the record's capability, require its
package to be installed, and run the runner. The gallery server answers when
the build is done; there is no background work.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path

from facetmedia import env, registry, runner
from facetmedia.contract import FacetError
from facetmedia.home import Home
from facetmedia.preview_folder import PREVIEW_SUFFIX, load_preview
from facetmedia.registry import Capability, Package

__all__ = [
    "ensure",
    "installed_packages",
    "rebuild",
]


def rebuild(
    home: Home,
    run_id: str,
    capability_id: str,
    stored: Mapping[str, object],
    asset_path: Path,
) -> dict[str, object]:
    """The runner's fresh preview: the package must be installed to rebuild."""
    _capability_record, package = _resolve(capability_id)
    if not env.is_installed(package, home):
        raise FacetError(
            "not_installed",
            f"{package.name} {package.version} is not installed",
            hint=f"facet pull {package.name}",
            details={"package": package.name, "version": package.version},
        )
    params = _params_of(stored, str(asset_path))
    preview_file = asset_path.parent / (asset_path.name + PREVIEW_SUFFIX)
    return ensure(package, home, run_id, capability_id, params, asset_path, preview_file)


def ensure(
    package: Package,
    home: Home,
    run_id: str,
    capability_id: str,
    params: dict[str, object],
    asset_path: Path,
    preview_file: Path,
) -> dict[str, object]:
    """The parsed preview file, regenerated when missing or older than asset."""
    try:
        asset_mtime = asset_path.stat().st_mtime
    except OSError as exc:
        raise FacetError("invalid_input", f"{asset_path}: cannot read asset") from exc
    try:
        preview_mtime = preview_file.stat().st_mtime
    except OSError:
        preview_mtime = None
    if preview_mtime is not None and preview_mtime >= asset_mtime:
        return load_preview(preview_file)
    preview_file.unlink(missing_ok=True)
    temp_dir = home.tmp_dir / f".facet-preview-{run_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        invocation = runner.Invocation(
            package,
            home,
            home.env_dir(package.name, package.version),
            temp_dir,
            run_id,
            None,
            False,
        )
        request = runner.make_request(
            "preview",
            capability_id,
            params,
            asset_path.parent,
            asset_path.parent,
            runner.model_paths(package, home),
            [asset_path],
        )
        runner.call(invocation, "preview", request)
        preview_data = load_preview(preview_file)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return preview_data


def installed_packages(names: Iterable[str | None]) -> frozenset[str]:
    """The names whose package resolves and is installed in this home."""
    try:
        home = Home.from_env()
        active = registry.load_active_registry()
    except FacetError:
        return frozenset()
    kept = {name for name in names if name is not None and _installed(active.package(name), home)}
    return frozenset(kept)


def _installed(package: Package | None, home: Home) -> bool:
    """Whether ``package`` resolved and its environment is complete."""
    return package is not None and env.is_installed(package, home)


def _resolve(capability_id: str) -> tuple[Capability, Package]:
    """The capability and its package; an unknown id lists the known ids."""
    active = registry.load_active_registry()
    capability = active.capability(capability_id)
    package = active.package(capability.package)
    if package is None:
        raise FacetError(
            "internal",
            f"{capability_id}: its package {capability.package} is not in the registry",
        )
    return capability, package


def _params_of(stored: Mapping[str, object], asset: str) -> dict[str, object]:
    """The record's params for the runner's ``preview`` verb."""
    provenance = stored.get("provenance")
    if not isinstance(provenance, dict):
        raise FacetError("invalid_input", f"{asset}: the record has no provenance")
    params = provenance.get("params")
    if not isinstance(params, dict):
        raise FacetError("invalid_input", f"{asset}: the record has no params")
    return {str(key): value for key, value in params.items()}
