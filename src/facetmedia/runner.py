"""The runner transport: call a package's runner through the file protocol.

facet's core never imports a tool library.  It writes a request file, runs
the package's runner exactly as its manifest's ``transport.invocation_template``
says (``{{python}}`` is the environment's interpreter, ``{{entry}}`` the
runner script), and reads the result file back.  The runner is a separate
interpreter in its own process group: on an interrupt, a timeout, or a crash
nothing of it survives — SIGTERM to the group, SIGKILL 10 s later.

A runner that exits non-zero, writes no result, or lists an asset it did not
produce is ``runner_failed`` with the last 40 lines of its output, the log
path and the (kept) temporary directory.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Final
from urllib.parse import urlsplit, urlunsplit

from facetmedia import config
from facetmedia.contract import FacetError
from facetmedia.env import binary as env_binary
from facetmedia.home import Home, log_file, uv_env
from facetmedia.registry import PROTOCOL_VERSION, Package
from facetmedia.registry_types import is_backend

__all__ = [
    "REQUEST_NAME",
    "RESULT_NAME",
    "Invocation",
    "Outcome",
    "RunAsset",
    "VerifyResult",
    "build_argv",
    "call",
    "make_request",
    "model_paths",
    "parse_run_result",
    "parse_verify_result",
]

logger = logging.getLogger("facetmedia")

REQUEST_NAME: Final[str] = ".facet-request"
RESULT_NAME: Final[str] = ".facet-result.json"
#: What a server package's client runner reads to reach the tool facet started.
SERVER_URL_ENV: Final[str] = "FACET_SERVER_URL"
#: The variable a launched server reads the port facet picked from.
SERVER_PORT_ENV: Final[str] = "PORT"
#: The manifest's port token; the same spelling ``[engine.server]`` declares.
PORT_TOKEN: Final[str] = "{{port}}"
#: The manifest's data token: the package's persistent directory under the home.
DATA_TOKEN: Final[str] = "{{data}}"
HEALTH_TIMEOUT_S: Final[float] = 1.0
SERVER_POLL_S: Final[float] = 0.2
HEALTHY_STATUS: Final[tuple[int, int]] = (200, 300)
#: How many lines of runner output are kept for a failure report.
TAIL_LINES: Final[int] = 40
#: Grace between SIGTERM and SIGKILL when a runner must die.
KILL_GRACE_S: Final[float] = 10.0
POLL_S: Final[float] = 0.1
RESERVED_PREFIX: Final[str] = ".facet-"
#: The flags Blender needs before the script: the exit-code flag must come
#: first, because Blender handles both in command-line order and a later
#: registration is too late to turn a raising script into a failing run.
BLENDER_FLAGS: Final[tuple[str, ...]] = (
    "-b",
    "--factory-startup",
    "--python-exit-code",
    "1",
    "--python",
)
#: The bundled FreeCAD wrapper, ported from the lab's ``freecad_run.py``.
FREECAD_WRAPPER: Final[Path] = Path(__file__).with_name("host_freecad_run.py")
WSLPATH_TIMEOUT_S: Final[float] = 10.0
#: Facet variables forwarded through ``WSLENV`` to a Windows ``.exe`` host.
#: Path variables take the ``/p`` flag so WSL translates them; the rest
#: cross as plain strings.
WSLENV_PATH_VARS: Final[tuple[str, ...]] = (
    "FACET_MODELS",
    "HF_HOME",
    "FACET_HOME",
    "FACET_BIN",
    "PLAYWRIGHT_BROWSERS_PATH",
)
WSLENV_PLAIN_VARS: Final[tuple[str, ...]] = (
    "FACET_SERVER_URL",
    "CUDA_VISIBLE_DEVICES",
)


class RunnerInterruptedError(Exception):
    """A SIGINT or SIGTERM arrived while a runner was running."""


@dataclass(frozen=True)
class Invocation:
    """Everything one runner call needs that is not the verb or request."""

    package: Package
    home: Home
    env_dir: Path
    temp_dir: Path
    run_id: str
    timeout_s: float | None = None
    start_server: bool = True


@dataclass(frozen=True)
class Outcome:
    """The runner's result file plus the output it printed on the way."""

    result: Mapping[str, object]
    output: str


