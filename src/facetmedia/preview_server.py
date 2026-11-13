"""Serve one asset's preview, or a whole folder's gallery, to a person.

Stdlib only. Every server binds 127.0.0.1 and serves read-only from exactly
three roots: the viewer's directory, the shared kit directory, and the
handful of allowed files beside the asset. Every other path is 404: there
is no directory listing, and a path that escapes its root (``..``, quoted
or not) is refused before the filesystem is touched.

The gallery server adds three routes over a folder's cards: the page under
``/gallery/``, the card document at ``/gallery.json``, and ``/thumb/<n>``.
``/open/<n>`` makes one card the server's current asset; ``/viewer/…`` and
``/asset/<name>`` then serve that asset exactly as the one-asset server
does. Opening another card replaces it, which is enough for one person on
one machine.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final, TypeAlias, cast
from urllib.parse import quote, unquote, urlsplit

from facetmedia import registry
from facetmedia.contract import FacetError
from facetmedia.preview_folder import FRESH, FolderAsset, gallery_data

__all__ = [
    "POLL_S",
    "GalleryServer",
    "GallerySite",
    "PreviewServer",
    "Site",
    "make_gallery_server",
    "make_server",
    "serve",
]

logger = logging.getLogger("facetmedia")

#: Seconds a serving loop waits for a request before re-checking idleness.
POLL_S: Final[float] = 0.1

_CONTENT_TYPES: Final[dict[str, str]] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".wav": "audio/wav",
    ".txt": "text/plain; charset=utf-8",
}
_DEFAULT_CONTENT_TYPE: Final[str] = "application/octet-stream"

_JSON_TYPE: Final[str] = "application/json"


@dataclass(frozen=True)
class Site:
    """The three roots one preview server may read.

    ``asset_names`` are the only file names served from ``asset_dir``: the
    asset, its record, its preview file and the preview's images.
    """

    viewer_root: Path
    kit_root: Path
    asset_dir: Path
    asset_names: frozenset[str]


@dataclass(frozen=True)
class GallerySite:
    """The roots and the cards one gallery server may read."""

    gallery_root: Path
    kit_root: Path
    folder_name: str
    assets: tuple[FolderAsset, ...]


class PreviewServer(ThreadingHTTPServer):
    """One preview's HTTP server, bound to 127.0.0.1, read-only."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], site: Site) -> None:
        self.site = site
        self.last_activity = time.monotonic()
        super().__init__(address, _PreviewHandler)


class GalleryServer(ThreadingHTTPServer):
    """One folder's gallery server, bound to 127.0.0.1, read-only.

    ``current`` is the asset the last ``/open/<n>`` selected, with the
    viewer directory resolved when it was opened; ``None`` until then.
    """

    daemon_threads = True

    def __init__(self, address: tuple[str, int], site: GallerySite) -> None:
        self.site = site
        self.current: tuple[FolderAsset, Path] | None = None
        self.last_activity = time.monotonic()
        super().__init__(address, _GalleryHandler)


def make_server(port: int | None, site: Site) -> PreviewServer:
    """Bind 127.0.0.1 on ``port``, or on a free port when ``None`` or 0."""
    return PreviewServer(("127.0.0.1", port or 0), site)


def make_gallery_server(port: int | None, site: GallerySite) -> GalleryServer:
    """Bind 127.0.0.1 on ``port``, or on a free port when ``None`` or 0."""
    return GalleryServer(("127.0.0.1", port or 0), site)


def serve(server: PreviewServer | GalleryServer, idle_timeout: float) -> None:
    """Serve until no request arrives for ``idle_timeout`` seconds.

    Polls every :data:`POLL_S` seconds so idleness is noticed promptly;
    always closes the server, so a finished loop holds no socket.
    """
    server.socket.settimeout(POLL_S)
    try:
        while time.monotonic() - server.last_activity < idle_timeout:
            server.handle_request()
    finally:
        server.server_close()


class _FileHandler(BaseHTTPRequestHandler):
    """The GET plumbing both servers share: read-only, no directory listing."""

    def _send(self, payload: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_file(self, target: Path) -> bool:
        """Send one file's bytes; ``False`` when it cannot be read."""
        try:
            payload = target.read_bytes()
        except OSError:
            return False
        self._send(payload, _content_type(target))
        return True

    def _not_found(self) -> None:
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "9")
        self.end_headers()
        self.wfile.write(b"not found")

    def log_message(self, format: str, *args: object) -> None:
        logger.debug("%s - %s", self.address_string(), format % args)


class _PreviewHandler(_FileHandler):
    """One read-only GET route per root; everything else is 404."""

    def do_GET(self) -> None:
        server = cast("PreviewServer", self.server)
        server.last_activity = time.monotonic()
        target = _resolve(server.site, urlsplit(self.path).path)
        if target is None or not self._send_file(target):
            self._not_found()


