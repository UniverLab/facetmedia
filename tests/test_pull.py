"""``facet pull`` end to end: real uv, the fixture registry, a temp home.

These tests run the real ``uv sync``, so the first environment of each test
home downloads the managed CPython interpreter (network needed).  The model
fixtures are ``file://`` paths or the loopback ``FakeHF`` server; no model is
ever fetched from the real network.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import shutil
import subprocess
import threading
import zipfile
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from facetmedia import __version__, update
from facetmedia import doctor as doctor_module
from facetmedia import env as env_module
from facetmedia import runner as runner_module
from facetmedia import shared as shared_module
from facetmedia.cli import main
from facetmedia.contract import FacetError
from facetmedia.env import download as env_download
from facetmedia.home import Home
from facetmedia.registry import load_registry
from facetmedia.registry_types import Binary, HostApp
from tests.registry_server import COMMIT, OTHER_COMMIT, FakeGitHub

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
REAL_SHA = "85a73737b5d85eaba43e73ba33ceade28de49f1f4f7573377f02d58408f715c7"
TERMSY_BYTES = 21
EXIT_LICENSE_NOT_ACCEPTED = 4
EXIT_DOWNLOAD_FAILED = 8
EXIT_MACHINE_CANNOT = 5
GB = 1024**3
HF_REPO = "facet/hf-fixture"
HF_REVISION = "0123456789abcdef0123456789abcdef01234567"
HF_CONFIG = b'{"model_type": "fixture"}\n'
HF_WEIGHTS = b"hf-weights-fixture\n"
HF_WEIGHTS_SHA = hashlib.sha256(HF_WEIGHTS).hexdigest()
BROWSER_REVISION = "1248"


def _pull(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, object]]:
    """Run ``facet <argv> --json`` and return (exit code, envelope)."""
    code = main([*argv, "--json"])
    envelope = json.loads(capsys.readouterr().out)
    assert isinstance(envelope, dict)
    return code, envelope


def _data(envelope: dict[str, object]) -> dict[str, object]:
    data = envelope.get("data")
    assert isinstance(data, dict)
    return data


def _registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))


def _accept_termsy(home: Home) -> None:
    """The acceptance record ``facet license accept`` will write (feat(license))."""
    record = {"model": "termsy-weights", "spdx": "CC-BY-NC-4.0"}
    home.licenses_file.write_text(
        json.dumps({"version": 1, "accepted": [record]}),
        encoding="utf-8",
    )


def test_pull_echo_installs_then_reports_already_installed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    home = Home.from_env()
    package = load_registry(FIXTURES).packages["echo"]
    env_dir = home.env_dir("echo", "0.1.0")

    code, envelope = _pull(capsys, "pull", "echo")
    assert code == 0
    data = _data(envelope)
    assert data["already_installed"] is False
    assert data["env"] == str(env_dir)
    assert data["python"] == "3.12"
    assert data["downloaded_bytes"] == 0
    assert data["models"] == []
    assert (env_dir / ".complete").read_text(encoding="utf-8") == package.lock_sha256
    run = subprocess.run(
        [str(env_dir / "venv" / "bin" / "python"), "-c", "import sys; print(sys.version_info[:2])"],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert run.returncode == 0
    assert run.stdout.strip() == "(3, 12)"
    # feat(run) ships the runner SDK: exactly one copy lands in the venv.
    copied = list((env_dir / "venv").rglob("facet_runner_sdk.py"))
    assert len(copied) == 1

    code, envelope = _pull(capsys, "pull", "echo")
    assert code == 0
    second = _data(envelope)
    assert second["already_installed"] is True
    assert second["downloaded_bytes"] == 0
    assert second["env"] == str(env_dir)


def test_pull_echo_rebuilds_when_the_complete_marker_is_gone(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _registry(monkeypatch)
    home = Home.from_env()
    env_dir = home.env_dir("echo", "0.1.0")
    _pull(capsys, "pull", "echo")
    (env_dir / "stale-marker").write_text("left by an interrupted pull", encoding="utf-8")
    (env_dir / ".complete").unlink()
    sdk = tmp_path / "runner_sdk.py"
    sdk.write_text("# runner SDK\n", encoding="utf-8")
    monkeypatch.setattr(env_module, "RUNNER_SDK", sdk)

    code, envelope = _pull(capsys, "pull", "echo")
    assert code == 0
    data = _data(envelope)
    assert data["already_installed"] is False
    assert not (env_dir / "stale-marker").exists()
    copied = [
        path.read_text(encoding="utf-8") for path in (env_dir / "venv").rglob("facet_runner_sdk.py")
    ]
    assert copied == ["# runner SDK\n"]


def test_pull_writes_the_package_fingerprint_marker(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    home = Home.from_env()
    package = load_registry(FIXTURES).packages["echo"]
    env_dir = home.env_dir("echo", "0.1.0")

    code, _envelope = _pull(capsys, "pull", "echo")

    assert code == 0
    marker = env_dir / ".package"
    assert marker.read_text(encoding="utf-8").strip() == env_module.package_fingerprint(package)
    assert env_module.package_stale(package, home) is False

    code, envelope = _pull(capsys, "pull", "echo")
    assert code == 0
    assert _data(envelope)["already_installed"] is True


def test_pull_rebuilds_when_a_non_entry_file_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_copy = tmp_path / "registry"
    shutil.copytree(FIXTURES, registry_copy)
    monkeypatch.setenv("FACET_REGISTRY", str(registry_copy))
    home = Home.from_env()

    assert _pull(capsys, "pull", "echo")[0] == 0
    env_dir = home.env_dir("echo", "0.1.0")
    complete = (env_dir / ".complete").read_text(encoding="utf-8")
    package = load_registry(registry_copy).packages["echo"]
    assert env_module.package_stale(package, home) is False

    skill = registry_copy / "packages" / "echo" / "skill" / "SKILL.md"
    skill.write_text(
        skill.read_text(encoding="utf-8") + "\nEdited in the registry.\n", encoding="utf-8"
    )
    edited = load_registry(registry_copy).packages["echo"]
    assert env_module.package_stale(edited, home) is True

    code, envelope = _pull(capsys, "pull", "echo")

    assert code == 0
    assert _data(envelope)["already_installed"] is False
    # The lock is untouched, so the `.complete` marker is rewritten as-is.
    assert (env_dir / ".complete").read_text(encoding="utf-8") == complete
    staged = env_dir / "package" / "skill" / "SKILL.md"
    assert staged.read_text(encoding="utf-8") == skill.read_text(encoding="utf-8")
    assert env_module.package_stale(edited, home) is False


def test_pull_treats_a_missing_package_marker_as_stale_once(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    home = Home.from_env()
    assert _pull(capsys, "pull", "echo")[0] == 0
    package = load_registry(FIXTURES).packages["echo"]
    env_dir = home.env_dir("echo", "0.1.0")
    (env_dir / ".package").unlink()

    assert env_module.package_stale(package, home) is True

    code, envelope = _pull(capsys, "pull", "echo")
    assert code == 0
    assert _data(envelope)["already_installed"] is False
    assert env_module.package_stale(package, home) is False


def test_package_stale_is_false_where_no_package_copy_is_staged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host-app and binary packages stage no copy, and absence is never stale."""
    _registry(monkeypatch)
    home = Home.from_env()
    echo = load_registry(FIXTURES).packages["echo"]
    assert env_module.package_stale(echo, home) is False
    hosted = dataclasses.replace(
        echo,
        host_app=HostApp(app="blender", min_version="4.0", config_key="blender.path", env="X"),
    )
    assert env_module.package_stale(hosted, home) is False
    nailed = dataclasses.replace(
        echo,
        binary=Binary(
            name="tool",
            version="1.0",
            url="https://example.invalid/tool.zip",
            sha256="0" * 64,
            bin="tool",
        ),
    )
    assert env_module.package_stale(nailed, home) is False


