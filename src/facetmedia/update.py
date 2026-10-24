"""Download the package registry at one pinned commit.

The registry is code that arrives from the network, so ``facet update`` pins
it: the reference is resolved to a commit, the archive for exactly that commit
is downloaded, and it is kept as an immutable snapshot under
``<home>/registry/<commit>/``.  Everything facet does afterwards reads that
snapshot, offline, and asset records can name the commit they came from.

``FACET_GITHUB_API`` and ``FACET_GITHUB_CODELOAD`` replace the two GitHub base
URLs.  They exist for the tests and for nothing else.
"""

from __future__ import annotations

import contextlib
import http.client
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from typing import Final

from facetmedia import __version__
from facetmedia.contract import FacetError
from facetmedia.home import Home, atomic_write_json, read_json
from facetmedia.registry import load_registry

__all__ = [
    "COMPLETE",
    "DEFAULT_REF",
    "SOURCE",
    "default_ref",
    "download_archive",
    "extract_snapshot",
    "install_snapshot",
    "resolve_ref",
    "update_registry",
]

REPO: Final[str] = "UniverLab/facetmedia-registry"
SOURCE: Final[str] = f"https://github.com/{REPO}"
DEFAULT_REF: Final[str] = "main"
API_BASE: Final[str] = "https://api.github.com"
CODELOAD_BASE: Final[str] = "https://codeload.github.com"
COMPLETE: Final[str] = ".complete"
COMMIT_HEX_LEN: Final[int] = 40
TIMEOUT_S: Final[float] = 60.0
ARCHIVE_SUFFIX: Final[str] = ".tar.gz"
HINT: Final[str] = "run facet update when online"
#: GitHub's status for a quota that is already spent (429 has no Remaining header).
RATE_LIMITED_STATUS: Final[int] = 429


def default_ref() -> str:
    """The reference ``facet update`` resolves when ``--ref`` is not given."""
    return os.environ.get("FACET_REGISTRY_REF") or DEFAULT_REF


def resolve_ref(ref: str) -> str:
    """Resolve a branch, tag, or 40-hex commit to the commit it names."""
    url = f"{_base('FACET_GITHUB_API', API_BASE)}/repos/{REPO}/commits/{_quote(ref)}"
    answer = _get(url, accept="application/vnd.github.sha").decode("utf-8", "replace").strip()
    if not is_commit(answer):
        raise FacetError(
            "download_failed",
            f"{ref}: GitHub returned no commit",
            hint=HINT,
            details={"ref": ref, "answer": answer[:80]},
        )
    return answer.lower()


def download_archive(home: Home, commit: str) -> Path:
    """Download the archive of exactly ``commit`` into ``<home>/tmp``.

    The tarball is read from disk rather than memory, and the caller removes it
    once the snapshot is installed or refused.
    """
    url = f"{_base('FACET_GITHUB_CODELOAD', CODELOAD_BASE)}/{REPO}/tar.gz/{commit}"
    target = home.tmp_dir / f"registry-{commit}{ARCHIVE_SUFFIX}"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _opened(url, None) as response, target.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def extract_snapshot(archive: Path, dest: Path) -> Path:
    """Extract ``archive`` into ``dest`` and return its stripped root.

    Every member is refused unless it is a plain file or directory inside the
    archive's single top directory: no absolute names, no ``..``, no links, no
    devices.  ``filter="data"`` applies the same rule a second time while
    extracting, so a crafted archive can never write outside ``dest``.
    """
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(name=str(archive), mode="r:gz") as tar:
            return _extract_members(tar, dest)
    except tarfile.TarError as exc:
        raise FacetError(
            "download_failed", f"registry archive is not a tar.gz: {exc}", hint=HINT
        ) from exc


