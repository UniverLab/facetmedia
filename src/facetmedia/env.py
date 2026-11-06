"""The ``python-env`` engine: install a package into its own pinned environment.

``facet pull`` runs here.  Preflight refuses what the machine cannot do before
anything is written or downloaded; the environment is rebuilt from scratch
whenever its ``.complete`` marker does not hold the package's ``uv.lock``
hash; models stream into ``<home>/models/direct`` through a ``.part`` file and
are checked by sha256.  Every step either finishes or is redone by the next
pull: there is no repair command.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any, Final

import uv

from facetmedia import __version__, shared
from facetmedia import doctor as doctor_module
from facetmedia.contract import FacetError
from facetmedia.home import Home, atomic_write_text, require_posix, uv_env
from facetmedia.registry_types import Browser, Model, Package, Server

__all__ = [
    "browser_dir",
    "browser_present",
    "client_env_present",
    "is_installed",
    "model_present",
    "pull",
    "server_dir",
    "server_present",
    "server_root",
    "server_venv",
]

logger: Final[logging.Logger] = logging.getLogger("facetmedia")

COMPLETE: Final[str] = ".complete"
CHUNK_BYTES: Final[int] = 1024 * 1024
TIMEOUT_S: Final[float] = 60.0
USER_AGENT: Final[str] = f"facetmedia/{__version__}"
#: Backoff between the three download attempts.
BACKOFF_S: Final[tuple[float, ...]] = (2.0, 8.0)
ATTEMPTS: Final[int] = len(BACKOFF_S) + 1
UV_TAIL_LINES: Final[int] = 40
HF_API: Final[str] = "https://huggingface.co"
#: The official Playwright CDN the ``[browser]`` archives come from.
PLAYWRIGHT_CDN: Final[str] = "https://cdn.playwright.dev/dbazure/download/playwright/builds"
#: The marker a verified browser extraction carries: the archive's sha256.
BROWSER_MARKER: Final[str] = ".facet-verified"
SKIPPED_HF_FILES: Final[frozenset[str]] = frozenset({".gitattributes", "README.md"})
#: feat(run) ships the runner SDK; until then there is nothing to copy.
RUNNER_SDK: Final[Path] = Path(__file__).with_name("runner_sdk.py")
SDK_MODULE: Final[str] = "facet_runner_sdk.py"


def client_env_present(package: Package, home: Home) -> bool:
    """Whether the thin client environment is complete, the server aside."""
    return _is_complete(home.env_dir(package.name, package.version), package.lock_sha256)


def is_installed(package: Package, home: Home) -> bool:
    """Whether ``package``'s environment and server checkout are complete.

    ``facet run`` asks this before pulling: an environment whose ``.complete``
    marker holds this package's ``uv.lock`` hash needs no network, and a
    server package additionally needs its verified checkout.
    """
    return client_env_present(package, home) and server_present(package, home)


def pull(
    package: Package, home: Home, verify_models: bool = False, run_id: str = ""
) -> dict[str, object]:
    """Install ``package`` into ``home``; returns the ``facet pull`` result."""
    started = time.monotonic()
    _preflight(package, home, run_id)
    env_dir = home.env_dir(package.name, package.version)
    _print_plan(package, home, env_dir, verify_models)
    if package.browser is not None:
        _fetch_browser(package.browser, home)
    up_to_date = _is_current(package, env_dir)
    server_ready = server_present(package, home)
    if not up_to_date:
        _rebuild(package, home, env_dir)
    _fetch_server(package, home)
    records, downloaded = _fetch_models(package, home, verify_models)
    if not up_to_date:
        atomic_write_text(env_dir / COMPLETE, package.lock_sha256, home.tmp_dir)
        if package.uses:
            atomic_write_text(env_dir / shared.MARKER, shared.fingerprint(package), home.tmp_dir)
    return {
        "package": package.name,
        "version": package.version,
        "env": str(env_dir),
        "python": package.python,
        "already_installed": up_to_date and server_ready,
        "downloaded_bytes": downloaded,
        "seconds": round(time.monotonic() - started, 3),
        "models": records,
    }


# --- FR2 preflight: nothing is written or downloaded before all of this passes.


def _preflight(package: Package, home: Home, run_id: str) -> None:
    """The FR2 checks, in order, before any disk or network write.

    The checks themselves live in ``facetmedia.doctor`` so ``facet doctor``
    reports exactly what ``pull`` refuses; the package doctor (which needs an
    installed environment) is skipped until one is there.
    """
    require_posix()
    errors, _warnings = doctor_module.requirement_errors(package, home, run_id)
    if errors:
        raise errors[0]


# --- The plan, printed to stderr before anything happens.


def _print_plan(package: Package, home: Home, env_dir: Path, verify: bool) -> None:
    """FR2's plan: python, environment, each model, and the total to download.

    Every line goes to stderr and to the run log, so a ``facet run`` that
    pulls first records what it was about to download.
    """
    lines = [f"python {package.python}", f"environment {env_dir}"]
    total = 0
    for model in package.models:
        present = model_present(model, home, verify)
        if present:
            state = "already present"
        else:
            state = "to download"
            total += model.bytes
        lines.append(f"model {model.id} {model.bytes} bytes {state}")
    lines.append(f"total to download {total} bytes")
    for line in lines:
        print(line, file=sys.stderr)
        logger.info("%s", line)


# --- FR3: the environment at <home>/envs/<package>/<version>/.


def _is_complete(env_dir: Path, lock_sha256: str) -> bool:
    """Whether ``.complete`` holds this package's lock hash."""
    try:
        return (env_dir / COMPLETE).read_text(encoding="utf-8").strip() == lock_sha256
    except OSError:
        return False


