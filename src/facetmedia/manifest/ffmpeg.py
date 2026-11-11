"""Read and validate a package's ``[ffmpeg]`` table.

``facetmedia.manifest`` keeps the package grammar; the ffmpeg grammar lives
here so the manifest module stays inside the lab's per-file size cap, the
same split ``facetmedia.manifest.binary`` makes for ``[engine.binary]``.
The loader passed in is :class:`facetmedia.manifest.RegistryLoader`, which
collects every problem instead of stopping at the first one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from facetmedia.registry_types import FFmpeg

if TYPE_CHECKING:
    from facetmedia.manifest import RegistryLoader

FFMPEG_KEYS: Final[frozenset[str]] = frozenset({"version", "url", "sha256"})
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}\Z")


def read_ffmpeg(
    loader: RegistryLoader,
    data: Mapping[str, Any],
    file: str,
) -> FFmpeg | None:
    """The optional ``[ffmpeg]`` table: one shared static FFmpeg build."""
    before = len(loader.problems)
    table = loader.table(data, "ffmpeg", file, "", required=False)
    if table is None:
        return None
    loader.unknown(table, FFMPEG_KEYS, file, "ffmpeg.")
    version = loader.string(table, "version", file, "ffmpeg.", required=True)
    url = loader.string(table, "url", file, "ffmpeg.", required=True)
    if url and not url.startswith("https://"):
        loader.add(file, "ffmpeg.url", "value")
    sha256 = loader.string(table, "sha256", file, "ffmpeg.", required=True)
    if sha256 and not _SHA256_RE.fullmatch(sha256):
        loader.add(file, "ffmpeg.sha256", "value")
    if len(loader.problems) != before:
        return None
    return FFmpeg(
        version=version or "",
        url=url or "",
        sha256=sha256 or "",
    )
