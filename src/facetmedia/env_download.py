"""The download primitives every facet fetch shares.

``facetmedia.env`` keeps the engine logic; the streaming, retry and
``file://`` handling live here so ``env`` stays inside the lab's per-file
size cap, and so engine siblings (``env_binary``) download the same way:
three attempts with 2 s / 8 s backoff, a ``.part`` file that a failed
attempt deletes, and the sha256 computed while streaming.
"""

from __future__ import annotations

import hashlib
import secrets
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Final

from facetmedia import __version__
from facetmedia.contract import FacetError
from facetmedia.home import Home

CHUNK_BYTES: Final[int] = 1024 * 1024
TIMEOUT_S: Final[float] = 60.0
USER_AGENT: Final[str] = f"facetmedia/{__version__}"
#: Backoff between the three download attempts.
BACKOFF_S: Final[tuple[float, ...]] = (2.0, 8.0)
ATTEMPTS: Final[int] = len(BACKOFF_S) + 1


def _part_path(home: Home, model_id: str) -> Path:
    return home.tmp_dir / f"{model_id}.{secrets.token_hex(4)}.part"


def _download_with_retries(part: Path, action: Callable[[], tuple[str, int]]) -> tuple[str, int]:
    """Three attempts with 2 s / 8 s backoff; a failed attempt leaves no part.

    ``urllib.error.URLError`` and ``HTTPError`` are ``OSError`` subclasses, so
    the network failures to retry are exactly ``OSError``.
    """
    last: OSError | None = None
    for attempt in range(ATTEMPTS):
        try:
            return action()
        except OSError as exc:
            last = exc
            part.unlink(missing_ok=True)
            if attempt < ATTEMPTS - 1:
                time.sleep(BACKOFF_S[attempt])
    raise FacetError(
        "download_failed",
        f"download failed after {ATTEMPTS} attempts: {last}",
        details={"error": str(last)},
    ) from last


def _stream_to_part(url: str, part: Path) -> tuple[str, int]:
    """Stream ``url`` into ``part`` while hashing; returns (sha256, bytes)."""
    digest = hashlib.sha256()
    size = 0
    with _opened(url) as source, part.open("wb") as sink:
        while chunk := source.read(CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
            sink.write(chunk)
    return digest.hexdigest(), size


@contextmanager
def _opened(url: str) -> Iterator[IO[bytes]]:
    """Open ``url`` for reading: ``file://`` is a path relative to the cwd."""
    if url.startswith("file://"):
        path = Path(urllib.parse.unquote(url.removeprefix("file://")))
        if not path.is_absolute():
            path = Path.cwd() / path
        with path.open("rb") as handle:
            yield handle
        return
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        yield response
