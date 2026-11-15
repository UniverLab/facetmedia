"""``facet preview <folder>``: every recorded asset below one folder, as data.

Folder mode starts no runner until a card is opened. It walks the folder,
keeps each file that has a sibling ``.facet.json`` record, and classifies
the asset's preview file as ``fresh``, ``stale`` (older than the asset) or
``missing``. The person form serves those cards through the gallery
(``facetmedia.preview_server``), which builds a card's preview when it is
opened; the agent form returns them as ``--json`` data.

The preview-file questions live here so the single-asset form answers "is
this preview fresh?" with the same code as the folder form.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from facetmedia import record, registry
from facetmedia.contract import FacetError

__all__ = [
    "FRESH",
    "KINDS",
    "MISSING",
    "PREVIEW_SUFFIX",
    "STALE",
    "FolderAsset",
    "card",
    "collect",
    "entry_data",
    "gallery_data",
    "load_preview",
    "preview_images",
    "preview_state",
    "refreshed",
    "served_names",
    "text_table",
]

#: ``echo.txt`` -> ``echo.txt.preview.json``, beside the asset.
PREVIEW_SUFFIX: Final[str] = ".preview.json"

#: A preview file that exists and is not older than its asset.
FRESH: Final[str] = "fresh"
#: A preview file older than its asset.
STALE: Final[str] = "stale"
#: No preview file at all.
MISSING: Final[str] = "missing"

#: The asset kinds ``--kind`` accepts.
KINDS: Final[tuple[str, ...]] = ("audio", "cad", "document", "image", "text", "video")


@dataclass(frozen=True)
class FolderAsset:
    """One recorded asset a folder preview found, with its preview state."""

    n: int
    asset: str
    path: Path
    name: str | None
    package: str | None
    facet: str | None
    kind: str
    capability: str
    created: str | None
    verified: bool
    bytes: int | None
    duration_s: float | None
    state: str
    preview: Mapping[str, object] | None
    asset_names: frozenset[str]
    thumb: Path | None


def collect(folder: Path, kinds: frozenset[str] | None = None) -> tuple[FolderAsset, ...]:
    """Every recorded asset below ``folder``, sorted by relative path.

    ``kinds`` keeps only the assets of those kinds. A folder with nothing
    left fails with ``invalid_input`` naming the folder.
    """
    if not folder.is_dir():
        raise FacetError(
            "invalid_input", f"{folder}: not a folder", details={"folder": str(folder)}
        )
    entries = [_entry(folder, path) for path in _recorded(folder)]
    if kinds is not None:
        entries = [entry for entry in entries if entry.kind in kinds]
    if not entries:
        raise FacetError(
            "invalid_input", f"{folder}: no assets to preview", details={"folder": str(folder)}
        )
    facets = _package_facets()
    return tuple(
        replace(entry, n=index, facet=_facet_of(facets, entry.package))
        for index, entry in enumerate(entries)
    )


def entry_data(entry: FolderAsset) -> dict[str, object]:
    """One ``--json`` entry: the card's fields plus the parsed preview."""
    data = _fields(entry)
    data["preview"] = dict(entry.preview) if entry.preview is not None else None
    return data


def card(entry: FolderAsset, *, openable: frozenset[str]) -> dict[str, object]:
    """One gallery card: the ``--json`` fields except ``preview``, plus routes.

    A ``fresh`` card always opens. A ``stale`` or ``missing`` card opens when
    its package is installed — opening it builds the preview first — and has
    ``open: null`` otherwise.
    """
    data = _fields(entry)
    data["n"] = entry.n
    data["name"] = entry.name
    data["package"] = entry.package
    data["thumb"] = f"/thumb/{entry.n}" if entry.thumb is not None else None
    data["open"] = (
        f"/open/{entry.n}"
        if entry.state == FRESH or (entry.package is not None and entry.package in openable)
        else None
    )
    return data


def gallery_data(
    entries: Sequence[FolderAsset], folder_name: str, *, openable: frozenset[str]
) -> dict[str, object]:
    """The document ``/gallery.json`` serves and the envelope carries."""
    return {"folder": folder_name, "assets": [card(entry, openable=openable) for entry in entries]}


def refreshed(entry: FolderAsset) -> FolderAsset:
    """``entry`` reread from its asset: the state after a preview was built."""
    preview_file = entry.path.parent / (entry.path.name + PREVIEW_SUFFIX)
    state = preview_state(entry.path, preview_file)
    preview = load_preview(preview_file) if state == FRESH else None
    return replace(
        entry,
        state=state,
        preview=preview,
        asset_names=_asset_names(entry.path, preview_file, preview),
        thumb=_thumb(entry.kind, state, entry.path, preview),
    )


