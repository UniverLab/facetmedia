"""Compatibility checks that run before package manifests are interpreted."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from facetmedia.registry_types import SkippedPackage, admits_facet

if TYPE_CHECKING:
    from facetmedia.manifest import RegistryLoader


def leaves_out(loader: RegistryLoader, name: str, file: str, data: dict[str, Any]) -> bool:
    """Skip a package whose valid facet floor excludes this core."""
    requires_facet = loader.requires_facet(data, file)
    if requires_facet is None or admits_facet(requires_facet):
        return False
    loader.skipped.append(SkippedPackage(name, requires_facet))
    if loader.strict:
        loader.add(file, "requires_facet", f"needs facet {requires_facet}")
    return True
