"""``facet preview``: show one asset through its viewer, or to an agent as data.

An agent asks with ``--json`` and gets the asset's record, its parsed
``<asset>.preview.json`` and the preview's image paths; nothing is opened
and no server starts. A person gets the capability's viewer (a static web
app from the registry's ``viewers/`` directory), served by facet on
127.0.0.1 and opened in the system browser.

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
from facetmedia.preview_server import Site, make_server, serve
from facetmedia.registry import Capability, Package

__all__ = [
    "PREVIEW_SUFFIX",
    "execute",
    "open_url",
    "opener_argv",
]

logger = logging.getLogger("facetmedia")

#: ``echo.txt`` -> ``echo.txt.preview.json``, beside the asset.
PREVIEW_SUFFIX: Final[str] = ".preview.json"


def execute(args: argparse.Namespace, run_id: str) -> tuple[dict[str, object], int]:
    """Preview one asset; returns the ``--json`` data and the exit code."""
    home = Home.from_env()
    stored = record.read_record(args.asset)
    capability_id = _capability_of(stored, args.asset)
    capability, package = _resolve(capability_id)
    if not env.is_installed(package, home):
        raise FacetError(
            "not_installed",
            f"{package.name} {package.version} is not installed",
            hint=f"facet pull {package.name}",
            details={"package": package.name, "version": package.version},
        )
    asset_path = _asset_path(args.asset)
    params = _params_of(stored, args.asset)
    preview_file = asset_path.parent / (asset_path.name + PREVIEW_SUFFIX)
    preview_data = _ensure_preview(
        package, home, run_id, capability_id, params, asset_path, preview_file
    )
    images = _images(preview_data, asset_path.parent)
    data: dict[str, object] = {
        "asset": _relative_asset(asset_path),
        "record": stored,
        "preview": preview_data,
        "images": [str(path) for path in images],
    }
    if bool(getattr(args, "json", False)):
        return data, 0
    _show(args, preview_data, capability, asset_path, preview_file, images)
    return data, 0


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
            package, home, home.env_dir(package.name, package.version), temp_dir, run_id, None
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


def _load_preview(preview_file: Path) -> dict[str, object]:
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


def _images(preview: Mapping[str, object], asset_dir: Path) -> list[Path]:
    """The preview's image files: absolute paths strictly inside ``asset_dir``.

    Both ``images`` entries and every view's ``file`` count; anything
    outside the asset's directory, or not a file, is ignored.
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
    return _inside(names, asset_dir)


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


def _show(
    args: argparse.Namespace,
    preview_data: dict[str, object],
    capability: Capability,
    asset_path: Path,
    preview_file: Path,
    images: list[Path],
) -> None:
    """Serve the capability's viewer for the asset until it idles out."""
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
