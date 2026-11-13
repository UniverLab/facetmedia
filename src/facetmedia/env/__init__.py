"""The ``python-env`` engine: install a package into its own pinned environment.

``facet pull`` runs here.  Preflight refuses what the machine cannot do before
anything is written or downloaded; the environment is rebuilt from scratch
whenever its ``.complete`` marker does not hold the package's ``uv.lock``
hash, or its ``.package`` marker does not hold the fingerprint over every
file under the package directory (see ``facetmedia.env.package_marker``):
a same-version registry edit still triggers a rebuild.  Models stream into
``<home>/models/direct`` through a ``.part`` file and are checked by sha256.
Every step either finishes or is redone by the next pull: there is no
repair command.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

import uv

from facetmedia import config as config_module
from facetmedia import doctor as doctor_module
from facetmedia import installed as installed_module
from facetmedia import licenses, shared
from facetmedia.contract import FacetError
from facetmedia.env import binary as env_binary
from facetmedia.env import browser as env_browser
from facetmedia.env import ffmpeg as env_ffmpeg
from facetmedia.env.binary import binary_dir, binary_present, binary_problem
from facetmedia.env.browser import browser_dir, browser_executable, browser_present
from facetmedia.env.download import (
    CHUNK_BYTES,
    TIMEOUT_S,
    USER_AGENT,
    _download_with_retries,
    _part_path,
    _stream_to_part,
)
from facetmedia.env.ffmpeg import (
    ffmpeg_bin_dir,
    ffmpeg_dir,
    ffmpeg_executable,
    ffmpeg_present,
    ffprobe_executable,
)
from facetmedia.env.node import (
    NODE_MARKER,
    _fetch_package,
    _fetch_runtime,
    node_package_present,
    node_present,
    node_problem,
    node_runtime_present,
    node_staged_current,
    stage_node,
)
from facetmedia.env.package_marker import (
    COMPLETE,
    PACKAGE_MARKER,
    complete_matches,
    package_fingerprint,
    package_stale,
)
from facetmedia.env.source import fetch_source, source_dir, source_present
from facetmedia.home import Home, atomic_write_text, require_posix, uv_env
from facetmedia.progress import Progress, verbose
from facetmedia.registry_types import (
    Model,
    Package,
    Server,
    is_backend,
)

__all__ = [
    "COMPLETE",
    "NODE_MARKER",
    "binary_dir",
    "binary_present",
    "binary_problem",
    "browser_dir",
    "browser_executable",
    "browser_present",
    "ffmpeg_bin_dir",
    "ffmpeg_dir",
    "ffmpeg_executable",
    "ffmpeg_present",
    "ffprobe_executable",
    "client_env_present",
    "is_installed",
    "model_present",
    "node_package_present",
    "node_present",
    "node_problem",
    "node_runtime_present",
    "node_staged_current",
    "package_fingerprint",
    "package_stale",
    "pull",
    "fetch_source",
    "server_dir",
    "server_present",
    "server_root",
    "server_venv",
    "source_dir",
    "source_present",
]

logger: Final[logging.Logger] = logging.getLogger("facetmedia")

UV_TAIL_LINES: Final[int] = 40
HF_API: Final[str] = "https://huggingface.co"
SKIPPED_HF_FILES: Final[frozenset[str]] = frozenset({".gitattributes", "README.md"})
#: feat(run) ships the runner SDK; until then there is nothing to copy.
#: ``runner_sdk.py`` stays at the package root, one level above ``env``.
RUNNER_SDK: Final[Path] = Path(__file__).parent.parent / "runner_sdk.py"
SDK_MODULE: Final[str] = "facet_runner_sdk.py"


def client_env_present(package: Package, home: Home) -> bool:
    """Whether the thin client side is complete, the server aside.

    A ``node`` package has no python environment: its client side is the
    verified runtime and package extractions under the facet home.  A
    ``host-app`` package has no environment either: it is usable exactly when
    its host application resolves and meets the manifest's minimum version.
    A ``binary`` package's client side is its verified zip extraction.
    """
    if package.host_app is not None:
        return config_module.host_app_installed(package.host_app, home)
    binary = env_binary.engine_present(package, home)
    if binary is not None:
        return binary
    if package.node is not None:
        return node_present(package, home) and node_staged_current(package, home)
    return complete_matches(home.env_dir(package.name, package.version), package.lock_sha256)


def is_installed(package: Package, home: Home) -> bool:
    """Whether ``package``'s environment and server checkout are complete.

    ``facet run`` asks this before pulling: an environment whose ``.complete``
    marker holds this package's ``uv.lock`` hash needs no network, and a
    server package additionally needs its verified checkout.
    """
    return (
        client_env_present(package, home)
        and server_present(package, home)
        and source_present(package, home)
    )


def pull(
    package: Package,
    home: Home,
    verify_models: bool = False,
    run_id: str = "",
    progress: Progress | None = None,
) -> dict[str, object]:
    """Install ``package`` into ``home``; returns the ``facet pull`` result.

    ``progress`` draws one line per phase. ``None`` keeps the historical
    behaviour for a caller such as ``facet run``: the plan and uv's output are
    echoed and no phase line is drawn.
    """
    started = time.monotonic()
    _preflight(package, home, run_id)
    env_dir = home.env_dir(package.name, package.version)
    _print_plan(package, home, env_dir, verify_models)
    if package.host_app is not None:
        host_report = _pull_host_app(package, home, env_dir, started)
        installed_module.record(home, package.name, package.version)
        return host_report
    reporter = progress if progress is not None else Progress.off()
    already, environment_bytes = _environment_phase(package, home, env_dir, reporter)
    records, model_bytes = _models_phase(package, home, verify_models, reporter)
    artifact_bytes = _artifacts_phase(package, home, reporter)
    report: dict[str, object] = {
        "package": package.name,
        "version": package.version,
        "env": str(env_dir),
        "python": package.python,
        "already_installed": already,
        "downloaded_bytes": environment_bytes + model_bytes,
        "seconds": round(time.monotonic() - started, 3),
        "models": records,
    }
    installed_module.record(home, package.name, package.version)
    return _with_artifacts(report, artifact_bytes)


def _environment_phase(
    package: Package, home: Home, env_dir: Path, reporter: Progress
) -> tuple[bool, int]:
    """Build what the engine runs on; returns ``already_installed`` and the bytes."""
    reporter.phase_start("environment")
    try:
        already, downloaded = _environment(package, home, env_dir, reporter)
    except BaseException as exc:
        reporter.phase_end(ok=False, err=_one_line(exc))
        raise
    reporter.phase_end(ok=True)
    return already, downloaded


def _environment(
    package: Package, home: Home, env_dir: Path, reporter: Progress
) -> tuple[bool, int]:
    """The engine's own install step, under the ``environment`` phase."""
    if package.node is not None:
        return _environment_node(package, home, env_dir, reporter)
    if package.binary is not None:
        return env_binary.binary_present(package.binary, home), 0
    return _environment_python(package, home, env_dir), 0


