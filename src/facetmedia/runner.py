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
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Final

from facetmedia.contract import FacetError
from facetmedia.home import Home, log_file, uv_env
from facetmedia.registry import PROTOCOL_VERSION, Package

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
#: How many lines of runner output are kept for a failure report.
TAIL_LINES: Final[int] = 40
#: Grace between SIGTERM and SIGKILL when a runner must die.
KILL_GRACE_S: Final[float] = 10.0
POLL_S: Final[float] = 0.1
RESERVED_PREFIX: Final[str] = ".facet-"


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
    env_dir: Path,
    verb: str,
    input_path: Path,
    result_path: Path,
) -> list[str]:
    """The runner command: the manifest template with its tokens substituted."""
    replacements = (
        ("{{python}}", str(env_dir / "venv" / "bin" / "python")),
        ("{{entry}}", str(env_dir / "package" / package.entry)),
        ("{{verb}}", verb),
        ("{{input}}", str(input_path)),
        ("{{result}}", str(result_path)),
    )
    return [_substitute(token, replacements) for token in shlex.split(package.invocation_template)]


def _substitute(token: str, replacements: Sequence[tuple[str, str]]) -> str:
    for placeholder, value in replacements:
        token = token.replace(placeholder, value)
    return token


def model_paths(package: Package, home: Home) -> dict[str, Path]:
    """``{model id: absolute path under models/direct}`` for the request."""
    paths: dict[str, Path] = {}
    for model in package.models:
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
        "out_dir": str(out_dir),
        "piece_dir": str(piece_dir),
        "models": {key: str(models[key]) for key in sorted(models)},
        "assets": listed,
    }


def runner_env(home: Home, package: Package) -> dict[str, str]:
    """The runner's environment: uv's, plus the models dir and the GPU flag."""
    env_vars = uv_env(home)
    env_vars["FACET_MODELS"] = str(home.models_dir / "direct")
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
    argv = build_argv(inv.package, inv.env_dir, verb, input_path, result_path)
    logger.info("runner %s: %s", verb, " ".join(argv))
    tail: deque[str] = deque(maxlen=TAIL_LINES)
    # The signal trap is installed before the process starts, so a signal that
    # arrives the instant the runner appears is still ours to clean up.
    with _trapped():
        try:
            process = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=runner_env(inv.home, inv.package),
                start_new_session=True,
            )
        except OSError as exc:
            raise _failure(inv, tail, f"cannot start the runner: {exc}") from exc
        assert process.stdout is not None
        reader = threading.Thread(target=_stream, args=(process.stdout, tail), daemon=True)
        reader.start()
        try:
            returncode = _wait(inv, process, verb, tail)
        finally:
            reader.join(timeout=KILL_GRACE_S)
    if returncode != 0:
        raise _failure(inv, tail, f"runner {verb} failed with exit code {returncode}")
    return Outcome(_read_result(result_path, inv, tail), "".join(tail))


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