@dataclass(frozen=True)
class RunAsset:
    """One asset the ``run`` verb says it produced in the temp directory."""

    file: str
    name: str
    kind: str
    mime: str
    properties: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    seed: int | None = None
    inputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerifyResult:
    """One asset's verdict from the ``verify`` verb."""

    file: str
    passed: bool
    metrics: dict[str, Any] = field(default_factory=dict)
    thresholds: dict[str, Any] = field(default_factory=dict)
    verifier: str = ""


def build_argv(
    package: Package,
    home: Home,
    env_dir: Path,
    verb: str,
    input_path: Path,
    result_path: Path,
) -> list[str]:
    """The runner command: the manifest template with its tokens substituted."""
    if package.host_app is not None:
        return _host_app_argv(package, home, verb, input_path, result_path)
    if package.binary is not None:
        return _binary_argv(package, home, verb, input_path, result_path)
    node, entry = _entry_points(package, home, env_dir)
    replacements = (
        ("{{node}}", node),
        ("{{python}}", str(env_dir / "venv" / "bin" / "python")),
        ("{{entry}}", entry),
        ("{{verb}}", verb),
        ("{{input}}", str(input_path)),
        ("{{result}}", str(result_path)),
    )
    return [_substitute(token, replacements) for token in shlex.split(package.invocation_template)]


def _host_app_argv(
    package: Package,
    home: Home,
    verb: str,
    input_path: Path,
    result_path: Path,
) -> list[str]:
    """The host application's argv: the injected flags, then the template's args.

    ``{{app}}`` and ``{{entry}}`` are owned by the runner, not the template:
    the resolved binary and the package's script land in the injected prefix,
    and every other template token follows it.  A host application is located
    and version-checked here as well, so a direct call cannot run the wrong one.
    """
    app = package.host_app
    assert app is not None
    problem = config.host_app_problem(app, home)
    resolved = config.resolve(home, app)
    if problem is not None or resolved is None:
        message = problem.message if problem is not None else f"{app.app} not found"
        raise FacetError("machine_cannot", message, hint=problem.fix if problem else None)
    entry = _host_arg(str(package.directory / package.entry), resolved.path)
    replacements = (
        ("{{verb}}", verb),
        ("{{input}}", str(input_path)),
        ("{{result}}", str(result_path)),
    )
    remaining = [
        _host_arg(_substitute(token, replacements), resolved.path)
        for token in shlex.split(package.invocation_template)
        if token not in {"{{app}}", "{{entry}}"}
    ]
    return [*_host_prefix(app.app, resolved.path, entry), *remaining]


def _binary_argv(
    package: Package,
    home: Home,
    verb: str,
    input_path: Path,
    result_path: Path,
) -> list[str]:
    """The managed binary's argv: ``{{binary}}`` is owned by the runner.

    The resolved executable is re-probed here — ``<bin> <version_arg>`` —
    so a direct call can never run a missing extraction or the wrong
    version; the template's remaining tokens follow it.
    """
    binary = package.binary
    assert binary is not None
    problem = env_binary.binary_problem(package, home)
    resolved = env_binary.binary_path(binary, home)
    if problem is not None:
        raise FacetError("machine_cannot", problem)
    replacements = (
        ("{{verb}}", verb),
        ("{{input}}", str(input_path)),
        ("{{result}}", str(result_path)),
    )
    remaining = [
        _substitute(token, replacements)
        for token in shlex.split(package.invocation_template)
        if token != "{{binary}}"
    ]
    return [str(resolved), *remaining]


def _host_prefix(app: str, binary: str, entry: str) -> list[str]:
    """The flags each host application needs before any of the script's args."""
    if app == "blender":
        return [binary, *BLENDER_FLAGS, entry]
    return [binary, _host_arg(str(FREECAD_WRAPPER), binary), "--pass", entry]


def _host_arg(value: str, binary: str) -> str:
    """Convert an argument that crosses into Windows, as the lab scripts do."""
    if not binary.lower().endswith(".exe") or "/" not in value:
        return value
    return _wslpath(os.path.abspath(value))


