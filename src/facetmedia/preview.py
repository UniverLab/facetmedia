"""``facet preview``: show one asset through its viewer, or a folder as a gallery.

An agent asks with ``--json`` and gets the asset's record, its parsed
``<asset>.preview.json`` and the preview's image paths; nothing is opened
and no server starts. A person gets the capability's viewer (a static web
app from the registry's ``viewers/`` directory), served by facet on
127.0.0.1 and opened in the system browser.

A folder — or no argument, meaning the current directory — is previewed as
one gallery over every asset below it that has a sibling record. Folder
mode never runs a runner: an asset whose preview is fresh is shown without
its package, and one whose preview must be rebuilt is a card that names
the command which fixes it.

The preview file is written by the package runner's ``preview`` verb and
refreshed whenever it is missing or older than the asset. The core carries
no HTML of its own.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final
from urllib.parse import quote

from facetmedia import env, record, registry, runner
from facetmedia.contract import FacetError
from facetmedia.home import Home
from facetmedia.preview_folder import (
    FRESH,
    KINDS,
    PREVIEW_SUFFIX,
    FolderAsset,
    collect,
    entry_data,
    gallery_data,
    load_preview,
    preview_images,
    preview_state,
    text_table,
)
from facetmedia.preview_server import (
    GallerySite,
    Site,
    make_gallery_server,
    make_server,
    serve,
)
from facetmedia.registry import Capability, Package

__all__ = [
    "KINDS",
    "PREVIEW_SUFFIX",
    "execute",
    "open_url",
    "opener_argv",
]

logger = logging.getLogger("facetmedia")

#: How many page images ``--json`` may carry before it truncates documents.
PAGE_LIMIT: Final[int] = 10

#: The preview's image files, kept as ``_images`` for this module's callers.
_images = preview_images
#: The parsed preview file, kept as ``_load_preview`` for this module's callers.
_load_preview = load_preview


def execute(args: argparse.Namespace, run_id: str) -> tuple[dict[str, object], int]:
    """Preview one asset or a folder; returns the ``--json`` data and the exit code."""
    target = getattr(args, "asset", None)
    if target is None:
        return _folder_mode(args, Path.cwd())
    given = Path(target)
    if given.is_dir():
        return _folder_mode(args, Path(os.path.abspath(given)))
    return _single_mode(args, run_id)


def _single_mode(args: argparse.Namespace, run_id: str) -> tuple[dict[str, object], int]:
    """One asset: the fresh preview on disk, or a rebuilt one; then show it."""
    home = Home.from_env()
    asset_path = _asset_path(args.asset)
    stored = record.read_record(args.asset)
    capability_id = _capability_of(stored, args.asset)
    preview_file = asset_path.parent / (asset_path.name + PREVIEW_SUFFIX)
    if preview_state(asset_path, preview_file) == FRESH:
        preview_data = _load_preview(preview_file)
    else:
        preview_data = _rebuilt_preview(
            home, run_id, capability_id, stored, asset_path, preview_file
        )
    images = _images(preview_data, asset_path.parent)
    _mark_truncated(preview_data)
    shown = images[:PAGE_LIMIT] if isinstance(preview_data.get("pages"), list) else images
    data: dict[str, object] = {
        "asset": _relative_asset(asset_path),
        "record": stored,
        "preview": preview_data,
        "images": [str(path) for path in shown],
    }
    if bool(getattr(args, "json", False)):
        return data, 0
    _show(args, preview_data, _capability(capability_id), asset_path, preview_file, images)
    return data, 0


def _rebuilt_preview(
    home: Home,
    run_id: str,
    capability_id: str,
    stored: Mapping[str, object],
    asset_path: Path,
    preview_file: Path,
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
    return _ensure_preview(package, home, run_id, capability_id, params, asset_path, preview_file)


def _folder_mode(args: argparse.Namespace, folder: Path) -> tuple[dict[str, object], int]:
    """Every recorded asset below ``folder``, as data, a table or a gallery."""
    kinds = _kind_filter(getattr(args, "kind", None))
    entries = collect(folder, kinds)
    name = folder.name
    if bool(getattr(args, "json", False)):
        return {"folder": name, "assets": [entry_data(entry) for entry in entries]}, 0
    gallery_root = registry.registry_dir() / "viewers" / "gallery"
    if not (gallery_root / "index.html").is_file():
        print(text_table(entries, name))
        return gallery_data(entries, name), 0
    if not _is_tty():
        print(json.dumps(gallery_data(entries, name), indent=2, sort_keys=True))
        return gallery_data(entries, name), 0
    _serve_gallery(args, entries, gallery_root, name)
    return gallery_data(entries, name), 0


def _kind_filter(kind: object) -> frozenset[str] | None:
    """The ``--kind`` value as a one-element filter, or ``None`` for every kind."""
    return frozenset({str(kind)}) if isinstance(kind, str) and kind else None


def _is_tty() -> bool:
    """Whether a person is watching stdout."""
    return sys.stdout.isatty()


def _serve_gallery(
    args: argparse.Namespace,
    entries: tuple[FolderAsset, ...],
    gallery_root: Path,
    folder_name: str,
) -> None:
    """Serve the cards and their assets on 127.0.0.1 until Ctrl+C or idle."""
    site = GallerySite(
        gallery_root=gallery_root,
        kit_root=gallery_root.parent / "_kit",
        folder_name=folder_name,
        assets=entries,
    )
    try:
        server = make_gallery_server(getattr(args, "port", None), site)
    except OSError as exc:
        raise FacetError(
            "invalid_input", f"cannot serve the preview: {exc}", details={"port": args.port}
        ) from exc
    url = f"http://127.0.0.1:{server.server_address[1]}/gallery/index.html"
    print(url, flush=True)
    if not bool(getattr(args, "no_open", False)):
        open_url(url)
    with contextlib.suppress(KeyboardInterrupt):
        serve(server, float(getattr(args, "idle_timeout", 600.0)))


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


def _capability(capability_id: str) -> Capability:
    """The capability alone: showing a fresh preview needs no package."""
    return registry.load_active_registry().capability(capability_id)


def _capability_of(stored: Mapping[str, object], asset: str) -> str:
    """The capability id the record's provenance names."""
    provenance = stored.get("provenance")
    if not isinstance(provenance, dict):
        raise FacetError("invalid_input", f"{asset}: the record has no provenance")
    capability = provenance.get("capability")
    if not isinstance(capability, str) or not capability:
        raise FacetError("invalid_input", f"{asset}: the record names no capability")
    return capability