def _environment_python(package: Package, home: Home, env_dir: Path) -> bool:
    """The ``python-env`` engine: the environment, server and source."""
    up_to_date = _is_current(package, env_dir) and not package_stale(package, home)
    server_ready = server_present(package, home) and source_present(package, home)
    if not up_to_date:
        _rebuild(package, home, env_dir)
    _fetch_server(package, home)
    fetch_source(package, home)
    if not up_to_date:
        atomic_write_text(env_dir / COMPLETE, package.lock_sha256, home.tmp_dir)
        atomic_write_text(env_dir / PACKAGE_MARKER, package_fingerprint(package), home.tmp_dir)
        if package.uses:
            atomic_write_text(env_dir / shared.MARKER, shared.fingerprint(package), home.tmp_dir)
    return up_to_date and server_ready


def _environment_node(
    package: Package, home: Home, env_dir: Path, reporter: Progress
) -> tuple[bool, int]:
    """The ``node`` engine: the verified runtime, then the staged entry."""
    node = package.node
    assert node is not None
    stale = package_stale(package, home)
    up_to_date = node_present(package, home) and node_staged_current(package, home) and not stale
    downloaded = _fetch_runtime(node, home, reporter.advance, reporter.phase_total)
    if not node_staged_current(package, home) or stale:
        stage_node(package, home, env_dir)
    return up_to_date, downloaded


def _models_phase(
    package: Package, home: Home, verify: bool, reporter: Progress
) -> tuple[list[dict[str, object]], int]:
    """Download the missing models as one bar over the bytes they declare."""
    total = _missing_model_bytes(package, home, verify)
    reporter.phase_start("models", total)
    try:
        records, downloaded = _fetch_models(package, home, verify, reporter.advance)
    except BaseException as exc:
        reporter.phase_end(ok=False, err=_one_line(exc))
        raise
    reporter.phase_end(ok=True)
    return records, downloaded


def _missing_model_bytes(package: Package, home: Home, verify: bool) -> int:
    """The declared bytes of every model this pull has to download."""
    return sum(
        model.bytes
        for model in package.models
        if not is_backend(model) and not model_present(model, home, verify)
    )


