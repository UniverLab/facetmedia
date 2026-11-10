"""The repo-based server engine: declared, pulled, run for one verb, killed.

The fixture server lives in a scratch git repository.  The manifest carries a
syntactically valid https ``repo`` URL; git's ``url.<base>.insteadOf`` (set
through ``GIT_CONFIG_*``) points it at the scratch checkout, so no packet
leaves the machine and no global config is touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import uv

from facetmedia import doctor as doctor_module
from facetmedia import env as env_module
from facetmedia import prune as prune_module
from facetmedia import runner as runner_module
from facetmedia.cli import main
from facetmedia.home import Home
from facetmedia.registry import load_registry, validate_registry
from facetmedia.registry_types import Server

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
REPO_URL = "https://example.invalid/tool.git"
EXIT_RUNNER_FAILED = 9
EXIT_INVALID = 2
SHORT_REV = 12
PAYLOAD_BYTES = 16

SERVE_PY = '''"""The fixture server: a /health endpoint on the port facet picked."""

from __future__ import annotations

import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT_ARG = 2


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b'{"ok": true}'
        status = 200 if self.path == "/health" else 404
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = b'{"ok": true}'
        status = 200 if self.path == "/job" else 405
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[PORT_ARG])), Handler).serve_forever()
'''

CLIENT_RUNNER = '''"""The thin client: it only talks to the server facet started for it."""

from __future__ import annotations

import os
import urllib.request
from typing import Any

from facet_runner_sdk import Request, Runner

runner = Runner()


@runner.verb("run")
def run(req: Request) -> dict[str, Any]:
    base = os.environ["FACET_SERVER_URL"]

    def _call(path: str, data: bytes | None = None) -> tuple[int, bytes]:
        request = urllib.request.Request(
            base + path, data=data, method="POST" if data else "GET"
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    health_status, _health_body = _call("/health")
    job_status, _job_body = _call("/job", b"{}")
    if not (200 <= health_status < 300 and 200 <= job_status < 300):
        raise SystemExit(1)
    (req.out_dir / "probe.txt").write_text(
        f"{base} {health_status} {job_status}", encoding="utf-8"
    )
    return {
        "tool": {"name": "probe", "version": "0.1.0"},
        "assets": [
            {
                "file": "probe.txt",
                "name": "probe",
                "kind": "text",
                "mime": "text/plain",
                "properties": {},
                "params": {key: str(value) for key, value in req.inputs.items()},
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
                "passed": True,
                "metrics": {},
                "thresholds": {},
                "verifier": "probe",
            }
            for path in req.assets
        ]
    }


@runner.verb("doctor")
def doctor(req: Request) -> dict[str, Any]:
    return {"checks": [{"name": "runner", "ok": True, "detail": "ready", "fix": ""}]}


runner.main()
'''

MANIFEST = """name = "{name}"
version = "0.1.0"
requires_facet = ">=0.1.0b1,<0.2"
protocol = 1
provider = "facet"
tool_name = "Probe"
description = "A thin client for a per-run server."
facet = "text"

[engine]
type = "python-env"
python = "3.12"

[engine.server]
repo = "{repo}"
rev = "{rev}"
requirements = "requirements.txt"
launch = {launch}
health_url = "http://127.0.0.1:{{{{port}}}}/health"
timeout_s = {timeout_s}

[transport]
type = "cli"
entry = "runner.py"

[requirements]
gpu = false
ram_gb = 0.1
disk_gb = 0.01
exclusive = false
platforms = ["linux"]
host_libs = []
host_hint = ""

[[licenses]]
subject = "probe"
spdx = "Apache-2.0"

[capabilities.health]
summary = "Report what the per-run server answered."
kind = "text"

[capabilities.health.preview]
viewer = "generic"
"""

HEALTHY_LAUNCH = '["python", "serve.py", "--port", "{{port}}"]'
SICK_LAUNCH = '["python", "-c", "import time; time.sleep(60)", "{{port}}"]'


@dataclass(frozen=True)
class World:
    """The scratch remote, the registry copy and the home all tests share."""

    registry: Path
    remote: Path
    rev: str
    home: Path
    git_env: dict[str, str]


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )


def _make_remote(remote: Path) -> str:
    """A real git repository holding the fixture server, at one real commit."""
    remote.mkdir(parents=True)
    (remote / "requirements.txt").write_text("", encoding="utf-8")
    (remote / "serve.py").write_text(SERVE_PY, encoding="utf-8")
    (remote / "README.md").write_text("probe\n", encoding="utf-8")
    _git(remote, "init", "--quiet")
    _git(remote, "add", "-A")
    _git(
        remote,
        "-c",
        "user.name=facet-tests",
        "-c",
        "user.email=facet@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "the fixture server",
    )
    return _git(remote, "rev-parse", "HEAD").stdout.strip()


def _git_env(remote: Path) -> dict[str, str]:
    """Rewrite the manifest's https URL to the scratch repository, in process only."""
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.{remote.as_uri()}.insteadOf",
        "GIT_CONFIG_VALUE_0": REPO_URL,
    }