def _is_current(package: Package, env_dir: Path) -> bool:
    """Whether the lock hash and, when the package has uses, the marker match."""
    if not _is_complete(env_dir, package.lock_sha256):
        return False
    return not package.uses or shared.matches(package, env_dir)


def _rebuild(package: Package, home: Home, env_dir: Path) -> None:
    """Redo the environment from scratch: delete, copy, sync, install the SDK."""
    if env_dir.exists():
        if env_dir.is_dir():
            shutil.rmtree(env_dir)
        else:
            env_dir.unlink()
    shutil.copytree(package.dir, env_dir / "package")
    _uv_sync(home, env_dir, package.python)
    _install_sdk(env_dir)
    if package.uses:
        shared.copy_into(package, _purelib(env_dir))


def _uv_sync(home: Home, env_dir: Path, python: str) -> None:
    """Run the package's frozen sync into ``<env>/venv`` with facet's own uv."""
    argv = [
        uv.find_uv_bin(),
        "sync",
        "--frozen",
        "--no-install-project",
        "--no-dev",
        "--python",
        python,
        "--project",
        str(env_dir / "package"),
    ]
    env_vars = uv_env(home)
    env_vars["UV_PROJECT_ENVIRONMENT"] = str(env_dir / "venv")
    _stream_output(argv, env_vars)


def _stream_output(argv: list[str], env_vars: dict[str, str]) -> None:
    """Run ``argv``, echoing its output to stderr and to the run log.

    A non-zero exit is ``download_failed`` carrying the last 40 lines.
    """
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env_vars,
        )
    except OSError as exc:
        raise FacetError("download_failed", f"{argv[0]}: {exc}") from exc
    tail: deque[str] = deque(maxlen=UV_TAIL_LINES)
    assert process.stdout is not None
    with process.stdout:
        for line in process.stdout:
            tail.append(line)
            sys.stderr.write(line)
            logger.info("%s", line.rstrip("\n"))
        returncode = process.wait()
    if returncode != 0:
        raise FacetError(
            "download_failed",
            f"uv {argv[1]} failed with exit code {returncode}",
            details={"exit": returncode, "output": "".join(tail)},
        )


def _install_sdk(env_dir: Path) -> None:
    """Copy the runner SDK into the venv when this facet ships one (feat(run))."""
    if not RUNNER_SDK.is_file():
        return
    shutil.copy2(RUNNER_SDK, _purelib(env_dir) / SDK_MODULE)