def _artifacts_phase(package: Package, home: Home, reporter: Progress) -> int:
    """Fetch the browser, FFmpeg and engine tarballs; returns the bytes."""
    reporter.phase_start("artifacts")
    try:
        downloaded = _fetch_artifacts(package, home, reporter.advance, reporter.phase_total)
        downloaded += _fetch_engine_artifacts(package, home, reporter)
    except BaseException as exc:
        reporter.phase_end(ok=False, err=_one_line(exc))
        raise
    reporter.phase_end(ok=True)
    return downloaded


def _fetch_engine_artifacts(package: Package, home: Home, reporter: Progress) -> int:
    """The binary zip and the node package tarballs, all under ``artifacts``."""
    downloaded = 0
    if package.binary is not None:
        downloaded += env_binary.fetch_binary(
            package.binary, home, reporter.advance, reporter.phase_total
        )
    node = package.node
    if node is not None:
        for spec in node.packages:
            if not node_package_present(spec, home):
                downloaded += _fetch_package(spec, home, reporter.advance, reporter.phase_total)
    return downloaded


def _one_line(exc: BaseException) -> str:
    """The first line of a failure, for the ``✗`` progress line."""
    text = exc.message if isinstance(exc, FacetError) else str(exc)
    return text.splitlines()[0] if text else type(exc).__name__


def _fetch_artifacts(
    package: Package,
    home: Home,
    on_bytes: Callable[[int], None] | None = None,
    on_total: Callable[[int], None] | None = None,
) -> int:
    """Fetch the shared, engine-independent artifacts; returns the bytes downloaded.

    The browser and the FFmpeg build serve every engine: node packages render
    and encode with them too.
    """
    downloaded = 0
    if package.browser is not None:
        downloaded += env_browser.fetch_browser(package.browser, home, on_bytes, on_total)
    if package.ffmpeg is not None:
        downloaded += env_ffmpeg.fetch_ffmpeg(package.ffmpeg, home, on_bytes, on_total)
    return downloaded


def _with_artifacts(report: dict[str, object], artifact_bytes: int) -> dict[str, object]:
    """Count the shared artifacts' bytes in the pull result."""
    if not artifact_bytes:
        return report
    previous = report.get("downloaded_bytes")
    report["downloaded_bytes"] = (previous if isinstance(previous, int) else 0) + artifact_bytes
    report["already_installed"] = False
    return report


def _pull_host_app(
    package: Package, home: Home, env_dir: Path, started: float
) -> dict[str, object]:
    """The ``host-app`` engine's pull: locate and verify, never install.

    Nothing is downloaded and no environment is built; a direct call re-checks
    what the preflight gate already reported.
    """
    app = package.host_app
    assert app is not None
    problem = config_module.host_app_problem(app, home)
    if problem is not None:
        raise FacetError(
            "machine_cannot", problem.message, hint=problem.fix, details=problem.details
        )
    return {
        "package": package.name,
        "version": package.version,
        "env": str(env_dir),
        "python": "",
        "already_installed": True,
        "downloaded_bytes": 0,
        "seconds": round(time.monotonic() - started, 3),
        "models": [],
    }


# --- FR2 preflight: nothing is written or downloaded before all of this passes.


def _preflight(package: Package, home: Home, run_id: str) -> None:
    """The FR2 checks, in order, before any disk or network write.

    The checks themselves live in ``facetmedia.doctor`` so ``facet doctor``
    reports exactly what ``pull`` refuses; the package doctor (which needs an
    installed environment) is skipped until one is there.
    """
    require_posix()
    if package.browser is not None:
        env_browser.require_host()
    if package.ffmpeg is not None:
        env_ffmpeg.require_host()
    errors, _warnings = doctor_module.requirement_errors(package, home, run_id)
    # pull gates only the licences of models it downloads: a backend-owned
    # model is never fetched here, and the run gates it per piece.
    errors = [error for error in errors if error.code != "license_not_accepted"]
    if errors:
        raise errors[0]
    licenses.require(model for model in package.models if not is_backend(model))


# --- The plan, printed to stderr before anything happens.


