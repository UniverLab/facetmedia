"""``facet run`` end to end, plus unit tests for the runner protocol.

The end-to-end tests share one pulled environment for the whole module, built
once by the ``seed_home`` fixture: the runner variants below only replace
``runner.py`` and never touch ``uv.lock``, so no test triggers a second
``uv sync``.  The kill and timeout tests run facet as a real subprocess whose
runner reports its process group before sleeping.
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from facetmedia import doctor as doctor_module
from facetmedia import record, runner, runner_sdk
from facetmedia.cli import main
from facetmedia.contract import FacetError
from facetmedia.home import Home
from facetmedia.registry import load_registry

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
EXIT_INVALID_INPUT = 2
EXIT_VERIFICATION_FAILED = 3
EXIT_MACHINE_CANNOT = 5
EXIT_NOT_INSTALLED = 7
EXIT_RUNNER_FAILED = 9
SEED = 7

#: The echo runner with a verify verb that always fails.
FAIL_VERIFY_RUNNER = '''"""Echo run; verify always fails."""

from __future__ import annotations

from typing import Any

from facet_runner_sdk import Request, Runner

runner = Runner()


@runner.verb("run")
def run(req: Request) -> dict[str, Any]:
    text = str(req.inputs["text"])
    repeat = int(req.inputs.get("repeat", 1))
    (req.out_dir / "echo.txt").write_text(text * repeat, encoding="utf-8")
    return {
        "tool": {"name": "echo", "version": "0.1.0"},
        "assets": [
            {
                "file": "echo.txt",
                "name": "echo",
                "kind": "text",
                "mime": "text/plain",
                "properties": {},
                "params": dict(req.inputs),
                "seed": None,
                "inputs": [],
            }
        ],
    }


@runner.verb("verify")
def verify(req: Request) -> dict[str, Any]:
    return {
        "assets": [
            {
                "file": path.name,
                "passed": False,
                "metrics": {"match": 0},
                "thresholds": {},
                "verifier": "echo-contrary",
            }
            for path in req.assets
        ]
    }


@runner.verb("doctor")
def doctor(req: Request) -> dict[str, Any]:
    return {"checks": [{"name": "runner", "ok": True, "detail": "ready", "fix": ""}]}


runner.main()
'''

#: A runner whose verify verb exits 0 without ever writing a result file.
SILENT_VERIFY_RUNNER = '''"""Echo run; verify exits 0 but writes no result file."""

from __future__ import annotations

import sys
from typing import Any

from facet_runner_sdk import Request, Runner

runner = Runner()


@runner.verb("run")
def run(req: Request) -> dict[str, Any]:
    text = str(req.inputs["text"])
    repeat = int(req.inputs.get("repeat", 1))
    (req.out_dir / "echo.txt").write_text(text * repeat, encoding="utf-8")
    return {
        "tool": {"name": "echo", "version": "0.1.0"},
        "assets": [
            {
                "file": "echo.txt",
                "name": "echo",
                "kind": "text",
                "mime": "text/plain",
                "properties": {},
                "params": {},
                "seed": None,
                "inputs": [],
            }
        ],
    }


@runner.verb("verify")
def verify(req: Request) -> dict[str, Any]:
    sys.exit(0)


@runner.verb("doctor")
def doctor(req: Request) -> dict[str, Any]:
    return {"checks": [{"name": "runner", "ok": True, "detail": "ready", "fix": ""}]}


runner.main()
'''

#: A runner that prints to stderr and dies before writing any result.
CRASHING_RUNNER = '''"""Echo run that dies before the result file."""

from __future__ import annotations

import sys
from typing import Any

from facet_runner_sdk import Request, Runner

runner = Runner()


@runner.verb("run")
def run(req: Request) -> dict[str, Any]:
    print("boom happened", file=sys.stderr)
    raise SystemExit(1)


@runner.verb("doctor")
def doctor(req: Request) -> dict[str, Any]:
    return {"checks": [{"name": "runner", "ok": True, "detail": "ready", "fix": ""}]}


runner.main()
'''

#: A runner that reports its process group, then sleeps until killed.
SLEEPING_RUNNER = '''"""Echo run that reports its process group and then sleeps."""

from __future__ import annotations

import os
import time
from typing import Any

from facet_runner_sdk import Request, Runner

runner = Runner()


@runner.verb("run")
def run(req: Request) -> dict[str, Any]:
    (req.out_dir / "pgid.txt").write_text(str(os.getpgid(0)), encoding="utf-8")
    time.sleep(600)
    return {"tool": {"name": "echo", "version": "0.1.0"}, "assets": []}


@runner.verb("doctor")
def doctor(req: Request) -> dict[str, Any]:
    return {"checks": [{"name": "runner", "ok": True, "detail": "ready", "fix": ""}]}


runner.main()
'''


# --- fixtures and helpers.


@pytest.fixture(scope="module")
def seed_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One home with echo installed; shared by every test in this module."""
    root = tmp_path_factory.mktemp("facet-home-seed")
    previous = {name: os.environ.get(name) for name in ("FACET_HOME", "FACET_REGISTRY")}
    os.environ["FACET_HOME"] = str(root)
    os.environ["FACET_REGISTRY"] = str(FIXTURES)
    try:
        assert main(["pull", "echo", "--json"]) == 0
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return root