def _purelib(env_dir: Path) -> Path:
    """The venv's purelib, asked from the venv's own isolated interpreter."""
    argv = [
        str(env_dir / "venv" / "bin" / "python"),
        "-I",
        "-c",
        "import sysconfig; print(sysconfig.get_paths()['purelib'])",
    ]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FacetError("download_failed", f"cannot run {argv[0]}: {exc}") from exc
    lines = result.stdout.strip().splitlines()
    if result.returncode != 0 or not lines:
        raise FacetError(
            "download_failed",
            f"{argv[0]} cannot report its purelib (exit {result.returncode})",
            details={"output": (result.stderr or result.stdout)[-2000:]},
        )
    return Path(lines[-1])


# --- FR4: the models.


def _fetch_models(
    package: Package, home: Home, verify: bool
) -> tuple[list[dict[str, object]], int]:
    """Download what is missing; returns the records and the bytes fetched."""
    records: list[dict[str, object]] = []
    downloaded = 0
    for model in package.models:
        if model_present(model, home, verify):
            records.append(_model_record(model, "present"))
            continue
        if model.url:
            downloaded += _model_url(model, home)
        else:
            downloaded += _model_hf(model, home)
        records.append(_model_record(model, "downloaded"))
    return records, downloaded


def _model_record(model: Model, status: str) -> dict[str, object]:
    return {
        "id": model.id,
        "path": model.path if model.url else model.id,
        "bytes": model.bytes,
        "status": status,
    }


def model_present(model: Model, home: Home, verify: bool = False) -> bool:
    """Whether ``model`` is already under ``<home>/models/direct``.

    ``verify`` recomputes hashes; without it only the byte count is checked.
    """
    if model.url:
        return _file_ok(home.models_dir / "direct" / model.path, model, verify)
    return _hf_present(model, home, verify)


def _file_ok(target: Path, model: Model, verify: bool) -> bool:
    try:
        if target.stat().st_size != model.bytes:
            return False
    except OSError:
        return False
    return not verify or _sha256(target) == model.sha256


def _hf_present(model: Model, home: Home, verify: bool) -> bool:
    root = home.models_dir / "direct" / model.id
    if not root.is_dir():
        return False
    files = [entry for entry in root.rglob("*") if entry.is_file()]
    if sum(entry.stat().st_size for entry in files) != model.bytes:
        return False
    if not verify:
        return True
    listing = _hf_listing(model)
    return all(
        _hf_file_ok(entry, *listing.get(entry.relative_to(root).as_posix(), ("", None)))
        for entry in files
    )