def text_table(entries: Sequence[FolderAsset], folder_name: str) -> str:
    """The requirement-3 list as a plain table, for a registry without gallery."""
    header = ("n", "state", "kind", "verified", "bytes", "duration", "asset")
    rows = [_table_entry(entry) for entry in entries]
    widths = [max(len(row[column]) for row in (header, *rows)) for column in range(len(header))]
    lines = [f"{folder_name}/ ({len(rows)} assets)", _table_row(header, widths)]
    lines.extend(_table_row(row, widths) for row in rows)
    return "\n".join(lines)


def _table_entry(entry: FolderAsset) -> tuple[str, ...]:
    """One table row: the card's short fields, then its asset path."""
    duration = f"{entry.duration_s:g}s" if entry.duration_s is not None else "-"
    return (
        str(entry.n),
        entry.state,
        entry.kind,
        "yes" if entry.verified else "no",
        str(entry.bytes) if entry.bytes is not None else "-",
        duration,
        entry.asset,
    )


def _table_row(row: tuple[str, ...], widths: list[int]) -> str:
    cells = [cell.ljust(width) for cell, width in zip(row, widths, strict=True)]
    return "  " + "  ".join(cells).rstrip()


def _fields(entry: FolderAsset) -> dict[str, object]:
    """The fields requirements 3 and 4 share, without the preview."""
    return {
        "asset": entry.asset,
        "kind": entry.kind,
        "capability": entry.capability,
        "facet": entry.facet,
        "created": entry.created,
        "verified": entry.verified,
        "bytes": entry.bytes,
        "duration_s": entry.duration_s,
        "preview_state": entry.state,
    }


def _recorded(folder: Path) -> list[Path]:
    """Every file below ``folder`` with a sibling record, sorted by relative path."""
    found: list[Path] = []
    for path in folder.rglob("*"):
        if not path.is_file() or path.name.endswith(record.RECORD_SUFFIX):
            continue
        if record.record_path(path).is_file():
            found.append(path)
    return sorted(found, key=lambda path: path.relative_to(folder).as_posix())


def _entry(folder: Path, path: Path) -> FolderAsset:
    """One asset, read from its record and its preview file."""
    stored = record.read_record(path)
    preview_file = path.parent / (path.name + PREVIEW_SUFFIX)
    state = preview_state(path, preview_file)
    preview = load_preview(preview_file) if state == FRESH else None
    kind = _text(stored, "kind") or ""
    return FolderAsset(
        n=0,
        asset=path.relative_to(folder).as_posix(),
        path=path,
        name=_text(stored, "name"),
        package=_package(stored),
        facet=None,
        kind=kind,
        capability=_capability(stored, path),
        created=_text(stored, "created"),
        verified=_verified(stored),
        bytes=_integer(stored, "bytes"),
        duration_s=_duration(stored),
        state=state,
        preview=preview,
        asset_names=_asset_names(path, preview_file, preview),
        thumb=_thumb(kind, state, path, preview),
    )


def _capability(stored: Mapping[str, object], path: Path) -> str:
    """The capability id the record's provenance names."""
    provenance = stored.get("provenance")
    if not isinstance(provenance, Mapping):
        raise FacetError("invalid_input", f"{path}: the record has no provenance")
    capability = _text(provenance, "capability")
    if capability is None:
        raise FacetError("invalid_input", f"{path}: the record names no capability")
    return capability


def _package(stored: Mapping[str, object]) -> str | None:
    """The record's package name, which the fix hint names."""
    provenance = stored.get("provenance")
    if not isinstance(provenance, Mapping):
        return None
    package = provenance.get("package")
    return _text(package, "name") if isinstance(package, Mapping) else None


def _package_facets() -> dict[str, str]:
    """Each package name to its facet id, from the active snapshot."""
    try:
        active = registry.load_active_registry()
    except FacetError:
        return {}
    return {name: pkg.facet for name, pkg in active.packages.items()}


def _facet_of(facets: Mapping[str, str], package: str | None) -> str | None:
    """The facet id for ``package``, or ``None`` when the snapshot names none."""
    return facets.get(package) if package is not None else None


def _verified(stored: Mapping[str, object]) -> bool:
    """A record is verified only when its check passed."""
    verification = stored.get("verification")
    if not isinstance(verification, Mapping):
        return False
    return verification.get("status") == "passed"