def _print_plan(package: Package, home: Home, env_dir: Path, verify: bool) -> None:
    """FR2's plan: python, environment, each model, and the total to download.

    Every line goes to stderr and to the run log, so a ``facet run`` that
    pulls first records what it was about to download.
    """
    if package.host_app is not None:
        lines = [f"host application {package.host_app.app}"]
    elif package.node is not None:
        lines = [f"node {package.node.runtime}"]
        lines += [
            f"node package {spec.name} {spec.version}"
            + ("" if node_package_present(spec, home) else " to download")
            for spec in package.node.packages
        ]
    elif package.binary is not None:
        lines = env_binary.plan_lines(package, home)
    else:
        lines = [f"python {package.python}", f"environment {env_dir}"]
    if package.source is not None:
        lines.append(f"source {package.source.repo} @ {package.source.rev[:SHORT_REV]}")
    total = 0
    for model in package.models:
        if is_backend(model):
            lines.append(f"model {model.id} backend-owned, skipped")
            continue
        present = model_present(model, home, verify)
        if present:
            state = "already present"
        else:
            state = "to download"
            total += model.bytes
        lines.append(f"model {model.id} {model.bytes} bytes {state}")
    lines.append(f"total to download {total} bytes")
    for line in lines:
        logger.info("%s", line)
        if verbose():
            print(line, file=sys.stderr)


# --- FR3: the environment at <home>/envs/<package>/<version>/.


def _is_current(package: Package, env_dir: Path) -> bool:
    """Whether the lock hash and, when the package has uses, the marker match."""
    if not complete_matches(env_dir, package.lock_sha256):
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
            logger.info("%s", line.rstrip("\n"))
            if verbose():
                sys.stderr.write(line)
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
    package: Package,
    home: Home,
    verify: bool,
    on_bytes: Callable[[int], None] | None = None,
) -> tuple[list[dict[str, object]], int]:
    """Download what is missing; returns the records and the bytes fetched."""
    records: list[dict[str, object]] = []
    downloaded = 0
    for model in package.models:
        if is_backend(model):
            records.append(_model_record(model, "backend-owned"))
            continue
        if model_present(model, home, verify):
            records.append(_model_record(model, "present"))
            continue
        if model.url:
            downloaded += _model_url(model, home, on_bytes)
        else:
            downloaded += _model_hf(model, home, on_bytes)
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
    A backend-owned model is never fetched, so it is always present.
    """
    if is_backend(model):
        return True
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


def _model_url(model: Model, home: Home, on_bytes: Callable[[int], None] | None = None) -> int:
    """Stream one ``url`` model into place; returns the bytes downloaded."""
    target = home.models_dir / "direct" / model.path
    part = _part_path(home, model.id)
    digest, size = _download_with_retries(part, lambda: _stream_to_part(model.url, part, on_bytes))
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


def _model_hf(model: Model, home: Home, on_bytes: Callable[[int], None] | None = None) -> int:
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
        downloaded += _model_hf_file(model, name, home, expected_sha, expected_size, on_bytes)
    return downloaded


def _model_hf_file(
    model: Model,
    name: str,
    home: Home,
    expected_sha: str,
    expected_size: int | None,
    on_bytes: Callable[[int], None] | None = None,
) -> int:
    """Stream one repository file into place; returns the bytes downloaded."""
    url = f"{HF_API}/{model.hf_repo}/resolve/{model.revision}/{urllib.parse.quote(name, safe='/')}"
    part = _part_path(home, model.id)
    digest, size = _download_with_retries(part, lambda: _stream_to_part(url, part, on_bytes))
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
        git_checkout(target, server.repo, server.rev, uv_env(home))
        _server_build(home, target, package, server)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    digest = _sha256(target / server.requirements)
    atomic_write_text(target / COMPLETE, _server_fingerprint(server, digest), home.tmp_dir)


def git_checkout(target: Path, repo: str, rev: str, env_vars: dict[str, str]) -> None:
    """A branchless shallow fetch of exactly ``rev``, then a detached checkout."""
    target.mkdir(parents=True)
    _git(target, ["init", "--quiet"], env_vars)
    _git(target, ["remote", "add", "origin", repo], env_vars)
    _git(target, ["fetch", "--quiet", "--depth", "1", "origin", rev], env_vars)
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
    if Path(server.requirements).name == "uv.lock":
        _server_sync(home, target, package)
    else:
        _server_pip_install(home, target, package, server)


def _server_sync(home: Home, target: Path, package: Package) -> None:
    """Build the venv from the checkout's locked project with ``uv sync``."""
    argv = [
        uv.find_uv_bin(),
        "sync",
        "--frozen",
        "--no-dev",
        "--project",
        str(target),
        "--python",
        package.python,
    ]
    env_vars = uv_env(home)
    env_vars["UV_PROJECT_ENVIRONMENT"] = str(target / "venv")
    _stream_output(argv, env_vars)


def _server_pip_install(home: Home, target: Path, package: Package, server: Server) -> None:
    """Create the venv and pip-install the manifest's requirements file."""
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