def _hf_file_ok(path: Path, expected_sha: str, expected_size: int | None) -> bool:
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if expected_size is not None and size != expected_size:
        return False
    return not expected_sha or _sha256(path) == expected_sha


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _model_url(model: Model, home: Home) -> int:
    """Stream one ``url`` model into place; returns the bytes downloaded."""
    target = home.models_dir / "direct" / model.path
    part = _part_path(home, model.id)
    digest, size = _download_with_retries(part, lambda: _stream_to_part(model.url, part))
    if digest != model.sha256:
        part.unlink(missing_ok=True)
        raise FacetError(
            "download_failed",
            f"{model.id}: sha256 mismatch: expected {model.sha256}, got {digest}",
            details={"model": model.id, "expected": model.sha256, "actual": digest},
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(part, target)
    return size


def _model_hf(model: Model, home: Home) -> int:
    """Fetch every file of the pinned revision into ``models/direct/<id>/``."""
    listing = _hf_listing(model)
    root = home.models_dir / "direct" / model.id
    downloaded = 0
    for name, (expected_sha, expected_size) in sorted(listing.items()):
        if Path(name).name in SKIPPED_HF_FILES:
            continue
        target = root / name
        if _hf_file_ok(target, expected_sha, expected_size):
            continue
        downloaded += _model_hf_file(model, name, home, expected_sha, expected_size)
    return downloaded


def _model_hf_file(
    model: Model, name: str, home: Home, expected_sha: str, expected_size: int | None
) -> int:
    """Stream one repository file into place; returns the bytes downloaded."""
    url = f"{HF_API}/{model.hf_repo}/resolve/{model.revision}/{urllib.parse.quote(name, safe='/')}"
    part = _part_path(home, model.id)
    digest, size = _download_with_retries(part, lambda: _stream_to_part(url, part))
    target = home.models_dir / "direct" / model.id / name
    if expected_sha and digest != expected_sha:
        part.unlink(missing_ok=True)
        raise FacetError(
            "download_failed",
            f"{model.id}/{name}: sha256 mismatch: expected {expected_sha}, got {digest}",
            details={"model": model.id, "file": name, "expected": expected_sha, "actual": digest},
        )
    if expected_size is not None and size != expected_size:
        part.unlink(missing_ok=True)
        raise FacetError(
            "download_failed",
            f"{model.id}/{name}: size mismatch: expected {expected_size}, got {size}",
            details={"model": model.id, "file": name, "expected": expected_size, "actual": size},
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(part, target)
    return size


def _hf_listing(model: Model) -> dict[str, tuple[str, int | None]]:
    """The files of the pinned revision: path -> (lfs sha256, size)."""
    url = f"{HF_API}/api/models/{model.hf_repo}/revision/{model.revision}?blobs=true"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            data = json.load(response)
    except (OSError, ValueError) as exc:
        raise FacetError("download_failed", f"{url}: {exc}", details={"url": url}) from exc
    siblings = data.get("siblings") if isinstance(data, dict) else None
    listing: dict[str, tuple[str, int | None]] = {}
    for sibling in siblings if isinstance(siblings, list) else []:
        if isinstance(sibling, dict):
            name = sibling.get("rfilename")
            if isinstance(name, str) and name:
                listing[name] = _sibling_expectation(sibling)
    if not listing:
        raise FacetError(
            "download_failed",
            f"{model.hf_repo}@{model.revision}: the revision lists no files",
            details={"url": url},
        )
    return listing


def _sibling_expectation(sibling: Mapping[str, Any]) -> tuple[str, int | None]:
    lfs = sibling.get("lfs")
    sha = lfs.get("sha256") if isinstance(lfs, dict) else ""
    size = sibling.get("size")
    return (sha if isinstance(sha, str) else "", size if isinstance(size, int) else None)


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


# --- The shared browser: one verified extraction per channel and revision.


def browser_dir(browser: Browser, home: Home) -> Path:
    """``<home>/browsers/<channel>-<revision>``, Playwright's own layout."""
    return home.browsers_dir / f"{browser.channel}-{browser.revision}"


def browser_present(browser: Browser, home: Home) -> bool:
    """Whether the extraction exists and its marker holds the declared sha256."""
    try:
        marker = (browser_dir(browser, home) / BROWSER_MARKER).read_text(encoding="utf-8")
    except OSError:
        return False
    return marker.strip() == browser.sha256


def _fetch_browser(browser: Browser, home: Home) -> None:
    """Download, verify and extract the declared browser, once per home.

    The archive lands at Playwright's ``<name>-<revision>`` layout under
    ``<home>/browsers``, so the package's own ``playwright`` finds it through
    ``PLAYWRIGHT_BROWSERS_PATH`` without downloading anything itself.
    """
    if browser_present(browser, home):
        return
    url = f"{PLAYWRIGHT_CDN}/{browser.channel}/{browser.revision}/chromium-linux.zip"
    part = _part_path(home, f"{browser.channel}-{browser.revision}")
    digest, _size = _download_with_retries(part, lambda: _stream_to_part(url, part))
    if digest != browser.sha256:
        part.unlink(missing_ok=True)
        raise FacetError(
            "download_failed",
            f"{browser.channel}-{browser.revision}: sha256 mismatch: "
            f"expected {browser.sha256}, got {digest}",
            details={
                "browser": f"{browser.channel}-{browser.revision}",
                "expected": browser.sha256,
                "actual": digest,
            },
        )
    staging = home.tmp_dir / f"{browser.channel}-{browser.revision}.{secrets.token_hex(4)}"
    try:
        _extract_browser(part, staging)
    finally:
        part.unlink(missing_ok=True)
    target = browser_dir(browser, home)
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    os.replace(staging, target)
    atomic_write_text(target / BROWSER_MARKER, browser.sha256, home.tmp_dir)


def _extract_browser(archive: Path, staging: Path) -> None:
    """Unpack the verified archive into ``staging``; a failure leaves nothing."""
    staging.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(staging)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _part_path(home: Home, model_id: str) -> Path:
    return home.tmp_dir / f"{model_id}.{secrets.token_hex(4)}.part"


# --- The server: one pinned checkout and its venv per package and version.

#: How long one git command may take; a large tool is cloned once per version.
GIT_TIMEOUT_S: Final[float] = 1800.0
#: The commit's short name in the checkout path.
SHORT_REV: Final[int] = 12
GIT_TAIL_LINES: Final[int] = 40


def server_root(package: Package, home: Home) -> Path:
    """``<home>/servers/<package>/<version>``: every checkout of one version."""
    return home.servers_dir / package.name / package.version


def server_dir(package: Package, home: Home) -> Path:
    """The checkout itself: ``<home>/servers/<package>/<version>/<shortrev>``."""
    server = package.server
    assert server is not None
    return server_root(package, home) / server.rev[:SHORT_REV]


def server_venv(package: Package, home: Home) -> Path:
    """The venv the server's launch argv finds on ``PATH``."""
    return server_dir(package, home) / "venv"


def server_present(package: Package, home: Home) -> bool:
    """Whether a verified checkout holds this repo, rev and requirements file.

    A package without ``[engine.server]`` is always present: there is
    nothing to fetch.  The marker is written last, after the venv holds the
    tool's requirements, so a crash mid-pull is redone by the next pull.
    """
    server = package.server
    if server is None:
        return True
    target = server_dir(package, home)
    try:
        marker = (target / COMPLETE).read_text(encoding="utf-8")
        digest = _sha256(target / server.requirements)
    except OSError:
        return False
    return marker.strip() == _server_fingerprint(server, digest)


def _server_fingerprint(server: Server, requirements_sha256: str) -> str:
    """The pinned rev plus the bytes of the tool's own requirements file."""
    return f"{server.rev}\n{requirements_sha256}"


def _fetch_server(package: Package, home: Home) -> None:
    """Clone, build and mark the declared server, unless its marker verifies."""
    server = package.server
    if server is None or server_present(package, home):
        return
    root = server_root(package, home)
    if root.exists():
        shutil.rmtree(root)
    target = server_dir(package, home)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        _git_checkout(target, server, uv_env(home))
        _server_build(home, target, package, server)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    digest = _sha256(target / server.requirements)
    atomic_write_text(target / COMPLETE, _server_fingerprint(server, digest), home.tmp_dir)


def _git_checkout(target: Path, server: Server, env_vars: dict[str, str]) -> None:
    """A branchless shallow fetch of exactly ``rev``, then a detached checkout."""
    target.mkdir(parents=True)
    _git(target, ["init", "--quiet"], env_vars)
    _git(target, ["remote", "add", "origin", server.repo], env_vars)
    _git(target, ["fetch", "--quiet", "--depth", "1", "origin", server.rev], env_vars)
    _git(target, ["checkout", "--quiet", "--detach", "FETCH_HEAD"], env_vars)


def _git(cwd: Path, argv: list[str], env_vars: dict[str, str]) -> None:
    """Run one git command; a non-zero exit is ``download_failed`` with its tail."""
    command = ["git", *argv]
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            env=env_vars,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FacetError("download_failed", f"git {argv[0]}: {exc}") from exc
    if result.returncode != 0:
        output = (result.stderr or result.stdout).splitlines()[-GIT_TAIL_LINES:]
        raise FacetError(
            "download_failed",
            f"git {argv[0]} failed with exit code {result.returncode}",
            details={"exit": result.returncode, "output": "\n".join(output)},
        )


def _server_build(home: Home, target: Path, package: Package, server: Server) -> None:
    """Create the venv with the manifest's pinned python and install requirements."""
    uv_bin = uv.find_uv_bin()
    _stream_output(
        [uv_bin, "venv", "--python", package.python, str(target / "venv")],
        uv_env(home),
    )
    _stream_output(
        [
            uv_bin,
            "pip",
            "install",
            "--python",
            str(target / "venv" / "bin" / "python"),
            "-r",
            str(target / server.requirements),
        ],
        uv_env(home),
    )