def _wslpath(value: str) -> str:
    """``wslpath -w`` of ``value``; the value itself when wslpath cannot answer."""
    try:
        result = subprocess.run(
            ["wslpath", "-w", value],
            capture_output=True,
            text=True,
            check=False,
            timeout=WSLPATH_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return value
    converted = result.stdout.strip()
    return converted if result.returncode == 0 and converted else value


def _entry_points(package: Package, home: Home, env_dir: Path) -> tuple[str, str]:
    """``({{node}}, {{entry}})``: the managed binary and the entry's real path.

    Every engine's entry is ``<env>/package/<entry>``, staged by the pull.
    """
    entry = str(env_dir / "package" / package.entry)
    if package.node is None:
        return "", entry
    node_bin, _entry_dir = _node_paths(package, home)
    return str(node_bin), entry


def _node_paths(package: Package, home: Home) -> tuple[Path, Path]:
    """``(bin/node, entry package dir)`` under the facet home for ``package``."""
    node = package.node
    assert node is not None
    first = node.packages[0]
    return (
        home.node_runtime_dir(node.runtime) / "bin" / "node",
        home.node_pkg_dir(first.name, first.version),
    )


def _substitute(token: str, replacements: Sequence[tuple[str, str]]) -> str:
    for placeholder, value in replacements:
        token = token.replace(placeholder, value)
    return token


def model_paths(package: Package, home: Home) -> dict[str, Path]:
    """``{model id: absolute path under models/direct}`` for the request.

    Backend-owned models have no local file and are left out.
    """
    paths: dict[str, Path] = {}
    for model in package.models:
        if is_backend(model):
            continue
        leaf = model.path if model.url else model.id
        paths[model.id] = home.models_dir / "direct" / leaf
    return paths


def make_request(
    verb: str,
    capability: str,
    inputs: Mapping[str, Any],
    out_dir: Path,
    piece_dir: Path,
    models: Mapping[str, Path],
    assets: Sequence[Path] = (),
) -> dict[str, object]:
    """The request file's JSON object (runner protocol 1, FR3)."""
    listed = [str(path) for path in assets] if verb in {"verify", "preview"} else []
    return {
        "protocol": PROTOCOL_VERSION,
        "verb": verb,
        "capability": capability,
        "inputs": dict(inputs),
        "out_dir": os.path.abspath(str(out_dir)),
        "piece_dir": os.path.abspath(str(piece_dir)),
        "models": {key: str(models[key]) for key in sorted(models)},
        "assets": listed,
    }


def runner_env(home: Home, package: Package) -> dict[str, str]:
    """The runner's environment: uv's, plus the models dir and the GPU flag."""
    from facetmedia import env as env_module  # noqa: PLC0415

    env_vars = uv_env(home)
    env_vars["FACET_MODELS"] = str(home.models_dir / "direct")
    if package.source is not None:
        env_vars["FACET_SOURCE_DIR"] = str(env_module.source_dir(package, home))
    if package.node is not None:
        node_bin, _entry_dir = _node_paths(package, home)
        env_vars["FACET_NODE"] = str(node_bin)
        env_vars["FACET_NODE_PATH"] = os.pathsep.join(
            str(home.node_pkg_dir(spec.name, spec.version)) for spec in package.node.packages
        )
    if package.binary is not None:
        resolved = env_binary.binary_path(package.binary, home)
        env_vars["FACET_BIN"] = str(resolved)
        env_vars["PATH"] = str(resolved.parent) + os.pathsep + env_vars.get("PATH", "")
    if package.browser is not None:
        env_vars["PLAYWRIGHT_BROWSERS_PATH"] = str(home.browsers_dir)
    if not package.requirements.gpu:
        env_vars["CUDA_VISIBLE_DEVICES"] = ""
    return env_vars


def call(inv: Invocation, verb: str, request: Mapping[str, object]) -> Outcome:
    """Run one verb and return its parsed-but-untyped result file.

    The runner's stdout and stderr are streamed to facet's stderr and the run
    log, never to facet's stdout.  SIGINT or SIGTERM to facet, or ``--timeout``
    running out, kills the runner's whole process group and fails with
    ``runner_failed``.
    """
    input_path = inv.temp_dir / REQUEST_NAME
    result_path = inv.temp_dir / RESULT_NAME
    input_path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    # run and verify share the temp dir: a result an earlier verb left behind
    # must never be read as this verb's answer to "did you write a result".
    result_path.unlink(missing_ok=True)
    argv = build_argv(inv.package, inv.home, inv.env_dir, verb, input_path, result_path)
    logger.info("runner %s: %s", verb, " ".join(argv))
    tail: deque[str] = deque(maxlen=TAIL_LINES)
    # The signal trap is installed before the server and the runner start, so a
    # signal that arrives the instant either appears is still ours to clean up.
    with _trapped(), _server_session(inv) as server_url:
        returncode = _run_client(inv, verb, argv, tail, server_url)
    if returncode != 0:
        raise _failure(inv, tail, f"runner {verb} failed with exit code {returncode}")
    return Outcome(_read_result(result_path, inv, tail), "".join(tail))


def _maybe_add_wslenv(inv: Invocation, env_vars: dict[str, str]) -> None:
    """Forward facet's variables through ``WSLENV`` to a Windows host.

    WSL interop passes only the variables named in ``WSLENV`` to a
    ``.exe`` process, so a Windows host application would otherwise never
    see ``FACET_MODELS`` and friends.  Linux binaries are untouched.
    """
    app = inv.package.host_app
    if app is None:
        return
    resolved = config.resolve(inv.home, app)
    if resolved is None or not resolved.path.lower().endswith(".exe"):
        return
    parts = [part for part in env_vars.get("WSLENV", "").split(":") if part]
    seen = {part.split("/")[0] for part in parts}
    for name, suffix in _wslenv_entries(env_vars, seen):
        parts.append(f"{name}{suffix}")
        seen.add(name)
    env_vars["WSLENV"] = ":".join(parts)


def _wslenv_entries(env_vars: Mapping[str, str], seen: set[str]) -> Iterator[tuple[str, str]]:
    """The ``(name, flag)`` entries to append to ``WSLENV``, in order."""
    for name in WSLENV_PATH_VARS:
        if name in env_vars and name not in seen:
            yield name, "/p"
    for name in WSLENV_PLAIN_VARS:
        if name in env_vars and name not in seen:
            yield name, ""


def _run_client(
    inv: Invocation,
    verb: str,
    argv: Sequence[str],
    tail: deque[str],
    server_url: str | None,
) -> int:
    """Start the thin client in its own process group and wait for it."""
    env_vars = runner_env(inv.home, inv.package)
    if server_url is not None:
        env_vars[SERVER_URL_ENV] = server_url
    _maybe_add_wslenv(inv, env_vars)
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env_vars,
            start_new_session=True,
        )
    except OSError as exc:
        raise _failure(inv, tail, f"cannot start the runner: {exc}") from exc
    assert process.stdout is not None
    reader = threading.Thread(target=_stream, args=(process.stdout, tail), daemon=True)
    reader.start()
    try:
        return _wait(inv, process, verb, tail)
    finally:
        reader.join(timeout=KILL_GRACE_S)