def install_snapshot(home: Home, commit: str, archive: Path) -> Path:
    """Install ``archive`` as ``<home>/registry/<commit>/``, validated.

    The archive is unpacked in ``<home>/tmp``; its top directory is renamed to
    ``<commit>.partial``, validated, marked ``.complete``, and only then
    renamed to ``<commit>/``.  A snapshot that does not validate leaves
    nothing behind.
    """
    home.ensure()
    scratch = Path(tempfile.mkdtemp(dir=str(home.tmp_dir), prefix=f"registry-{commit[:7]}-"))
    partial = home.registry_dir(f"{commit}.partial")
    try:
        top = extract_snapshot(archive, scratch)
        _discard(partial)
        os.replace(top, partial)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    _validate(partial, commit)
    (partial / COMPLETE).write_text(f"{commit}\n", encoding="utf-8")
    final = home.registry_dir(commit)
    _discard(final)
    os.replace(partial, final)
    return final


def update_registry(home: Home, ref: str | None = None) -> dict[str, object]:
    """Pin ``ref``, install its snapshot when needed, and record it in state.

    The state file is written last, so a download or validation failure leaves
    the previous snapshot in use.
    """
    home.ensure()
    pinned = ref or default_ref()
    state_file = home.state_file("registry.json")
    previous = _previous_commit(state_file)
    commit = resolve_ref(pinned)
    snapshot = home.registry_dir(commit)
    if not (snapshot / COMPLETE).is_file():
        archive = download_archive(home, commit)
        try:
            install_snapshot(home, commit, archive)
        finally:
            archive.unlink(missing_ok=True)
    packages = _package_records(home, snapshot, previous)
    atomic_write_json(
        state_file,
        {
            "commit": commit,
            "ref": pinned,
            "fetched_at": _now(),
            "source": SOURCE,
        },
        home.tmp_dir,
    )
    return {
        "commit": commit,
        "ref": pinned,
        "previous_commit": previous,
        "packages": packages,
    }


def is_commit(text: str) -> bool:
    """Whether ``text`` is a 40-character hexadecimal commit."""
    return len(text) == COMMIT_HEX_LEN and all(char in "0123456789abcdefABCDEF" for char in text)


def _base(variable: str, fallback: str) -> str:
    """A GitHub base URL, overridable by a test-only environment variable."""
    return (os.environ.get(variable) or fallback).rstrip("/")


def _quote(ref: str) -> str:
    """``ref`` for a URL path, keeping the ``/`` of a branch name."""
    return urllib.parse.quote(ref, safe="/")


def _headers(accept: str | None) -> dict[str, str]:
    headers = {"User-Agent": f"facetmedia/{__version__}"}
    if accept is not None:
        headers["Accept"] = accept
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get(url: str, accept: str | None = None) -> bytes:
    """Fetch ``url`` and return its body."""
    with _opened(url, accept) as response:
        return bytes(response.read())


