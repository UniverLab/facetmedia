"""A local stand-in for the two GitHub endpoints ``facet update`` calls.

The tests never touch the network: ``FakeGitHub`` serves the commit lookup and
the ``tar.gz`` archive from 127.0.0.1, and the caller points
``FACET_GITHUB_API`` and ``FACET_GITHUB_CODELOAD`` at it.

The archive is built from ``tests/fixtures/registry`` at test time, so it
always matches the registry checkouts the rest of the suite uses.
"""

from __future__ import annotations

import io
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final

import pytest

from facetmedia.update import REPO

FIXTURE_ROOT: Final[Path] = Path(__file__).parent / "fixtures" / "registry"
COMMIT: Final[str] = "0123456789abcdef0123456789abcdef01234567"
OTHER_COMMIT: Final[str] = "fedcba9876543210fedcba9876543210fedcba98"
TOP: Final[str] = "facetmedia-registry-0123456"
#: Port 1 on loopback has no listener, so a request is refused immediately.
DEAD_BASE: Final[str] = "http://127.0.0.1:1"

API_ROUTE: Final[str] = f"/repos/{REPO}/commits/"
ARCHIVE_ROUTE: Final[str] = f"/{REPO}/tar.gz/"


def _add_file(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def _add_symlink(tar: tarfile.TarFile, name: str, target: str) -> None:
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    info.mode = 0o777
    tar.addfile(info)


def build_archive(source: Path = FIXTURE_ROOT, top: str = TOP) -> bytes:
    """A ``tar.gz`` of ``source`` under one top directory, like codeload's."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in sorted(source.rglob("*")):
            tar.add(
                path,
                arcname=f"{top}/{path.relative_to(source).as_posix()}",
                recursive=False,
            )
    return buffer.getvalue()


def archive_with_member(name: str, top: str = TOP) -> bytes:
    """An archive that carries a registry index plus one crafted member."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        _add_file(tar, f"{top}/index.toml", b"version = 1\n")
        _add_file(tar, name, b"evil\n")
    return buffer.getvalue()


def archive_with_symlink(top: str = TOP) -> bytes:
    """An archive whose second member is a symlink to ``/etc/passwd``."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        _add_file(tar, f"{top}/index.toml", b"version = 1\n")
        _add_symlink(tar, f"{top}/link", "/etc/passwd")
    return buffer.getvalue()


def archive_without_top() -> bytes:
    """An archive whose members sit at the root instead of one directory."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        _add_file(tar, "index.toml", b"version = 1\n")
        _add_file(tar, "README.md", b"no top directory\n")
    return buffer.getvalue()


def archive_with_file_top(top: str = TOP) -> bytes:
    """An archive whose single top entry is a file, not a directory."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        _add_file(tar, top, b"not a directory\n")
    return buffer.getvalue()


def dead_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point both GitHub bases at a closed port: every fetch is refused."""
    monkeypatch.setenv("FACET_GITHUB_API", DEAD_BASE)
    monkeypatch.setenv("FACET_GITHUB_CODELOAD", DEAD_BASE)


class FakeGitHub:
    """The commit endpoint and the archive endpoint, with request counters."""

    def __init__(self, commit: str = COMMIT, archive: bytes | None = None) -> None:
        self.commit = commit
        self.archive_commit = COMMIT
        self.archive = build_archive() if archive is None else archive
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.api_hits = 0
        self.archive_hits = 0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> FakeGitHub:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                server.serve(self)

            def log_message(self, format: str, *args: object) -> None:
                return

        return Handler

    def serve(self, handler: BaseHTTPRequestHandler) -> None:
        """Answer one GET, counting it and recording its headers."""
        path = handler.path
        headers = {name.lower(): value for name, value in handler.headers.items()}
        self.requests.append((path, headers))
        if path.startswith(API_ROUTE):
            self.api_hits += 1
            self._reply(handler, 200, self.commit.encode("ascii"), "text/plain")
            return
        if path.startswith(ARCHIVE_ROUTE):
            self.archive_hits += 1
            if path.removeprefix(ARCHIVE_ROUTE) == self.archive_commit:
                self._reply(handler, 200, self.archive, "application/gzip")
                return
        self._reply(handler, 404, b"not found", "text/plain")

    def _reply(
        self,
        handler: BaseHTTPRequestHandler,
        status: int,
        body: bytes,
        content_type: str,
    ) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