# --- the server: started for one verb, killed before the call returns.


@contextmanager
def _server_session(inv: Invocation) -> Iterator[str | None]:
    """Yield the server's base URL, starting and stopping it around the call."""
    server = inv.package.server
    if server is None:
        yield None
        return
    port = _free_port()
    health_url = _substitute(server.health_url, ((PORT_TOKEN, str(port)),))
    base_url = _base_url(health_url)
    if _healthy(health_url):
        yield base_url
        return
    if not inv.start_server:
        yield None
        return
    process, _tail = _launch_server(inv, port, health_url)
    try:
        yield base_url
    finally:
        _kill_group(process)


def _free_port() -> int:
    """A port the kernel has just called free on the loopback interface."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _base_url(url: str) -> str:
    """The scheme and network location of ``url``, with no path or query."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _healthy(url: str) -> bool:
    """True when ``url`` answers 2xx within the short health timeout."""
    try:
        with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT_S) as response:
            status: int = response.status
            return HEALTHY_STATUS[0] <= status < HEALTHY_STATUS[1]
    except (OSError, ValueError):
        return False


def _launch_server(
    inv: Invocation, port: int, url: str
) -> tuple[subprocess.Popen[str], deque[str]]:
    """Start the launch argv in its own group and wait for the health endpoint."""
    from facetmedia import env as env_module  # noqa: PLC0415

    server = inv.package.server
    assert server is not None
    argv = [_substitute(token, ((PORT_TOKEN, str(port)),)) for token in server.launch]
    checkout = env_module.server_dir(inv.package, inv.home)
    env_vars = _server_env(inv, port)
    logger.info("server: %s (cwd %s)", " ".join(argv), checkout)
    tail: deque[str] = deque(maxlen=TAIL_LINES)
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(checkout),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env_vars,
            start_new_session=True,
        )
    except OSError as exc:
        raise _failure(inv, tail, f"cannot start the server: {exc}") from exc
    assert process.stdout is not None
    reader = threading.Thread(target=_stream, args=(process.stdout, tail), daemon=True)
    reader.start()
    try:
        healthy = _await_healthy(process, url, server.timeout_s)
    except BaseException:
        # A signal during startup must still leave no server process behind.
        _kill_group(process)
        reader.join(timeout=KILL_GRACE_S)
        raise
    if healthy:
        return process, tail
    _kill_group(process)
    reader.join(timeout=KILL_GRACE_S)
    raise _failure(
        inv,
        tail,
        f"the server did not become healthy at {url} within {server.timeout_s} s",
    )