def _duration(stored: Mapping[str, object]) -> float | None:
    properties = stored.get("properties")
    if not isinstance(properties, Mapping):
        return None
    value = properties.get("duration_s")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _integer(stored: Mapping[str, object], key: str) -> int | None:
    value = stored.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _text(source: Mapping[str, object], key: str) -> str | None:
    value = source.get(key)
    return value if isinstance(value, str) and value else None


def preview_state(asset_path: Path, preview_file: Path) -> str:
    """``fresh``, ``stale`` or ``missing`` for one asset's preview file."""
    try:
        asset_mtime = asset_path.stat().st_mtime
    except OSError as exc:
        raise FacetError("invalid_input", f"{asset_path}: cannot read asset") from exc
    try:
        preview_mtime = preview_file.stat().st_mtime
    except OSError:
        return MISSING
    return FRESH if preview_mtime >= asset_mtime else STALE


def load_preview(preview_file: Path) -> dict[str, object]:
    """The parsed preview file, which must be a JSON object."""
    try:
        text = preview_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise FacetError(
            "invalid_input", f"{preview_file}: the runner wrote no preview file"
        ) from exc
    try:
        loaded: object = json.loads(text)
    except ValueError as exc:
        raise FacetError("invalid_input", f"{preview_file}: the preview is not JSON") from exc
    if not isinstance(loaded, dict):
        raise FacetError("invalid_input", f"{preview_file}: the preview is not a JSON object")
    return {str(key): value for key, value in loaded.items()}


def preview_images(preview: Mapping[str, object], asset_dir: Path) -> list[Path]:
    """The preview's image files: absolute paths strictly inside ``asset_dir``.

    The ``images`` entries, every view's ``file`` and the document ``pages``
    count; anything outside the asset's directory, or not a file, is ignored.
    """
    names: list[str] = []
    raw_images = preview.get("images")
    if isinstance(raw_images, list):
        names.extend(item for item in raw_images if isinstance(item, str))
    raw_views = preview.get("views")
    if isinstance(raw_views, list):
        for view in raw_views:
            if isinstance(view, Mapping):
                file_value = view.get("file")
                if isinstance(file_value, str):
                    names.append(file_value)
    raw_pages = preview.get("pages")
    if isinstance(raw_pages, list):
        names.extend(item for item in raw_pages if isinstance(item, str))
    return _inside(names, asset_dir)


def _thumb(
    kind: str,
    state: str,
    path: Path,
    preview: Mapping[str, object] | None,
) -> Path | None:
    """The file the gallery shows as this card's thumbnail, if any."""
    if kind == "image":
        return path
    if state != FRESH or preview is None:
        return None
    images = preview_images(preview, path.parent)
    return images[0] if images else None


def served_names(preview: Mapping[str, object], asset_dir: Path) -> list[str]:
    """The preview's images as paths relative to ``asset_dir``, for the server.

    A page image may live one directory down (a deck's ``preview/page-01.png``),
    so the name the server serves keeps that subdirectory; one that cannot be
    made relative to the asset's directory is skipped.
    """
    try:
        base = asset_dir.resolve()
    except OSError:
        return []
    names: list[str] = []
    for path in preview_images(preview, asset_dir):
        try:
            names.append(path.relative_to(base).as_posix())
        except ValueError:
            continue
    return names


def _asset_names(
    asset_path: Path, preview_file: Path, preview: Mapping[str, object] | None
) -> frozenset[str]:
    """The paths the viewer may fetch from this asset's directory."""
    names = [asset_path.name, record.record_path(asset_path).name, preview_file.name]
    if preview is not None:
        names.extend(served_names(preview, asset_path.parent))
    return frozenset(names)


def _inside(names: list[str], asset_dir: Path) -> list[Path]:
    """The named files that resolve inside ``asset_dir``, deduplicated."""
    try:
        base = asset_dir.resolve()
    except OSError:
        return []
    found: list[Path] = []
    for name in names:
        candidate = _resolve_image(name, asset_dir, base)
        if candidate is not None and candidate not in found:
            found.append(candidate)
    return found


def _resolve_image(name: str, asset_dir: Path, base: Path) -> Path | None:
    """One image name as an absolute path, or ``None`` when unusable."""
    if not name or name.startswith("/") or ".." in Path(name).parts:
        return None
    try:
        resolved = (asset_dir / name).resolve()
        resolved.relative_to(base)
    except (OSError, ValueError):
        return None
    if not resolved.is_file():
        return None
    return resolved