def _add_package(registry: Path, name: str, rev: str, launch: str, timeout_s: int) -> None:
    package_dir = registry / "packages" / name
    package_dir.mkdir(parents=True)
    (package_dir / "package.toml").write_text(
        MANIFEST.format(name=name, repo=REPO_URL, rev=rev, launch=launch, timeout_s=timeout_s),
        encoding="utf-8",
    )
    for shared_file in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(FIXTURES / "packages" / "echo" / shared_file, package_dir / shared_file)
    (package_dir / "runner.py").write_text(CLIENT_RUNNER, encoding="utf-8")
    index = registry / "index.toml"
    index.write_text(
        index.read_text(encoding="utf-8") + f'\n[[packages]]\nname = "{name}"\n',
        encoding="utf-8",
    )


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> World:
    root = tmp_path_factory.mktemp("server-world")
    remote = root / "remote"
    rev = _make_remote(remote)
    registry = root / "registry"
    shutil.copytree(FIXTURES, registry)
    _add_package(registry, "probe", rev, HEALTHY_LAUNCH, 20)
    _add_package(registry, "sick", rev, SICK_LAUNCH, 2)
    return World(
        registry=registry,
        remote=remote,
        rev=rev,
        home=root / "facet-home",
        git_env=_git_env(remote),
    )


def _apply(world: World) -> None:
    """Point facet and git at this module's scratch world."""
    os.environ["FACET_HOME"] = str(world.home)
    os.environ["FACET_REGISTRY"] = str(world.registry)
    os.environ.update(world.git_env)


@pytest.fixture(scope="module")
def seed_home(world: World) -> Path:
    """One home with the healthy ``probe`` package already pulled."""
    names = ("FACET_HOME", "FACET_REGISTRY", *world.git_env)
    previous = {name: os.environ.get(name) for name in names}
    _apply(world)
    try:
        assert main(["pull", "probe", "--json"]) == 0
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return world.home


@pytest.fixture
def server_home(
    seed_home: Path, world: World, monkeypatch: pytest.MonkeyPatch, _facet_home_tmp: None
) -> Home:
    """The shared home, after the per-test temp home has been set and replaced."""
    monkeypatch.setenv("FACET_HOME", str(seed_home))
    monkeypatch.setenv("FACET_REGISTRY", str(world.registry))
    for name, value in world.git_env.items():
        monkeypatch.setenv(name, value)
    return Home.from_env()


def _pull(capsys: pytest.CaptureFixture[str], name: str) -> dict[str, Any]:
    code = main(["pull", name, "--json"])
    envelope = json.loads(capsys.readouterr().out)
    assert code == 0, envelope
    data = envelope["data"]
    assert isinstance(data, dict)
    return data