def _server_env(inv: Invocation, port: int) -> dict[str, str]:
    """The launch environment: the server venv first on ``PATH``, ``PORT`` set."""
    from facetmedia import env as env_module  # noqa: PLC0415

    env_vars = runner_env(inv.home, inv.package)
    venv_bin = str(env_module.server_venv(inv.package, inv.home) / "bin")
    env_vars["PATH"] = venv_bin + os.pathsep + env_vars.get("PATH", "")
    env_vars[SERVER_PORT_ENV] = str(port)
    env_vars.pop(SERVER_URL_ENV, None)
    server = inv.package.server
    assert server is not None
    data_dir = inv.home.data_dir(inv.package.name)
    for key, value in server.env:
        if DATA_TOKEN in value:
            data_dir.mkdir(parents=True, exist_ok=True)
        env_vars[key] = _substitute(value, ((DATA_TOKEN, str(data_dir)), (PORT_TOKEN, str(port))))
    return env_vars


def _await_healthy(process: subprocess.Popen[str], url: str, timeout_s: float) -> bool:
    """Poll the health endpoint until it answers or the launch dies or times out."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        if _healthy(url):
            return True
        time.sleep(SERVER_POLL_S)
    return _healthy(url)


def parse_run_result(
    result: Mapping[str, object], temp_dir: Path
) -> tuple[dict[str, str], list[RunAsset]]:
    """``(tool, assets)`` from a ``run`` result; every file must be in temp."""
    tool = _tool(result.get("tool"), temp_dir)
    listed = result.get("assets")
    if not isinstance(listed, list):
        raise FacetError(
            "runner_failed", "the run result has no assets list", details={"temp": str(temp_dir)}
        )
    return tool, [_run_asset(item, temp_dir) for item in listed]


def parse_verify_result(
    result: Mapping[str, object],
    assets: Sequence[RunAsset],
    temp_dir: Path,
) -> dict[str, VerifyResult]:
    """The verdict per file; an asset without one is ``runner_failed``."""
    listed = result.get("assets")
    if not isinstance(listed, list):
        raise FacetError(
            "runner_failed",
            "the verify result has no assets list",
            details={"temp": str(temp_dir)},
        )
    verdicts: dict[str, VerifyResult] = {}
    for item in listed:
        entry = _verify_entry(item, temp_dir)
        verdicts[entry.file] = entry
    for asset in assets:
        if asset.file not in verdicts:
            raise FacetError(
                "runner_failed",
                f"the verify result misses {asset.file}",
                details={"temp": str(temp_dir), "expected": [asset.file for asset in assets]},
            )
    return verdicts


# --- running the process.


@contextmanager
def _trapped() -> Iterator[None]:
    """Turn SIGINT/SIGTERM into an exception in this thread while inside."""

    def interrupted(signum: int, _frame: object) -> None:
        raise RunnerInterruptedError(signum)

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _stream(stream: IO[str], tail: deque[str]) -> None:
    """Drain the runner's merged output into the tail, stderr and the log."""
    with stream:
        for line in stream:
            tail.append(line)
            sys.stderr.write(line)
            logger.info("%s", line.rstrip("\n"))


def _wait(inv: Invocation, process: subprocess.Popen[str], verb: str, tail: deque[str]) -> int:
    try:
        if inv.timeout_s is None:
            return process.wait()
        return process.wait(timeout=inv.timeout_s)
    except subprocess.TimeoutExpired:
        _kill_group(process)
        raise _failure(inv, tail, f"the runner did not finish within {inv.timeout_s} s") from None
    except RunnerInterruptedError:
        _kill_group(process)
        raise _failure(
            inv, tail, f"the {verb} run was interrupted; the runner was terminated"
        ) from None


def _kill_group(process: subprocess.Popen[str]) -> None:
    """SIGTERM the runner's process group, SIGKILL it after the grace period."""
    pgid = process.pid
    _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + KILL_GRACE_S
    while _group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(POLL_S)
    _signal_group(pgid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=KILL_GRACE_S)