class _GalleryHandler(_FileHandler):
    """The gallery's routes, then the current asset's three roots."""

    def do_GET(self) -> None:
        server = cast("GalleryServer", self.server)
        server.last_activity = time.monotonic()
        decoded = unquote(urlsplit(self.path).path)
        if ".." in decoded.split("/") or not self._route(server, decoded):
            self._not_found()

    def _route(self, server: GalleryServer, decoded: str) -> bool:
        """Answer one path, or ``False`` so the caller sends 404."""
        if decoded == "/gallery.json":
            return self._json(gallery_data(server.site.assets, server.site.folder_name))
        head, rest = _split_route(decoded)
        route = _ROUTES.get(head)
        return route is not None and route(self, server, rest)

    def _json(self, payload: dict[str, object]) -> bool:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self._send(body, _JSON_TYPE)
        return True

    def _file(self, root: Path, relative: str) -> bool:
        target = _under(root, relative)
        return target is not None and self._send_file(target)

    def _gallery(self, server: GalleryServer, relative: str) -> bool:
        return self._file(server.site.gallery_root, relative)

    def _kit(self, server: GalleryServer, relative: str) -> bool:
        return self._file(server.site.kit_root, relative)

    def _thumb(self, server: GalleryServer, raw: str) -> bool:
        entry = _entry_at(server.site.assets, raw)
        if entry is None or entry.thumb is None:
            return False
        return self._send_file(entry.thumb)

    def _open(self, server: GalleryServer, raw: str) -> bool:
        """Make one fresh card current, or serve a document's own file."""
        entry = _entry_at(server.site.assets, raw)
        if entry is None or entry.state != FRESH:
            return False
        if entry.kind == "document":
            return self._send_file(entry.path)
        viewer_root = _viewer_root(entry.capability)
        if viewer_root is None:
            return False
        server.current = (entry, viewer_root)
        self._redirect(f"/viewer/index.html?asset={quote(entry.path.name)}")
        return True

    def _viewer(self, server: GalleryServer, relative: str) -> bool:
        if server.current is None:
            return False
        return self._file(server.current[1], relative)

    def _asset(self, server: GalleryServer, name: str) -> bool:
        if server.current is None:
            return False
        entry = server.current[0]
        if not name or "/" in name or name not in entry.asset_names:
            return False
        return self._file(entry.path.parent, name)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()


#: One gallery route: the handler plus the relative path after its prefix.
_Route: TypeAlias = Callable[["_GalleryHandler", "GalleryServer", str], bool]

_ROUTES: Final[dict[str, _Route]] = {
    "open": _GalleryHandler._open,
    "thumb": _GalleryHandler._thumb,
    "gallery": _GalleryHandler._gallery,
    "kit": _GalleryHandler._kit,
    "viewer": _GalleryHandler._viewer,
    "asset": _GalleryHandler._asset,
}


def _split_route(decoded: str) -> tuple[str, str]:
    """``/open/0`` -> ``("open", "0")``; a path without a second slash is no route."""
    head, slash, rest = decoded.removeprefix("/").partition("/")
    return (head, rest) if slash else ("", decoded)


def _entry_at(assets: tuple[FolderAsset, ...], raw: str) -> FolderAsset | None:
    """The card number ``raw`` names, or ``None`` when it is out of range."""
    if not (raw.isascii() and raw.isdigit()):
        return None
    index = int(raw)
    return assets[index] if index < len(assets) else None


def _viewer_root(capability_id: str) -> Path | None:
    """The viewer directory for one capability, with the ``generic`` fallback."""
    viewers = registry.registry_dir() / "viewers"
    try:
        capability = registry.load_active_registry().capability(capability_id)
    except FacetError:
        return None
    root = viewers / (capability.viewer or "generic")
    if not (root / "index.html").is_file():
        root = viewers / "generic"
    return root if (root / "index.html").is_file() else None


def _resolve(site: Site, raw_path: str) -> Path | None:
    """The file ``raw_path`` names, or ``None`` when it is not served."""
    decoded = unquote(raw_path)
    if ".." in decoded.split("/"):
        return None
    if decoded.startswith("/viewer/"):
        return _under(site.viewer_root, decoded[len("/viewer/") :])
    if decoded.startswith("/kit/"):
        return _under(site.kit_root, decoded[len("/kit/") :])
    if decoded.startswith("/asset/"):
        name = decoded[len("/asset/") :]
        if not name or "/" in name or name not in site.asset_names:
            return None
        return _under(site.asset_dir, name)
    return None


def _under(root: Path, relative: str) -> Path | None:
    """``root/relative`` when it stays inside ``root`` and is a file."""
    if not relative or relative.startswith("/"):
        return None
    candidate = root / relative
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    if not resolved.is_file():
        return None
    return resolved


def _content_type(path: Path) -> str:
    """The served content type by file suffix; anything else is bytes."""
    return _CONTENT_TYPES.get(path.suffix.lower(), _DEFAULT_CONTENT_TYPE)