@contextlib.contextmanager
def _opened(url: str, accept: str | None) -> Iterator[http.client.HTTPResponse]:
    """Open ``url``, turning every network failure into ``download_failed``."""
    request = urllib.request.Request(url, headers=_headers(accept))
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            yield response
    except urllib.error.HTTPError as exc:
        rate_limited = _rate_limit_error(url, exc)
        if rate_limited is not None:
            raise rate_limited from exc
        raise _failed(url, f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise _failed(url, str(reason)) from exc


def _failed(url: str, why: str) -> FacetError:
    return FacetError("download_failed", f"{url}: {why}", hint=HINT, details={"url": url})


def _rate_limit_error(url: str, exc: urllib.error.HTTPError) -> FacetError | None:
    """The GitHub rate-limit failure for ``exc``, or ``None`` for any other HTTP error."""
    if exc.code not in (403, 429):
        return None
    headers: Message | None = getattr(exc, "headers", None)
    remaining = headers.get("X-RateLimit-Remaining") if headers is not None else None
    if exc.code != RATE_LIMITED_STATUS and (remaining is None or remaining.strip() != "0"):
        return None
    raw = headers.get("X-RateLimit-Reset") if headers is not None else None
    reset: str | None = None
    if raw is not None:
        try:
            reset = datetime.fromtimestamp(int(raw), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, OverflowError, OSError):
            reset = None  # an unreadable reset is reported as null
    message = f"{url}: GitHub rate limit reached"
    if reset is not None:
        message = f"{message} (retry after {reset})"
    hint = (
        "retry after the reset"
        if os.environ.get("GITHUB_TOKEN")
        else "set GITHUB_TOKEN to a GitHub token to lift the limit, or retry after the reset"
    )
    return FacetError(
        "download_failed",
        message,
        hint=hint,
        details={"url": url, "rate_limited": True, "reset": reset},
    )


def _refuse_member(member: tarfile.TarInfo) -> None:
    """Refuse a member that could escape ``dest`` or is not plain data."""
    name = member.name
    if name.startswith("/") or ".." in Path(name).parts:
        raise FacetError(
            "download_failed",
            f"refused archive member: {name}",
            hint=HINT,
            details={"member": name},
        )
    if member.issym() or member.islnk() or member.isdev():
        raise FacetError(
            "download_failed",
            f"refused archive member: {name} is a link or device",
            hint=HINT,
            details={"member": name},
        )


def _top_dir(members: list[tarfile.TarInfo]) -> str:
    """The archive's single top directory, the one GitHub wraps it in."""
    tops = {
        member.name.split("/", 1)[0]
        for member in members
        if member.name and member.name != "pax_global_header"
    }
    if len(tops) != 1:
        raise FacetError(
            "download_failed",
            "registry archive does not have a single top directory",
            hint=HINT,
            details={"top": sorted(tops)[:5]},
        )
    return tops.pop()


def _extract_members(tar: tarfile.TarFile, dest: Path) -> Path:
    members = tar.getmembers()
    for member in members:
        _refuse_member(member)
    top = _top_dir(members)
    _extractall(tar, members, dest)
    root = dest / top
    if not root.is_dir():
        raise FacetError(
            "download_failed",
            f"registry archive has no {top} directory",
            hint=HINT,
            details={"top": top},
        )
    return root


def _extractall(tar: tarfile.TarFile, members: list[tarfile.TarInfo], dest: Path) -> None:
    if sys.version_info >= (3, 12):
        tar.extractall(path=dest, members=members, filter="data")
    else:  # pragma: no cover - 3.11 has no extraction filter; members are checked above
        tar.extractall(path=dest, members=members)


def _discard(path: Path) -> None:
    """Remove a leftover snapshot directory, if any."""
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _validate(partial: Path, commit: str) -> None:
    """Reject a snapshot whose manifests do not load, leaving nothing."""
    try:
        load_registry(partial)
    except FacetError as exc:
        _discard(partial)
        raise FacetError(
            "download_failed",
            f"registry snapshot {commit} does not validate: {exc.message}",
            details={"commit": commit},
        ) from exc


def _previous_commit(state_file: Path) -> str | None:
    """The commit recorded in state before this update, if any."""
    try:
        state = read_json(state_file, None)
    except FacetError:
        return None
    if not isinstance(state, dict):
        return None
    commit = state.get("commit")
    return commit if isinstance(commit, str) and commit else None


def _versions(home: Home, commit: str | None) -> dict[str, str]:
    """Manifest versions of the snapshot ``commit``, empty when unreadable."""
    if not commit:
        return {}
    try:
        previous = load_registry(home.registry_dir(commit))
    except FacetError:
        return {}
    return {name: package.version for name, package in previous.packages.items()}


def _package_records(home: Home, snapshot: Path, previous: str | None) -> list[dict[str, object]]:
    """One record per package, with the version change against ``previous``."""
    versions = _versions(home, previous)
    registry = load_registry(snapshot)
    return [
        {
            "name": package.name,
            "version": package.version,
            "compatible": package.compatible,
            "changed": versions.get(package.name) != package.version,
        }
        for package in sorted(registry.packages.values(), key=lambda item: item.name)
    ]


def _now() -> str:
    """A UTC timestamp for the state file's ``fetched_at``."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