def _signal_group(pgid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, sig)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --- reading the result file and the two parsers.


def _read_result(result_path: Path, inv: Invocation, tail: deque[str]) -> Mapping[str, object]:
    try:
        text = result_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _failure(inv, tail, f"the runner wrote no result file ({exc})") from exc
    try:
        loaded: object = json.loads(text)
    except ValueError as exc:
        raise _failure(inv, tail, f"the result file is not JSON ({exc})") from exc
    if not isinstance(loaded, dict):
        raise _failure(inv, tail, "the result file is not a JSON object")
    return loaded


def _tool(value: object, temp_dir: Path) -> dict[str, str]:
    if not isinstance(value, Mapping) or not isinstance(value.get("name"), str):
        raise _protocol(temp_dir, "the run result has no tool.name")
    version = value.get("version")
    return {"name": value["name"], "version": version if isinstance(version, str) else ""}


def _run_asset(item: object, temp_dir: Path) -> RunAsset:
    if not isinstance(item, Mapping):
        raise _protocol(temp_dir, "a run asset is not a JSON object")
    file = _bare_name(item.get("file"), temp_dir)
    return RunAsset(
        file=file,
        name=_text(item.get("name"), temp_dir, file, "name"),
        kind=_text(item.get("kind"), temp_dir, file, "kind"),
        mime=_text(item.get("mime"), temp_dir, file, "mime"),
        properties=_object_map(item.get("properties"), temp_dir, file, "properties"),
        params=_object_map(item.get("params"), temp_dir, file, "params"),
        seed=_seed(item.get("seed"), temp_dir, file),
        inputs=_paths(item.get("inputs"), temp_dir, file),
    )


def _bare_name(value: object, temp_dir: Path) -> str:
    """A ``file`` is one bare name that exists in temp — never a path."""
    if not isinstance(value, str) or not value or Path(value).name != value:
        raise _protocol(temp_dir, f"asset file must be a bare name, got {value!r}")
    if value.startswith(RESERVED_PREFIX):
        raise _protocol(
            temp_dir, f"asset file {value!r} uses the reserved {RESERVED_PREFIX} prefix"
        )
    if not (temp_dir / value).is_file():
        raise _protocol(temp_dir, f"the runner listed {value}, which is not in {temp_dir}")
    return value


def _text(value: object, temp_dir: Path, file: str, key: str) -> str:
    if not isinstance(value, str) or not value:
        raise _protocol(temp_dir, f"asset {file} has no {key}")
    return value


def _object_map(value: object, temp_dir: Path, file: str, key: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _protocol(temp_dir, f"asset {file} has no {key} object")
    return {str(name): item for name, item in value.items()}


def _seed(value: object, temp_dir: Path, file: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise _protocol(temp_dir, f"asset {file} has a seed that is not null or an integer")


def _paths(value: object, temp_dir: Path, file: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _protocol(temp_dir, f"asset {file} has inputs that are not a list of paths")
    return tuple(value)


def _verify_entry(item: object, temp_dir: Path) -> VerifyResult:
    if not isinstance(item, Mapping):
        raise _protocol(temp_dir, "a verify asset is not a JSON object")
    file = item.get("file")
    passed = item.get("passed")
    if not isinstance(file, str) or not file:
        raise _protocol(temp_dir, "a verify asset has no file")
    if not isinstance(passed, bool):
        raise _protocol(temp_dir, f"verify asset {file} has no boolean passed")
    verifier = item.get("verifier")
    return VerifyResult(
        file=file,
        passed=passed,
        metrics=_loose_map(item.get("metrics")),
        thresholds=_loose_map(item.get("thresholds")),
        verifier=verifier if isinstance(verifier, str) else "",
    )


def _loose_map(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _protocol(temp_dir: Path, message: str) -> FacetError:
    """A runner that broke the protocol, with the temp dir to inspect."""
    return FacetError("runner_failed", message, details={"temp": str(temp_dir)})


def _details(inv: Invocation, output: str) -> dict[str, object]:
    return {
        "output": output,
        "log": str(log_file(inv.run_id)),
        "temp": str(inv.temp_dir),
    }


def _failure(inv: Invocation, tail: deque[str], message: str) -> FacetError:
    """The ``runner_failed`` failure carrying output tail, log and temp dir."""
    return FacetError("runner_failed", message, details=_details(inv, "".join(tail)))