def test_termsy_without_acceptance_downloads_nothing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    home = Home.from_env()

    code, envelope = _pull(capsys, "pull", "termsy")
    assert code == EXIT_LICENSE_NOT_ACCEPTED
    error = envelope["error"]
    assert isinstance(error, dict)
    assert error["code"] == "license_not_accepted"
    assert error["details"]["models"] == [
        {
            "id": "termsy-weights",
            "spdx": "CC-BY-NC-4.0",
            "terms_url": "https://example.invalid/termsy",
            "command": "facet license accept termsy-weights",
        }
    ]
    assert list((home.models_dir / "direct").iterdir()) == []
    assert list(home.tmp_dir.iterdir()) == []
    assert not home.env_dir("termsy", "0.1.0").exists()


def test_termsy_with_acceptance_downloads_the_file_model(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    home = Home.from_env()
    _accept_termsy(home)

    code, envelope = _pull(capsys, "pull", "termsy")
    assert code == 0
    data = _data(envelope)
    assert data["already_installed"] is False
    assert data["downloaded_bytes"] == TERMSY_BYTES
    assert data["models"][0]["status"] == "downloaded"
    target = home.models_dir / "direct" / "termsy-weights" / "termsy.bin"
    assert target.stat().st_size == TERMSY_BYTES

    code, envelope = _pull(capsys, "pull", "termsy")
    assert code == 0
    data = _data(envelope)
    assert data["already_installed"] is True
    assert data["downloaded_bytes"] == 0
    assert data["models"][0]["status"] == "present"

    code, envelope = _pull(capsys, "pull", "termsy", "--verify-models")
    assert code == 0
    assert _data(envelope)["models"][0]["status"] == "present"


def test_verify_models_detects_corruption_and_refetches(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    home = Home.from_env()
    _accept_termsy(home)
    _pull(capsys, "pull", "termsy")
    target = home.models_dir / "direct" / "termsy-weights" / "termsy.bin"
    target.write_bytes(b"x" * TERMSY_BYTES)  # right size, wrong bytes

    code, envelope = _pull(capsys, "pull", "termsy", "--verify-models")
    assert code == 0
    assert _data(envelope)["models"][0]["status"] == "downloaded"
    assert target.read_bytes() == (FIXTURES.parent / "models" / "termsy.bin").read_bytes()


def test_wrong_sha256_fails_and_leaves_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_copy = tmp_path / "registry"
    shutil.copytree(FIXTURES, registry_copy)
    manifest = registry_copy / "packages" / "termsy" / "package.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(REAL_SHA, "0" * 64),
        encoding="utf-8",
    )
    monkeypatch.setenv("FACET_REGISTRY", str(registry_copy))
    home = Home.from_env()
    _accept_termsy(home)

    code, envelope = _pull(capsys, "pull", "termsy")
    assert code == EXIT_DOWNLOAD_FAILED
    error = envelope["error"]
    assert isinstance(error, dict)
    assert error["code"] == "download_failed"
    assert error["details"]["expected"] == "0" * 64
    assert error["details"]["actual"] == REAL_SHA
    assert list((home.models_dir / "direct").rglob("*")) == []
    assert list(home.tmp_dir.iterdir()) == []


def test_insufficient_disk_fails_before_writing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    home = Home.from_env()
    usage = SimpleNamespace(total=10 * GB, used=9 * GB, free=0)
    monkeypatch.setattr(env_module.shutil, "disk_usage", lambda _path: usage)

    code, envelope = _pull(capsys, "pull", "echo")
    assert code == EXIT_MACHINE_CANNOT
    error = envelope["error"]
    assert isinstance(error, dict)
    assert error["code"] == "machine_cannot"
    assert "GB free" in error["message"]
    assert error["details"]["free_bytes"] == 0
    assert error["details"]["need_bytes"] > 0
    assert not home.env_dir("echo", "0.1.0").exists()


# --- the hf_repo source of FR4, served from a loopback stand-in for HF.


class FakeHF:
    """The two Hugging Face endpoints ``facet pull`` calls, on loopback."""

    def __init__(self, weights: bytes = HF_WEIGHTS, sha: str = HF_WEIGHTS_SHA) -> None:
        self.weights = weights
        self.sha = sha
        self.requests: list[tuple[str, dict[str, str]]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> FakeHF:
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

    def listing(self) -> bytes:
        return json.dumps(
            {
                "siblings": [
                    {"rfilename": ".gitattributes", "size": 2},
                    {"rfilename": "README.md", "size": 4},
                    {"rfilename": "config.json", "size": len(HF_CONFIG)},
                    {
                        "rfilename": "sub/weights.bin",
                        "size": len(self.weights),
                        "lfs": {"sha256": self.sha, "size": len(self.weights)},
                    },
                ]
            }
        ).encode("utf-8")

    def serve(self, handler: BaseHTTPRequestHandler) -> None:
        """Answer one GET, recording it so tests can assert what was asked."""
        path = handler.path
        headers = {name.lower(): value for name, value in handler.headers.items()}
        self.requests.append((path, headers))
        if path.startswith(f"/api/models/{HF_REPO}/revision/{HF_REVISION}"):
            self._reply(handler, self.listing())
            return
        resolve = f"/{HF_REPO}/resolve/{HF_REVISION}/"
        if path.startswith(resolve):
            body = HF_CONFIG if path.endswith("config.json") else self.weights
            self._reply(handler, body)
            return
        self._reply(handler, b"not found", status=404)

    def _reply(self, handler: BaseHTTPRequestHandler, body: bytes, status: int = 200) -> None:
        handler.send_response(status)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)


@pytest.fixture
def fake_hf(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeHF]:
    """Serve both Hugging Face endpoints locally and point facet at them."""
    server = FakeHF().start()
    monkeypatch.setattr(env_module, "HF_API", server.base)
    try:
        yield server
    finally:
        server.stop()


def _echo_with_hf_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Home:
    """A registry whose echo package gains one hf_repo model."""
    registry_copy = tmp_path / "registry"
    shutil.copytree(FIXTURES, registry_copy)
    manifest = registry_copy / "packages" / "echo" / "package.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + f"""
[[models]]
id = "hf-weights"
spdx = "Apache-2.0"
commercial_use = true
requires_acceptance = false
bytes = {len(HF_CONFIG) + len(HF_WEIGHTS)}
hf_repo = "{HF_REPO}"
revision = "{HF_REVISION}"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("FACET_REGISTRY", str(registry_copy))
    return Home.from_env()


def test_pull_hf_model_downloads_the_pinned_revision(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_hf: FakeHF,
) -> None:
    home = _echo_with_hf_model(monkeypatch, tmp_path)

    code, envelope = _pull(capsys, "pull", "echo")
    assert code == 0
    data = _data(envelope)
    assert data["downloaded_bytes"] == len(HF_CONFIG) + len(HF_WEIGHTS)
    assert data["models"][0] == {
        "id": "hf-weights",
        "path": "hf-weights",
        "bytes": len(HF_CONFIG) + len(HF_WEIGHTS),
        "status": "downloaded",
    }
    root = home.models_dir / "direct" / "hf-weights"
    assert (root / "config.json").read_bytes() == HF_CONFIG
    assert (root / "sub" / "weights.bin").read_bytes() == HF_WEIGHTS
    asked = "".join(path for path, _ in fake_hf.requests)
    assert ".gitattributes" not in asked
    assert "README.md" not in asked
    assert "blobs=true" in asked
    assert all(head["user-agent"] == f"facetmedia/{__version__}" for _, head in fake_hf.requests)

    code, envelope = _pull(capsys, "pull", "echo")
    assert code == 0
    assert _data(envelope)["models"][0]["status"] == "present"
    # --verify-models recomputes every file against the revision listing.
    code, envelope = _pull(capsys, "pull", "echo", "--verify-models")
    assert code == 0
    assert _data(envelope)["models"][0]["status"] == "present"


def test_pull_hf_wrong_lfs_sha_fails_and_leaves_nothing(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tampered = b"tampered-weights\n"
    server = FakeHF(weights=tampered).start()
    monkeypatch.setattr(env_module, "HF_API", server.base)
    try:
        home = _echo_with_hf_model(monkeypatch, tmp_path)

        code, envelope = _pull(capsys, "pull", "echo")
    finally:
        server.stop()
    assert code == EXIT_DOWNLOAD_FAILED
    error = envelope["error"]
    assert isinstance(error, dict)
    assert error["code"] == "download_failed"
    assert error["details"]["expected"] == HF_WEIGHTS_SHA
    assert error["details"]["actual"] == hashlib.sha256(tampered).hexdigest()
    # The verified config.json may stay for the next pull to reuse; the
    # tampered file must not be in place and no part may linger.
    root = home.models_dir / "direct" / "hf-weights"
    assert not (root / "sub" / "weights.bin").exists()
    assert list(home.tmp_dir.iterdir()) == []


# --- the failure paths of FR3/FR4: uv and the retries.


@pytest.mark.parametrize("uv_bin", ["/bin/false", "/nonexistent-facet-uv"])
def test_uv_failure_is_download_failed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, uv_bin: str
) -> None:
    _registry(monkeypatch)
    home = Home.from_env()
    monkeypatch.setattr(env_module.uv, "find_uv_bin", lambda: uv_bin)

    code, envelope = _pull(capsys, "pull", "echo")
    assert code == EXIT_DOWNLOAD_FAILED
    error = envelope["error"]
    assert isinstance(error, dict)
    assert error["code"] == "download_failed"
    # Interrupt safety: without a synced environment the marker is not written.
    assert not (home.env_dir("echo", "0.1.0") / ".complete").exists()


def test_download_retries_with_backoff_then_gives_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(env_download.time, "sleep", sleeps.append)
    part = tmp_path / "fixture.part"
    attempts = {"n": 0}

    def flaky() -> tuple[str, int]:
        attempts["n"] += 1
        if attempts["n"] < env_download.ATTEMPTS:
            part.write_text("partial", encoding="utf-8")
            raise OSError("connection reset")
        part.write_text("done", encoding="utf-8")
        return "ok", 4

    assert env_download._download_with_retries(part, flaky) == ("ok", 4)
    assert sleeps == [2.0, 8.0]

    def always_fails() -> tuple[str, int]:
        raise OSError("connection refused")

    sleeps.clear()
    with pytest.raises(FacetError) as excinfo:
        env_download._download_with_retries(part, always_fails)
    assert excinfo.value.code == "download_failed"
    assert sleeps == [2.0, 8.0]
    assert not part.exists()


def test_platform_not_supported_fails(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    monkeypatch.setattr(env_module.sys, "platform", "darwin")

    code, envelope = _pull(capsys, "pull", "echo")
    assert code == EXIT_MACHINE_CANNOT
    assert envelope["error"]["code"] == "machine_cannot"


@pytest.mark.parametrize(("mem_gb", "fragment"), [(None, "MemTotal"), (0.0, "needs 0.1 GB of RAM")])
def test_ram_requirement_fails(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mem_gb: float | None,
    fragment: str,
) -> None:
    _registry(monkeypatch)
    monkeypatch.setattr(doctor_module, "mem_total_gb", lambda: mem_gb)

    code, envelope = _pull(capsys, "pull", "echo")
    assert code == EXIT_MACHINE_CANNOT
    assert fragment in envelope["error"]["message"]


def test_missing_host_lib_fails_with_the_manifest_hint() -> None:
    package = load_registry(FIXTURES).packages["echo"]
    requirements = dataclasses.replace(
        package.requirements,
        host_libs=("libfacet_fixture_missing.so",),
        host_hint="install libfacet_fixture_missing",
    )
    home = Home.from_env()

    with pytest.raises(FacetError) as excinfo:
        env_module.pull(dataclasses.replace(package, requirements=requirements), home)
    error = excinfo.value
    assert error.code == "machine_cannot"
    assert error.hint == "install libfacet_fixture_missing"
    assert "libfacet_fixture_missing.so" in error.message
    assert list(home.envs_dir.iterdir()) == []


# --- shared modules: the ``uses`` surface copied into the environment.


def _registry_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    copy = tmp_path / "registry"
    shutil.copytree(FIXTURES, copy)
    monkeypatch.setenv("FACET_REGISTRY", str(copy))
    return copy


def test_pull_copies_the_declared_shared_modules_and_only_those(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry_copy = _registry_copy(tmp_path, monkeypatch)
    home = Home.from_env()
    env_dir = home.env_dir("echo", "0.1.0")
    package = load_registry(registry_copy).packages["echo"]

    code, envelope = _pull(capsys, "pull", "echo")
    assert code == 0
    assert _data(envelope)["already_installed"] is False
    lib = env_module._purelib(env_dir) / "facet_lib"
    assert (lib / "textnorm.py").read_bytes() == (
        registry_copy / "lib" / "textnorm.py"
    ).read_bytes()
    assert (lib / "__init__.py").read_text(encoding="utf-8") == ""
    assert not (lib / "other.py").exists()
    assert (env_dir / ".registry").read_text(encoding="utf-8").strip() == (
        shared_module.fingerprint(package)
    )
    # .complete keeps holding only the lock hash.
    assert (env_dir / ".complete").read_text(encoding="utf-8") == package.lock_sha256

    run = subprocess.run(
        [
            str(env_dir / "venv" / "bin" / "python"),
            "-c",
            "from facet_lib import textnorm; print(textnorm.normalize('ok'))",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert run.returncode == 0
    assert run.stdout.strip() == "ok"


def test_pull_refreshes_shared_modules_when_the_override_content_changes(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry_copy = _registry_copy(tmp_path, monkeypatch)
    home = Home.from_env()
    env_dir = home.env_dir("echo", "0.1.0")
    # A recorded snapshot must not leak into FACET_REGISTRY's content hash.
    home.state_file("registry.json").parent.mkdir(parents=True, exist_ok=True)
    home.state_file("registry.json").write_text(
        json.dumps({"commit": COMMIT, "ref": "main"}), encoding="utf-8"
    )
    _pull(capsys, "pull", "echo")
    assert (env_dir / ".registry").read_text(encoding="utf-8").startswith("override:")
    module = registry_copy / "lib" / "textnorm.py"
    module.write_text(
        module.read_text(encoding="utf-8").replace("return text\n", "return text.upper()\n"),
        encoding="utf-8",
    )

    code, envelope = _pull(capsys, "pull", "echo")
    assert code == 0
    assert _data(envelope)["already_installed"] is False
    copied = env_module._purelib(env_dir) / "facet_lib" / "textnorm.py"
    assert "return text.upper()" in copied.read_text(encoding="utf-8")


def test_pull_pinned_rebuilds_when_the_snapshot_changes(
    capsys: pytest.CaptureFixture[str],
    fake_github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = Home.from_env()
    update.update_registry(home, "main")
    assert fake_github.archive_hits == 1
    env_dir = home.env_dir("echo", "0.1.0")

    code, envelope = _pull(capsys, "pull", "echo")
    assert code == 0
    assert (env_dir / ".registry").read_text(encoding="utf-8").strip() == COMMIT

    other = home.registry_dir(OTHER_COMMIT)
    shutil.copytree(home.registry_dir(COMMIT), other)
    home.state_file("registry.json").write_text(
        json.dumps({"commit": OTHER_COMMIT, "ref": "main"}), encoding="utf-8"
    )

    code, envelope = _pull(capsys, "pull", "echo")
    assert code == 0
    assert _data(envelope)["already_installed"] is False
    assert (env_dir / ".registry").read_text(encoding="utf-8").strip() == OTHER_COMMIT


def test_pull_of_a_package_without_uses_writes_no_registry_marker(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch)
    home = Home.from_env()
    _accept_termsy(home)
    package = load_registry(FIXTURES).packages["termsy"]

    code, _envelope = _pull(capsys, "pull", "termsy")
    assert code == 0
    env_dir = home.env_dir("termsy", "0.1.0")
    assert not (env_dir / ".registry").exists()
    assert env_module._is_current(package, env_dir) is True
    # A stray marker is ignored: without ``uses`` it is never read.
    (env_dir / ".registry").write_text("bogus", encoding="utf-8")
    assert env_module._is_current(package, env_dir) is True


# --- the shared browser of ``[browser]``: a tiny local stand-in for the CDN.


def _browser_zip() -> bytes:
    """A tiny archive in Playwright's layout: ``chrome-linux/chrome``."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        info = zipfile.ZipInfo("chrome-linux/chrome", date_time=(2024, 1, 1, 0, 0, 0))
        bundle.writestr(info, b"#!/bin/sh\nexec true\n")
    return buffer.getvalue()


class FakeBrowser:
    """The Playwright CDN's ``chromium-linux.zip`` endpoint, on loopback."""

    def __init__(self, revision: str = BROWSER_REVISION) -> None:
        self.revision = revision
        self.archive = _browser_zip()
        self.sha256 = hashlib.sha256(self.archive).hexdigest()
        self.requests: list[str] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> FakeBrowser:
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
        """Answer one GET, recording it so tests can assert what was asked."""
        self.requests.append(handler.path)
        if handler.path == f"/chromium/{self.revision}/chromium-linux.zip":
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(self.archive)))
            handler.end_headers()
            handler.wfile.write(self.archive)
            return
        handler.send_response(404)
        handler.send_header("Content-Length", "9")
        handler.end_headers()
        handler.wfile.write(b"not found")


@pytest.fixture
def fake_browser(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeBrowser]:
    """Serve the chromium archive locally and point facet at it."""
    server = FakeBrowser().start()
    monkeypatch.setattr(env_module, "PLAYWRIGHT_CDN", server.base)
    try:
        yield server
    finally:
        server.stop()


def _echo_with_browser(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sha256: str) -> Home:
    """A registry whose echo package declares ``[browser]``."""
    registry_copy = tmp_path / "registry"
    shutil.copytree(FIXTURES, registry_copy)
    manifest = registry_copy / "packages" / "echo" / "package.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + f"""
[browser]
channel = "chromium"
revision = "{BROWSER_REVISION}"
sha256 = "{sha256}"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("FACET_REGISTRY", str(registry_copy))
    return Home.from_env()


def test_pull_fetches_the_browser_once_into_the_playwright_layout(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_browser: FakeBrowser,
) -> None:
    home = _echo_with_browser(monkeypatch, tmp_path, fake_browser.sha256)
    target = home.browsers_dir / f"chromium-{BROWSER_REVISION}"
    archive = f"/chromium/{BROWSER_REVISION}/chromium-linux.zip"

    code, envelope = _pull(capsys, "pull", "echo")
    assert code == 0
    assert _data(envelope)["already_installed"] is False
    # Playwright's own <name>-<revision> layout, never <channel>/<revision>.
    assert (target / "chrome-linux" / "chrome").is_file()
    marker = target / env_module.BROWSER_MARKER
    assert marker.read_text(encoding="utf-8").strip() == fake_browser.sha256
    assert fake_browser.requests == [archive]
    assert not (home.browsers_dir / "chromium" / BROWSER_REVISION).exists()

    # A verified extraction skips the download entirely.
    code, _envelope = _pull(capsys, "pull", "echo")
    assert code == 0
    assert fake_browser.requests == [archive]

    # Removing the extraction makes the next pull fetch it again.
    shutil.rmtree(target)
    code, _envelope = _pull(capsys, "pull", "echo")
    assert code == 0
    assert fake_browser.requests == [archive, archive]
    assert (target / "chrome-linux" / "chrome").is_file()
    assert (target / env_module.BROWSER_MARKER).is_file()

    # A marker that does not hold the declared sha256 counts as absent too.
    marker.write_text("0" * 64, encoding="utf-8")
    code, _envelope = _pull(capsys, "pull", "echo")
    assert code == 0
    assert fake_browser.requests == [archive, archive, archive]
    assert marker.read_text(encoding="utf-8").strip() == fake_browser.sha256


def test_runner_env_points_playwright_at_the_shared_browsers_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _echo_with_browser(monkeypatch, tmp_path, "0" * 64)
    package = load_registry(tmp_path / "registry").packages["echo"]
    env_vars = runner_module.runner_env(home, package)
    assert env_vars["PLAYWRIGHT_BROWSERS_PATH"] == str(home.browsers_dir)

    _registry(monkeypatch)
    plain = load_registry(FIXTURES).packages["echo"]
    assert "PLAYWRIGHT_BROWSERS_PATH" not in runner_module.runner_env(home, plain)