def _listener_closed(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


# --- pull: the client environment first, then the pinned server checkout.


def test_pull_installs_the_client_and_its_verified_server_checkout(
    capsys: pytest.CaptureFixture[str], server_home: Home, world: World
) -> None:
    package = load_registry(world.registry).packages["probe"]
    checkout = env_module.server_dir(package, server_home)

    _pull(capsys, "probe")

    assert checkout.name == world.rev[:SHORT_REV]
    assert (checkout / "serve.py").is_file()
    assert (checkout / "venv" / "bin" / "python").is_file()
    requirements = (checkout / "requirements.txt").read_bytes()
    marker = (checkout / ".complete").read_text(encoding="utf-8")
    assert marker.strip() == f"{world.rev}\n{hashlib.sha256(requirements).hexdigest()}"
    client = server_home.env_dir("probe", "0.1.0")
    assert (client / ".complete").read_text(encoding="utf-8") == package.lock_sha256
    assert env_module.is_installed(package, server_home) is True
    version = subprocess.run(
        [
            str(checkout / "venv" / "bin" / "python"),
            "-c",
            "import sys; print(sys.version_info[:2])",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert version.returncode == 0
    assert version.stdout.strip() == "(3, 12)"


def test_pull_skips_a_verified_checkout_and_rebuilds_after_removal(
    capsys: pytest.CaptureFixture[str], server_home: Home, world: World
) -> None:
    package = load_registry(world.registry).packages["probe"]
    checkout = env_module.server_dir(package, server_home)
    sentinel = checkout / "sentinel"
    sentinel.write_text("left by the first pull", encoding="utf-8")

    data = _pull(capsys, "probe")
    assert data["already_installed"] is True
    assert sentinel.is_file()

    shutil.rmtree(checkout)
    assert env_module.is_installed(package, server_home) is False

    data = _pull(capsys, "probe")
    assert data["already_installed"] is False
    assert not sentinel.exists()
    assert (checkout / "serve.py").is_file()
    assert (checkout / ".complete").is_file()
    assert env_module.is_installed(package, server_home) is True


# --- run: start, healthy, the client verb, and nothing left listening.


def test_run_starts_the_server_runs_the_client_and_kills_the_process_group(
    capsys: pytest.CaptureFixture[str], server_home: Home, tmp_path: Path
) -> None:
    piece = tmp_path / "piece.toml"
    piece.write_text("[inputs]\n", encoding="utf-8")
    out = tmp_path / "out"

    code = main(["run", "probe.health", str(piece), "--out", str(out), "--json"])
    envelope = json.loads(capsys.readouterr().out)
    assert code == 0, envelope
    data = envelope["data"]
    assert data["pulled"] is False

    url, health_status, job_status = (out / "probe.txt").read_text(encoding="utf-8").split()
    assert health_status == "200"
    assert job_status == "200"
    port = int(url.rsplit(":", 1)[1])
    assert url == f"http://127.0.0.1:{port}"
    assert _listener_closed(port)
    assert list(out.glob(".facet-tmp-*")) == []


def _await_health(port: int) -> None:
    """Wait until GET /health on ``port`` answers 200, or fail after 10 s."""
    deadline = time.monotonic() + 10.0
    url = f"http://127.0.0.1:{port}/health"
    while True:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                low, high = runner_module.HEALTHY_STATUS
                if low <= response.status < high:
                    return
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise AssertionError(f"the fixture server never turned healthy at {url}")
        time.sleep(0.1)


def test_an_already_healthy_server_is_reused(
    capsys: pytest.CaptureFixture[str],
    server_home: Home,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    port = runner_module._free_port()
    script = tmp_path / "serve.py"
    script.write_text(SERVE_PY, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(script), "--port", str(port)], start_new_session=True
    )
    try:
        _await_health(port)
        monkeypatch.setattr(runner_module, "_free_port", lambda: port)
        monkeypatch.setattr(
            runner_module,
            "_launch_server",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("facet launched a second server process")
            ),
        )
        piece = tmp_path / "piece.toml"
        piece.write_text("[inputs]\n", encoding="utf-8")
        out = tmp_path / "out"

        code = main(["run", "probe.health", str(piece), "--out", str(out), "--json"])
        envelope = json.loads(capsys.readouterr().out)
        assert code == 0, envelope

        url, health_status, job_status = (out / "probe.txt").read_text(encoding="utf-8").split()
        assert health_status == "200"
        assert job_status == "200"
        assert url == f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_a_launch_that_never_turns_healthy_fails_runner_failed(
    capsys: pytest.CaptureFixture[str], server_home: Home, world: World, tmp_path: Path
) -> None:
    code = main(["pull", "sick", "--json"])
    assert code == 0
    capsys.readouterr()
    piece = tmp_path / "piece.toml"
    piece.write_text("[inputs]\n", encoding="utf-8")
    out = tmp_path / "out"

    code = main(["run", "sick.health", str(piece), "--out", str(out), "--json"])

    envelope = json.loads(capsys.readouterr().out)
    assert code == EXIT_RUNNER_FAILED
    error = envelope["error"]
    assert error["code"] == "runner_failed"
    assert "did not become healthy" in error["message"]
    assert "/health" in error["message"]


# --- validate: six ways a ``[engine.server]`` table is rejected.

VALID_REV = "a" * 40
SERVER_FIELDS: dict[str, str] = {
    "repo": REPO_URL,
    "rev": VALID_REV,
    "requirements": "requirements.txt",
    "launch": '["python", "serve.py", "--port", "{{port}}"]',
    "health_url": "http://127.0.0.1:{{port}}/health",
}


STRING_FIELDS = frozenset({"repo", "rev", "requirements", "health_url"})


def _server_block(*, omit: str = "", **overrides: str) -> str:
    values = {**SERVER_FIELDS, **overrides}
    lines = ["", "[engine.server]"]
    for key, value in values.items():
        if key == omit:
            continue
        lines.append(f'{key} = "{value}"' if key in STRING_FIELDS else f"{key} = {value}")
    return "\n".join(lines) + "\n"


def _server_manifest(
    tmp_path: Path, *, omit: str = "", engine_type: str = "python-env", **overrides: str
) -> tuple[Path, set[tuple[str, str, str]]]:
    """The fixture registry with echo's manifest carrying a ``[engine.server]``."""
    root = tmp_path / "registry"
    shutil.copytree(FIXTURES, root)
    manifest = root / "packages" / "echo" / "package.toml"
    body = manifest.read_text(encoding="utf-8")
    if engine_type != "python-env":
        body = body.replace('[engine]\ntype = "python-env"', f'[engine]\ntype = "{engine_type}"')
    body += _server_block(omit=omit, **overrides)
    manifest.write_text(body, encoding="utf-8")
    found = {
        (problem["file"], problem["key"], problem["problem"]) for problem in validate_registry(root)
    }
    return root, found


def test_validate_accepts_a_server_and_the_runtime_receives_it(tmp_path: Path) -> None:
    root, found = _server_manifest(tmp_path)

    assert found == set()
    package = load_registry(root).packages["echo"]
    assert package.server == Server(
        repo=REPO_URL,
        rev=VALID_REV,
        requirements="requirements.txt",
        launch=("python", "serve.py", "--port", "{{port}}"),
        health_url="http://127.0.0.1:{{port}}/health",
        timeout_s=120,
    )
    assert main(["registry", "validate", str(root)]) == 0


def test_validate_rejects_a_server_on_a_non_python_env_engine(tmp_path: Path) -> None:
    _root, found = _server_manifest(tmp_path, engine_type="server")

    assert (
        "packages/echo/package.toml",
        "engine.server",
        "requires python-env with transport cli",
    ) in found


def test_validate_rejects_a_non_https_repo(tmp_path: Path) -> None:
    _root, found = _server_manifest(tmp_path, repo="http://example.invalid/tool.git")

    assert ("packages/echo/package.toml", "engine.server.repo", "value") in found


def test_validate_rejects_a_rev_that_is_not_40_hex(tmp_path: Path) -> None:
    _root, found = _server_manifest(tmp_path, rev="main")

    assert ("packages/echo/package.toml", "engine.server.rev", "value") in found


def test_validate_rejects_a_missing_requirements_file_name(tmp_path: Path) -> None:
    _root, found = _server_manifest(tmp_path, omit="requirements")

    assert ("packages/echo/package.toml", "engine.server.requirements", "required") in found


def test_validate_rejects_a_launch_without_the_port_token(tmp_path: Path) -> None:
    _root, found = _server_manifest(tmp_path, launch='["python", "serve.py"]')

    assert (
        "packages/echo/package.toml",
        "engine.server.launch",
        "value: needs {{port}}",
    ) in found


def test_validate_rejects_a_health_url_without_scheme(tmp_path: Path) -> None:
    _root, found = _server_manifest(tmp_path, health_url="127.0.0.1:{{port}}/health")

    assert ("packages/echo/package.toml", "engine.server.health_url", "value") in found


# --- build: a uv.lock server syncs the locked project; anything else pip-installs.


def test_server_build_from_a_uv_lock_runs_one_frozen_sync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(env_module, "_stream_output", lambda argv, env: calls.append((argv, env)))
    package = load_registry(FIXTURES).packages["echo"]
    server = Server(
        REPO_URL, "a" * 40, "uv.lock", ("python", "{{port}}"), "http://127.0.0.1:{{port}}/health"
    )
    target = tmp_path / "checkout"

    env_module._server_build(Home.from_env(), target, package, server)

    assert len(calls) == 1
    argv, env = calls[0]
    assert argv == [
        uv.find_uv_bin(),
        "sync",
        "--frozen",
        "--no-dev",
        "--project",
        str(target),
        "--python",
        package.python,
    ]
    assert env["UV_PROJECT_ENVIRONMENT"] == str(target / "venv")
    assert not any("pip" in token and token == "pip" for token in argv[1:3])


def test_server_build_from_a_requirements_file_keeps_venv_and_pip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(env_module, "_stream_output", lambda argv, env: calls.append((argv, env)))
    package = load_registry(FIXTURES).packages["echo"]
    server = Server(
        REPO_URL,
        "a" * 40,
        "requirements.txt",
        ("python", "{{port}}"),
        "http://127.0.0.1:{{port}}/health",
    )
    target = tmp_path / "checkout"
    uv_bin = uv.find_uv_bin()

    env_module._server_build(Home.from_env(), target, package, server)

    assert [argv for argv, _ in calls] == [
        [uv_bin, "venv", "--python", package.python, str(target / "venv")],
        [
            uv_bin,
            "pip",
            "install",
            "--python",
            str(target / "venv" / "bin" / "python"),
            "-r",
            str(target / "requirements.txt"),
        ],
    ]
    assert all("UV_PROJECT_ENVIRONMENT" not in env for _, env in calls)


def test_validate_accepts_a_uv_lock_requirements(tmp_path: Path) -> None:
    _root, found = _server_manifest(tmp_path, requirements="uv.lock")

    assert found == set()


# --- prune: server checkouts join the orphans contract and the clean sweep.


def _plant_checkout(home: Home, name: str, version: str, rev: str) -> Path:
    target = home.servers_dir / name / version / rev
    target.mkdir(parents=True)
    (target / "payload").write_bytes(b"x" * PAYLOAD_BYTES)
    return target


def test_orphaned_server_checkouts_are_listed_then_freed_by_clean(
    capsys: pytest.CaptureFixture[str],
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("FACET_HOME", str(tmp_path / "facet-home"))
    monkeypatch.setenv("FACET_REGISTRY", str(world.registry))
    home = Home.from_env()
    registry = load_registry(world.registry)
    gone = _plant_checkout(home, "gone", "1.0.0", "b" * SHORT_REV)
    version_dir = gone.parent

    items = [item for item in prune_module.orphans(home, registry) if item.path == version_dir]
    assert [item.reason for item in items] == ["orphan-package"]

    code = main(["clean", "--dry-run", "--json"])
    dry = json.loads(capsys.readouterr().out)["data"]
    assert code == 0
    assert str(version_dir) in {item["path"] for item in dry["items"]}
    assert gone.is_dir()

    code = main(["clean", "--json"])
    real = json.loads(capsys.readouterr().out)["data"]
    assert code == 0
    assert str(version_dir) in {item["path"] for item in real["items"]}
    assert not gone.exists()


def test_a_server_checkout_without_its_marker_is_incomplete(
    capsys: pytest.CaptureFixture[str],
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("FACET_HOME", str(tmp_path / "facet-home"))
    monkeypatch.setenv("FACET_REGISTRY", str(world.registry))
    home = Home.from_env()
    registry = load_registry(world.registry)
    unfinished = _plant_checkout(home, "probe", "0.1.0", world.rev[:SHORT_REV])

    reasons = {item.path: item.reason for item in prune_module.orphans(home, registry)}
    assert reasons[unfinished.parent] == "incomplete-server"

    code = main(["clean", "--dry-run", "--json"])
    assert code == 0
    capsys.readouterr()


def test_removing_a_package_frees_its_whole_server_tree(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FACET_HOME", str(world.home / "remove-home"))
    monkeypatch.setenv("FACET_REGISTRY", str(world.registry))
    home = Home.from_env()
    checkout = _plant_checkout(home, "probe", "0.1.0", world.rev[:SHORT_REV])

    result = prune_module.rm_package(home, "probe")

    assert not checkout.exists()
    assert not (home.servers_dir / "probe").exists()
    assert str(home.servers_dir / "probe") in result["removed"]
    assert int(result["freed_bytes"]) >= PAYLOAD_BYTES


# --- the package doctor: the checkout is reported, never started.


def test_the_package_doctor_reports_a_missing_checkout_without_starting_it(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FACET_HOME", str(tmp_path / "facet-home"))
    monkeypatch.setenv("FACET_REGISTRY", str(world.registry))
    home = Home.from_env()
    package = load_registry(world.registry).packages["probe"]
    env_dir = home.env_dir("probe", "0.1.0")
    env_dir.mkdir(parents=True)
    (env_dir / ".complete").write_text(package.lock_sha256, encoding="utf-8")

    checks, failure = doctor_module.package_doctor(
        package, home, "run-doctor", "quick", "probe.health"
    )

    assert failure is None
    assert checks == [
        {
            "name": "server",
            "ok": False,
            "detail": f"{env_module.server_dir(package, home)}: the checkout is not verified",
            "fix": "facet pull probe",
        }
    ]


def test_validate_reports_exit_2_for_a_broken_server(tmp_path: Path) -> None:
    root, _found = _server_manifest(tmp_path, repo="http://example.invalid/tool.git")

    assert main(["registry", "validate", str(root)]) == EXIT_INVALID