@pytest.fixture
def facet_home(seed_home: Path, monkeypatch: pytest.MonkeyPatch, _facet_home_tmp: None) -> Path:
    """The shared home, after the per-test temp home has been set and replaced."""
    monkeypatch.setenv("FACET_HOME", str(seed_home))
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))
    return seed_home


@pytest.fixture
def swap_echo_runner(facet_home: Path) -> Iterator[Callable[[str], None]]:
    """Replace the installed echo runner for one test, then restore it.

    The runner a run executes is the copy inside the installed environment
    (``<env>/package/runner.py``), not the registry's file: swapping it there
    keeps the environment complete, so no test triggers a second sync.
    """
    target = facet_home / "envs" / "echo" / "0.1.0" / "package" / "runner.py"
    original = target.read_text(encoding="utf-8")

    def swap(source: str) -> None:
        target.write_text(source, encoding="utf-8")

    yield swap
    target.write_text(original, encoding="utf-8")


def _registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))


def _piece(directory: Path, *, text: str = "hello", repeat: int = 2, extra: str = "") -> Path:
    piece = directory / "piece.toml"
    piece.write_text(f'[inputs]\ntext = "{text}"\nrepeat = {repeat}\n{extra}', encoding="utf-8")
    return piece


def _envelope(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    """The single JSON document facet printed on stdout."""
    captured = capsys.readouterr()
    parsed: Any = json.loads(captured.out)
    assert isinstance(parsed, dict)
    return parsed


def _error(envelope: dict[str, Any]) -> dict[str, Any]:
    assert envelope["ok"] is False
    error: dict[str, Any] = envelope["error"]
    return error


def _asset_payload(file_name: str = "echo.txt") -> dict[str, Any]:
    return {
        "file": file_name,
        "name": "echo",
        "kind": "text",
        "mime": "text/plain",
        "properties": {},
        "params": {},
        "seed": None,
        "inputs": [],
    }


# --- the end-to-end paths (FR7).


def test_run_echo_writes_the_asset_and_a_passed_record(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    facet_home: Path,
) -> None:
    _registry(monkeypatch)
    piece = _piece(tmp_path)
    out = tmp_path / "out"

    code = main(["run", "echo.text", str(piece), "--out", str(out), "--json"])
    envelope = _envelope(capsys)
    assert code == 0
    assert envelope["ok"] is True
    data = envelope["data"]
    assert data["capability"] == "echo.text"
    assert data["out_dir"] == str(out)
    assert data["pulled"] is False
    assert data["downloaded_bytes"] == 0
    assert (out / "echo.txt").read_text(encoding="utf-8") == "hellohello"
    assert list(out.glob(".facet-tmp-*")) == []

    stored = json.loads((out / "echo.txt.facet.json").read_text(encoding="utf-8"))
    assert data["assets"] == [stored]
    assert record.verify_integrity(out / "echo.txt") is True
    assert stored["name"] == "echo"
    assert stored["kind"] == "text"
    assert stored["mime"] == "text/plain"
    assert stored["path"] == "echo.txt"
    assert stored["verification"] == {
        "status": "passed",
        "metrics": {"match": 1},
        "thresholds": {},
        "verifier": "echo-content",
    }
    provenance = stored["provenance"]
    package = load_registry(FIXTURES).packages["echo"]
    assert provenance["capability"] == "echo.text"
    assert provenance["package"] == {
        "name": "echo",
        "version": "0.1.0",
        "lock_sha256": package.lock_sha256,
    }
    assert provenance["tool"] == {"name": "echo", "version": "0.1.0"}
    assert provenance["params"] == {"text": "hello", "repeat": 2}
    assert provenance["seed"] is None
    license_section = stored["license"]
    assert license_section["tools"] == [{"subject": "echo", "spdx": "Apache-2.0"}]
    assert license_section["models"] == []
    assert license_section["accepted"] == []
    assert license_section["commercial_use"] is True


def test_run_verify_failure_exits_3_and_keeps_asset_and_record(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    facet_home: Path,
    swap_echo_runner: Callable[[str], None],
) -> None:
    swap_echo_runner(FAIL_VERIFY_RUNNER)
    piece = _piece(tmp_path)
    out = tmp_path / "out"

    code = main(["run", "echo.text", str(piece), "--out", str(out), "--json"])
    envelope = _envelope(capsys)
    assert code == EXIT_VERIFICATION_FAILED
    # The envelope is still ok with every record; the exit code carries the failure.
    assert envelope["ok"] is True
    stored = json.loads((out / "echo.txt.facet.json").read_text(encoding="utf-8"))
    assert envelope["data"]["assets"] == [stored]
    assert (out / "echo.txt").read_text(encoding="utf-8") == "hellohello"
    assert stored["verification"]["status"] == "failed"
    assert stored["verification"]["metrics"] == {"match": 0}
    assert stored["verification"]["verifier"] == "echo-contrary"
    assert record.verify_integrity(out / "echo.txt") is True
    assert list(out.glob(".facet-tmp-*")) == []


def test_run_no_verify_records_skipped_and_default_out_is_the_piece_dir(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    facet_home: Path,
) -> None:
    _registry(monkeypatch)
    piece_dir = tmp_path / "pieces"
    piece_dir.mkdir()
    piece = _piece(piece_dir)

    code = main(["run", "echo.text", str(piece), "--no-verify", "--json"])
    envelope = _envelope(capsys)
    assert code == 0
    assert envelope["data"]["out_dir"] == str(piece_dir)
    stored = json.loads((piece_dir / "echo.txt.facet.json").read_text(encoding="utf-8"))
    assert stored["verification"]["status"] == "skipped"


def test_run_pulls_a_fresh_home_first(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    facet_home: Path,
) -> None:
    monkeypatch.setenv("FACET_HOME", str(tmp_path / "fresh-home"))
    _registry(monkeypatch)
    piece = _piece(tmp_path)
    out = tmp_path / "out"

    code = main(["run", "echo.text", str(piece), "--out", str(out), "--json"])
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert code == 0
    assert envelope["data"]["pulled"] is True
    assert envelope["data"]["downloaded_bytes"] == 0
    # The pull plan is printed before anything runs, on stderr only.
    assert "total to download 0 bytes" in captured.err
    assert (out / "echo.txt").is_file()


def test_run_no_pull_on_a_fresh_home_is_not_installed(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("FACET_HOME", str(tmp_path / "fresh-home"))
    _registry(monkeypatch)
    piece = _piece(tmp_path)

    code = main(["run", "echo.text", str(piece), "--no-pull", "--json"])
    error = _error(_envelope(capsys))
    assert code == EXIT_NOT_INSTALLED
    assert error["code"] == "not_installed"
    assert list((tmp_path / "fresh-home" / "envs").glob("*/*")) == []


def test_run_refuses_a_machine_that_cannot_before_pulling(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("FACET_HOME", str(tmp_path / "fresh-home"))
    _registry(monkeypatch)
    monkeypatch.setattr(doctor_module, "mem_total_gb", lambda: 0.05)
    piece = _piece(tmp_path)

    code = main(["run", "echo.text", str(piece), "--no-pull", "--json"])
    error = _error(_envelope(capsys))
    assert code == EXIT_MACHINE_CANNOT
    assert error["code"] == "machine_cannot"
    # The gate ran before the pull/not-installed decision; nothing was installed.
    assert list((tmp_path / "fresh-home" / "envs").glob("*/*")) == []


def test_run_with_a_bogus_piece_key_is_invalid_input(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    facet_home: Path,
) -> None:
    _registry(monkeypatch)
    piece = _piece(tmp_path, extra='bogus = "nope"\n')

    code = main(["run", "echo.text", str(piece), "--json"])
    error = _error(_envelope(capsys))
    assert code == EXIT_INVALID_INPUT
    assert error["code"] == "invalid_input"
    assert {"field": "bogus", "problem": "unknown"} in error["details"]["errors"]


def test_run_with_an_unknown_capability_lists_the_known_ids(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    facet_home: Path,
) -> None:
    _registry(monkeypatch)
    piece = _piece(tmp_path)

    code = main(["run", "nope.thing", str(piece), "--json"])
    error = _error(_envelope(capsys))
    assert code == EXIT_INVALID_INPUT
    assert "echo.text" in error["details"]["known"]


def test_run_reads_a_json_piece_from_stdin(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    facet_home: Path,
) -> None:
    _registry(monkeypatch)
    out = tmp_path / "out"
    payload = json.dumps({"inputs": {"text": "yo", "repeat": 3}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

    code = main(["run", "echo.text", "-", "--out", str(out), "--json"])
    envelope = _envelope(capsys)
    assert code == 0
    assert envelope["data"]["out_dir"] == str(out)
    assert (out / "echo.txt").read_text(encoding="utf-8") == "yoyoyo"


def test_run_runner_exit_1_is_runner_failed_with_its_output(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    facet_home: Path,
    swap_echo_runner: Callable[[str], None],
) -> None:
    swap_echo_runner(CRASHING_RUNNER)
    piece = _piece(tmp_path)
    out = tmp_path / "out"

    code = main(["run", "echo.text", str(piece), "--out", str(out), "--json"])
    error = _error(_envelope(capsys))
    assert code == EXIT_RUNNER_FAILED
    assert error["code"] == "runner_failed"
    assert "exit code 1" in error["message"]
    assert "boom happened" in error["details"]["output"]
    assert Path(error["details"]["log"]).is_file()
    # The temp dir is kept for inspection.
    assert list(out.glob(".facet-tmp-*/.facet-request")) != []


def test_run_verify_exit_0_without_a_result_is_runner_failed(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    facet_home: Path,
    swap_echo_runner: Callable[[str], None],
) -> None:
    """The run verb's result is never misread as the verify verb's answer."""
    swap_echo_runner(SILENT_VERIFY_RUNNER)
    piece = _piece(tmp_path)
    out = tmp_path / "out"

    code = main(["run", "echo.text", str(piece), "--out", str(out), "--json"])
    error = _error(_envelope(capsys))
    assert code == EXIT_RUNNER_FAILED
    assert error["code"] == "runner_failed"
    assert "wrote no result file" in error["message"]


# --- facet killed mid-run and the runner timeout (FR3, FR7).


def _facet_process(piece: Path, out: Path, extra: list[str], home: Path) -> subprocess.Popen[str]:
    argv = [
        sys.executable,
        "-m",
        "facetmedia",
        "run",
        "echo.text",
        str(piece),
        "--out",
        str(out),
        "--json",
        *extra,
    ]
    return subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ, FACET_HOME=str(home)),
        cwd=str(home),
    )


def _wait_for_pgid(process: subprocess.Popen[str], out: Path) -> int:
    """The runner's process group, once it has reported it; else fail."""
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        matches = list(out.glob(".facet-tmp-*/pgid.txt"))
        if matches:
            return int(matches[0].read_text(encoding="utf-8").strip())
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f"facet exited {process.returncode} early:\n{stdout}\n{stderr}")
        time.sleep(0.2)
    process.kill()
    pytest.fail("the runner never reported its process group")


def _assert_group_gone(pgid: int) -> None:
    """The whole group must be dead within 12 s of the signal (FR7)."""
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)
    pytest.fail(f"the runner's process group {pgid} is still alive")


def test_killing_facet_mid_run_leaves_no_runner_process(
    tmp_path: Path,
    facet_home: Path,
    swap_echo_runner: Callable[[str], None],
) -> None:
    swap_echo_runner(SLEEPING_RUNNER)
    piece = _piece(tmp_path)
    out = tmp_path / "out"

    process = _facet_process(piece, out, [], facet_home)
    pgid = _wait_for_pgid(process, out)
    process.send_signal(signal.SIGTERM)
    stdout, _ = process.communicate(timeout=60)
    assert process.returncode == EXIT_RUNNER_FAILED
    envelope = json.loads(stdout)
    assert envelope["error"]["code"] == "runner_failed"
    _assert_group_gone(pgid)


def test_runner_timeout_kills_the_group_and_exits_9(
    tmp_path: Path,
    facet_home: Path,
    swap_echo_runner: Callable[[str], None],
) -> None:
    swap_echo_runner(SLEEPING_RUNNER)
    piece = _piece(tmp_path)
    out = tmp_path / "out"

    process = _facet_process(piece, out, ["--timeout", "2"], facet_home)
    pgid = _wait_for_pgid(process, out)
    stdout, _ = process.communicate(timeout=120)
    assert process.returncode == EXIT_RUNNER_FAILED
    envelope = json.loads(stdout)
    assert "did not finish within 2" in envelope["error"]["message"]
    _assert_group_gone(pgid)


# --- the runner protocol's unit tests (no subprocess, no network).


def test_build_argv_substitutes_the_default_template() -> None:
    package = load_registry(FIXTURES).packages["echo"]
    argv = runner.build_argv(package, Path("/env"), "run", Path("/tmp/in"), Path("/tmp/out"))
    assert argv == [
        "/env/venv/bin/python",
        "-I",
        "/env/package/runner.py",
        "run",
        "--input",
        "/tmp/in",
        "--result",
        "/tmp/out",
    ]


def test_model_paths_map_ids_onto_models_direct(tmp_path: Path) -> None:
    package = load_registry(FIXTURES).packages["echo"]
    assert runner.model_paths(package, Home(tmp_path)) == {}


def test_make_request_lists_assets_only_for_verify(tmp_path: Path) -> None:
    models = {"m": tmp_path / "m.bin"}
    run_request = runner.make_request("run", "echo.text", {}, tmp_path, tmp_path, models)
    assert run_request["protocol"] == 1
    assert run_request["verb"] == "run"
    assert run_request["assets"] == []
    verify_request = runner.make_request(
        "verify", "echo.text", {}, tmp_path, tmp_path, models, [tmp_path / "x.txt"]
    )
    assert verify_request["assets"] == [str(tmp_path / "x.txt")]


def test_parse_run_result_rejects_a_listed_but_absent_file(tmp_path: Path) -> None:
    result = {"tool": {"name": "echo", "version": "0.1.0"}, "assets": [_asset_payload()]}
    with pytest.raises(FacetError) as excinfo:
        runner.parse_run_result(result, tmp_path)
    assert excinfo.value.code == "runner_failed"
    assert "not in" in excinfo.value.message


@pytest.mark.parametrize(
    "file_name", ["sub/echo.txt", "../echo.txt", "", ".facet-request"], ids=repr
)
def test_parse_run_result_requires_a_plain_bare_name(tmp_path: Path, file_name: str) -> None:
    (tmp_path / "echo.txt").write_text("hello", encoding="utf-8")
    result = {
        "tool": {"name": "echo", "version": "0.1.0"},
        "assets": [_asset_payload(file_name)],
    }
    with pytest.raises(FacetError) as excinfo:
        runner.parse_run_result(result, tmp_path)
    assert excinfo.value.code == "runner_failed"


def test_parse_run_result_parses_a_valid_asset(tmp_path: Path) -> None:
    (tmp_path / "echo.txt").write_text("hello", encoding="utf-8")
    payload = _asset_payload()
    payload["properties"] = {"chars": 5}
    payload["seed"] = SEED
    payload["inputs"] = ["/piece/ref.txt"]
    result = {"tool": {"name": "echo", "version": "0.1.0"}, "assets": [payload]}
    tool, assets = runner.parse_run_result(result, tmp_path)
    assert tool == {"name": "echo", "version": "0.1.0"}
    assert len(assets) == 1
    asset = assets[0]
    assert (asset.file, asset.name, asset.kind, asset.mime) == (
        "echo.txt",
        "echo",
        "text",
        "text/plain",
    )
    assert asset.properties == {"chars": 5}
    assert asset.seed == SEED
    assert asset.inputs == ("/piece/ref.txt",)


@pytest.mark.parametrize(
    "result",
    [
        {"assets": []},
        {"tool": {"name": "echo", "version": "0.1.0"}, "assets": "nope"},
        {"tool": {}, "assets": []},
    ],
    ids=["no-tool", "assets-not-list", "tool-without-name"],
)
def test_parse_run_result_rejects_a_broken_result(tmp_path: Path, result: dict[str, Any]) -> None:
    with pytest.raises(FacetError) as excinfo:
        runner.parse_run_result(result, tmp_path)
    assert excinfo.value.code == "runner_failed"


def test_parse_verify_result_requires_every_asset(tmp_path: Path) -> None:
    asset = runner.RunAsset(file="echo.txt", name="echo", kind="text", mime="text/plain")
    entry = {
        "file": "other.txt",
        "passed": True,
        "metrics": {},
        "thresholds": {},
        "verifier": "v",
    }
    with pytest.raises(FacetError) as excinfo:
        runner.parse_verify_result({"assets": [entry]}, [asset], tmp_path)
    assert excinfo.value.code == "runner_failed"
    assert "misses echo.txt" in excinfo.value.message

    entry["file"] = "echo.txt"
    verdicts = runner.parse_verify_result({"assets": [entry]}, [asset], tmp_path)
    assert verdicts["echo.txt"].passed is True
    assert verdicts["echo.txt"].verifier == "v"


def test_runner_env_hides_the_gpu_when_the_manifest_says_so(tmp_path: Path) -> None:
    package = load_registry(FIXTURES).packages["echo"]
    env_vars = runner.runner_env(Home(tmp_path), package)
    assert env_vars["CUDA_VISIBLE_DEVICES"] == ""
    assert env_vars["FACET_MODELS"].endswith("/models/direct")


# --- the SDK's unit tests.


def _sdk_request(tmp_path: Path) -> Path:
    request_file = tmp_path / "request.json"
    request_file.write_text(
        json.dumps(
            {
                "protocol": 1,
                "verb": "run",
                "capability": "echo.text",
                "inputs": {"text": "hi", "repeat": 2},
                "out_dir": str(tmp_path / "out"),
                "piece_dir": str(tmp_path),
                "models": {"m": str(tmp_path / "models" / "m.bin")},
                "assets": [str(tmp_path / "echo.txt")],
            }
        ),
        encoding="utf-8",
    )
    return request_file


def test_sdk_parses_the_request_and_writes_the_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    request_file = _sdk_request(tmp_path)
    result_file = tmp_path / "result.json"
    sdk = runner_sdk.Runner()

    @sdk.verb("run")
    def handle(req: runner_sdk.Request) -> dict[str, Any]:
        assert req.verb == "run"
        assert req.capability == "echo.text"
        assert req.protocol == 1
        assert req.inputs == {"text": "hi", "repeat": 2}
        assert req.out_dir == tmp_path / "out"
        assert req.piece_dir == tmp_path
        assert req.model_path("m") == tmp_path / "models" / "m.bin"
        assert req.assets == [tmp_path / "echo.txt"]
        return {"ok": True}

    code = sdk.run(["run", "--input", str(request_file), "--result", str(result_file)])
    assert code == 0
    assert capsys.readouterr().out == ""
    assert json.loads(result_file.read_text(encoding="utf-8")) == {"ok": True}


def test_sdk_model_path_names_what_the_piece_has() -> None:
    request = runner_sdk.Request({"models": {}})
    with pytest.raises(KeyError, match="unknown model"):
        request.model_path("missing")


@pytest.mark.parametrize(
    ("argv", "fragment"),
    [
        ([], "usage"),
        (["run", "--result", "r"], "missing --input"),
        (["run", "--input", "i"], "missing --result"),
        (["run", "--bogus", "x"], "usage"),
    ],
    ids=["empty", "no-input", "no-result", "bogus-flag"],
)
def test_sdk_usage_errors_exit_1(
    capsys: pytest.CaptureFixture[str], argv: list[str], fragment: str
) -> None:
    assert runner_sdk.Runner().run(argv) == 1
    assert fragment in capsys.readouterr().err


def test_sdk_unknown_verb_names_the_known_ones(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sdk = runner_sdk.Runner()
    request_file = _sdk_request(tmp_path)
    code = sdk.run(["probe", "--input", str(request_file), "--result", str(tmp_path / "r")])
    assert code == 1
    assert "unknown verb: probe" in capsys.readouterr().err


def test_sdk_handler_exception_is_a_traceback_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    request_file = _sdk_request(tmp_path)
    result_file = tmp_path / "result.json"
    sdk = runner_sdk.Runner()

    @sdk.verb("run")
    def boom(_req: runner_sdk.Request) -> dict[str, Any]:
        raise RuntimeError("kablam")

    code = sdk.run(["run", "--input", str(request_file), "--result", str(result_file)])
    assert code == 1
    captured = capsys.readouterr()
    assert "Traceback" in captured.err
    assert "kablam" in captured.err
    assert not result_file.exists()


def test_sdk_request_file_must_be_an_object(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    request_file = tmp_path / "request.json"
    request_file.write_text("[]", encoding="utf-8")
    result_file = tmp_path / "result.json"
    sdk = runner_sdk.Runner()

    @sdk.verb("run")
    def handle(_req: runner_sdk.Request) -> dict[str, Any]:
        return {}

    code = sdk.run(["run", "--input", str(request_file), "--result", str(result_file)])
    assert code == 1
    assert "not a JSON object" in capsys.readouterr().err
    assert not result_file.exists()