def _params_of(stored: Mapping[str, object], asset: str) -> dict[str, object]:
    """The record's params for the runner's ``preview`` verb."""
    provenance = stored.get("provenance")
    if not isinstance(provenance, dict):
        raise FacetError("invalid_input", f"{asset}: the record has no provenance")
    params = provenance.get("params")
    if not isinstance(params, dict):
        raise FacetError("invalid_input", f"{asset}: the record has no params")
    return {str(key): value for key, value in params.items()}


def _asset_path(asset: str) -> Path:
    """The asset an argument names, whether it is the asset or its record."""
    given = Path(asset)
    if given.name.endswith(record.RECORD_SUFFIX):
        given = Path(str(given)[: -len(record.RECORD_SUFFIX)])
    if not given.is_absolute():
        given = Path.cwd() / given
    return given


def _relative_asset(asset_path: Path) -> str:
    """The asset's path relative to the working directory, never absolute."""
    return Path(os.path.relpath(asset_path, Path.cwd())).as_posix()


def _ensure_preview(
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
        return _load_preview(preview_file)
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
        preview_data = _load_preview(preview_file)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return preview_data


def _mark_truncated(preview: dict[str, object]) -> None:
    """Flag a document preview whose pages exceed the ``--json`` page limit."""
    raw_pages = preview.get("pages")
    if not isinstance(raw_pages, list):
        return
    count: object = preview.get("page_count")
    if not isinstance(count, int):
        count = len(raw_pages)
    preview["truncated"] = bool(count > PAGE_LIMIT)


def _show(
    args: argparse.Namespace,
    preview_data: dict[str, object],
    capability: Capability,
    asset_path: Path,
    preview_file: Path,
    images: list[Path],
) -> None:
    """Show the asset to a person.

    A document opens the PDF itself in the system browser; every other
    kind gets the capability's viewer, served by facet.
    """
    if capability.kind == "document":
        _show_document(args, asset_path)
        return
    viewer_root = registry.registry_dir() / "viewers" / (capability.viewer or "generic")
    if not (viewer_root / "index.html").is_file():
        viewer_root = registry.registry_dir() / "viewers" / "generic"
    if not (viewer_root / "index.html").is_file():
        print(json.dumps(preview_data, indent=2, sort_keys=True))
        return
    site = Site(
        viewer_root=viewer_root,
        kit_root=registry.registry_dir() / "viewers" / "_kit",
        asset_dir=asset_path.parent,
        asset_names=_asset_names(asset_path, preview_file, images),
    )
    try:
        server = make_server(getattr(args, "port", None), site)
    except OSError as exc:
        raise FacetError(
            "invalid_input", f"cannot serve the preview: {exc}", details={"port": args.port}
        ) from exc
    url = (
        f"http://127.0.0.1:{server.server_address[1]}"
        f"/viewer/index.html?asset={quote(asset_path.name)}"
    )
    print(url, flush=True)
    if not bool(getattr(args, "no_open", False)):
        open_url(url)
    with contextlib.suppress(KeyboardInterrupt):
        serve(server, float(getattr(args, "idle_timeout", 600.0)))


def _show_document(args: argparse.Namespace, asset_path: Path) -> None:
    """Open the document asset itself in the system browser, not a viewer.

    The PDF is the person's preview: the URL is on stdout even when no
    opener can start.
    """
    url = asset_path.resolve().as_uri()
    print(url, flush=True)
    if not bool(getattr(args, "no_open", False)):
        open_url(url)


def _asset_names(asset_path: Path, preview_file: Path, images: list[Path]) -> frozenset[str]:
    """The file names the viewer may fetch from the asset's directory."""
    names = [asset_path.name, record.record_path(asset_path).name, preview_file.name]
    names.extend(path.name for path in images)
    return frozenset(names)


def _wsl_interop() -> bool:
    """Whether the WSL interop device exists (we run inside WSL)."""
    return Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists()


def _proc_version() -> str:
    """The kernel version line, or ``""`` where ``/proc`` is absent."""
    try:
        return Path("/proc/version").read_text(encoding="utf-8")
    except OSError:
        return ""


def _is_wsl() -> bool:
    """Whether facet runs inside WSL (interop device or Microsoft kernel)."""
    return _wsl_interop() or "microsoft" in _proc_version().lower()


def opener_argv(url: str) -> list[str]:
    """The command opening ``url`` on this platform.

    WSL reaches its own localhost from Windows, so it opens through
    ``wslview`` when present and ``explorer.exe`` otherwise.
    """
    if _is_wsl():
        if shutil.which("wslview") is not None:
            return ["wslview", url]
        return ["explorer.exe", url]
    if sys.platform == "darwin":
        return ["open", url]
    return ["xdg-open", url]


def open_url(url: str) -> None:
    """Open ``url`` in the system browser; a missing opener keeps the URL.

    The URL is already on stdout, so a browser that cannot start is a
    debug-log line, never a failure.
    """
    try:
        subprocess.Popen(
            opener_argv(url),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        logger.debug("could not open the preview url", exc_info=True)
