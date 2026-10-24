"""Serve one asset's preview to a person through its registry viewer.

Stdlib only. The server binds 127.0.0.1 and serves read-only from exactly
three roots: the viewer's directory, the shared kit directory, and the
handful of allowed files beside the asset. Every other path is 404: there
is no directory listing, and a path that escapes its root (``..``, quoted
or not) is refused before the filesystem is touched.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final, cast
from urllib.parse import unquote, urlsplit

__all__ = ["POLL_S", "Site", "PreviewServer", "make_server", "serve"]

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
    ".wav": "audio/wav",
    ".txt": "text/plain; charset=utf-8",
}
_DEFAULT_CONTENT_TYPE: Final[str] = "application/octet-stream"


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


class PreviewServer(ThreadingHTTPServer):
    """One preview's HTTP server, bound to 127.0.0.1, read-only."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], site: Site) -> None:
        self.site = site
        self.last_activity = time.monotonic()
        super().__init__(address, _PreviewHandler)


def make_server(port: int | None, site: Site) -> PreviewServer:
    """Bind 127.0.0.1 on ``port``, or on a free port when ``None`` or 0."""
    return PreviewServer(("127.0.0.1", port or 0), site)


def serve(server: PreviewServer, idle_timeout: float) -> None:
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


class _PreviewHandler(BaseHTTPRequestHandler):
    """One read-only GET route per root; everything else is 404."""

    def do_GET(self) -> None:
        cast("PreviewServer", self.server).last_activity = time.monotonic()
        site = cast("PreviewServer", self.server).site
        target = _resolve(site, urlsplit(self.path).path)
        if target is None:
            self._not_found()
            return
        try:
            payload = target.read_bytes()
        except OSError:
            self._not_found()
            return
        self._send(payload, _content_type(target))

    def _send(self, payload: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _not_found(self) -> None:
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "9")
        self.end_headers()
        self.wfile.write(b"not found")

    def log_message(self, format: str, *args: object) -> None:
        logger.debug("%s - %s", self.address_string(), format % args)


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
