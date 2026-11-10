"""The ``node`` engine: pinned runtime and npm tarballs, verified, offline after pull.

No real Node is downloaded: the runtime tarball is a stub ``bin/node`` (a
Python script in node's argv shape) built in the test, and both tarballs are
served by a loopback ``ThreadingHTTPServer``.  The manifest declares the real
``registry.npmjs.org`` and ``nodejs.org`` hosts — validation requires them —
and an in-process ``urlopen`` rewrite points both at the local server.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import sys
import tarfile
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import IO

import pytest

from facetmedia import doctor as doctor_module
from facetmedia import env as env_module
from facetmedia.cli import main
from facetmedia.home import Home
from facetmedia.registry import load_registry, validate_registry
from facetmedia.registry_types import Node, NodePackage, Package

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
NODE_RUNTIME = "22.21.0"
NPM_NAME = "@facet/fixture-cli"
NPM_VERSION = "0.1.0"
RUNTIME_TARBALL_PATH = f"/v{NODE_RUNTIME}/node-v{NODE_RUNTIME}-linux-x64.tar.gz"
NPM_TARBALL_PATH = f"/{NPM_NAME}/-/{NPM_NAME}-{NPM_VERSION}.tgz"
NPM_TARBALL_URL = f"https://registry.npmjs.org{NPM_TARBALL_PATH}"
EXIT_DOWNLOAD_FAILED = 8
REDIRECTED_HOSTS = frozenset({"nodejs.org", "registry.npmjs.org"})

#: The stub ``bin/node``: answers ``--version`` and speaks the runner protocol
#: in Python, recording the argv and the FACET_NODE* environment it was given.
NODE_STUB = '''#!{python}
"""The fixture node binary: a Python script wearing node's argv."""

import json
import os
import sys
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    if "--input" not in args:
        print("v{runtime}")
        return 0
    verb = args[1]
    request = json.loads(Path(args[args.index("--input") + 1]).read_text())
    if verb == "run":
        result = _run(request)
    elif verb == "verify":
        result = _verify(request)
    else:
        result = {"checks": [{"name": "stub", "ok": True, "detail": "ready", "fix": ""}]}
    Path(args[args.index("--result") + 1]).write_text(json.dumps(result))
    return 0


def _seen() -> dict:
    return {
        "argv": sys.argv,
        "facet_node": os.environ.get("FACET_NODE"),
        "facet_node_path": os.environ.get("FACET_NODE_PATH"),
    }


def _run(request: dict) -> dict:
    out_dir = Path(request["out_dir"])
    payload = {"text": str(request["inputs"].get("text", "")), "seen": _seen()}
    (out_dir / "node-out.txt").write_text(json.dumps(payload), encoding="utf-8")
    return {
        "tool": {"name": "nodepkg", "version": "0.1.0"},
        "assets": [
            {
                "file": "node-out.txt",
                "name": "node-out",
                "kind": "text",
                "mime": "text/plain",
                "properties": {},
                "params": dict(request["inputs"]),
                "seed": None,
                "inputs": [],
            }
        ],
    }


def _verify(request: dict) -> dict:
    return {
        "assets": [
            {
                "file": Path(path).name,
                "passed": True,
                "metrics": {},
                "thresholds": {},
                "verifier": "stub",
            }
            for path in request["assets"]
        ]
    }


raise SystemExit(main())
'''

JS_ENTRY = """// The fixture entry: facet's stub node binary does the protocol work.
console.log("fixture-cli");
"""

MANIFEST = """name = "nodepkg"
version = "0.1.0"
requires_facet = ">=0.1.0b1,<0.2"
protocol = 1
provider = "facet"
tool_name = "NodePkg"
description = "A node-engine fixture package."
facet = "text"

[engine]
type = "node"

[engine.node]
runtime = "@@RUNTIME@@"
runtime_sha256 = "@@RUNTIME_SHA@@"

[[engine.node.packages]]
name = "@@NPM_NAME@@"
version = "@@NPM_VERSION@@"
tarball = "@@NPM_TARBALL_URL@@"
integrity = "@@INTEGRITY@@"

[transport]
type = "cli"
entry = "cli.js"
invocation_template = "{{node}} {{entry}} {{verb}} --input {{input}} --result {{result}}"

[requirements]
gpu = false
ram_gb = 0.1
disk_gb = 0.01
exclusive = false
platforms = ["linux"]
host_libs = []
host_hint = ""

[[licenses]]
subject = "fixture-cli"
spdx = "Apache-2.0"

[capabilities.echo]
summary = "Echo text back through the node stub."
kind = "text"

[capabilities.echo.preview]
viewer = "generic"

[capabilities.echo.inputs.text]
type = "string"
required = true
"""

SKILL = """---
name: nodepkg
description: A node-engine fixture skill for pull and run tests.
---
# Nodepkg

Body mentions nodepkg.echo.

## When to use

Use it in tests.

## Pieces

```toml
[inputs]
text = "hi"
```

Pieces for nodepkg.echo.

## Judging the result

Verify passes; read the generic preview fields.

## Pitfalls

- None.
"""


@dataclass(frozen=True)
class World:
    """The registry copy, the two tarballs and the loopback server."""

    registry: Path
    runtime: bytes
    package: bytes
    runtime_sha256: str
    integrity: str
    server: NodeTarballs


def _tarball_bytes(root: str, files: dict[str, tuple[bytes, int]]) -> bytes:
    """A deterministic ``.tar.gz``: ``root/<name>`` entries, mode, mtime 0."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, (data, mode) in files.items():
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(data)
            info.mode = mode
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _runtime_tarball() -> bytes:
    stub = (
        NODE_STUB.replace("{python}", sys.executable)
        .replace("{runtime}", NODE_RUNTIME)
        .encode("utf-8")
    )
    return _tarball_bytes(
        f"node-v{NODE_RUNTIME}-linux-x64",
        {
            "bin/node": (stub, 0o755),
            "bin/npm": (b"#!/bin/sh\nexit 0\n", 0o755),
        },
    )


def _package_tarball() -> bytes:
    return _tarball_bytes(
        "package",
        {
            "cli.js": (JS_ENTRY.encode("utf-8"), 0o644),
            "facet-npm": (b"#!/bin/sh\nexit 0\n", 0o755),
        },
    )


def _manifest(runtime_sha256: str, integrity: str) -> str:
    return (
        MANIFEST.replace("@@RUNTIME@@", NODE_RUNTIME)
        .replace("@@RUNTIME_SHA@@", runtime_sha256)
        .replace("@@NPM_NAME@@", NPM_NAME)
        .replace("@@NPM_VERSION@@", NPM_VERSION)
        .replace("@@NPM_TARBALL_URL@@", NPM_TARBALL_URL)
        .replace("@@INTEGRITY@@", integrity)
    )


class NodeTarballs:
    """The two tarballs over loopback; ``hits`` counts what was asked."""

    def __init__(self, runtime: bytes, package: bytes) -> None:
        self.runtime = runtime
        self.package = package
        self.hits: list[str] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> NodeTarballs:
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
                server.hits.append(self.path)
                body = server._body(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        return Handler

    def _body(self, path: str) -> bytes | None:
        if path == f"/dist{RUNTIME_TARBALL_PATH}":
            return self.runtime
        if path == NPM_TARBALL_PATH:
            return self.package
        return None


def _redirect_downloads(monkeypatch: pytest.MonkeyPatch, server: NodeTarballs) -> None:
    """Point the two declared hosts at the loopback server, in process only."""
    real_urlopen = urllib.request.urlopen

    def redirected(request: object, *args: object, **kwargs: object) -> IO[bytes]:
        url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
        parts = urllib.parse.urlsplit(url)
        if parts.hostname in REDIRECTED_HOSTS:
            headers = (
                dict(request.header_items()) if isinstance(request, urllib.request.Request) else {}
            )
            request = urllib.request.Request(server.base + parts.path, headers=headers)
        return real_urlopen(request, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", redirected)


def _world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """A registry copy with ``nodepkg`` added, its hashes baked into the manifest."""
    registry = tmp_path / "registry"
    shutil.copytree(FIXTURES, registry)
    runtime = _runtime_tarball()
    package = _package_tarball()
    runtime_sha256 = hashlib.sha256(runtime).hexdigest()
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(package).digest()).decode("ascii")
    package_dir = registry / "packages" / "nodepkg"
    package_dir.mkdir()
    (package_dir / "package.toml").write_text(
        _manifest(runtime_sha256, integrity), encoding="utf-8"
    )
    (package_dir / "cli.js").write_text(JS_ENTRY, encoding="utf-8")
    skill_dir = package_dir / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(SKILL, encoding="utf-8")
    index = registry / "index.toml"
    index.write_text(
        index.read_text(encoding="utf-8") + '\n[[packages]]\nname = "nodepkg"\n',
        encoding="utf-8",
    )
    server = NodeTarballs(runtime, package).start()
    monkeypatch.setenv("FACET_REGISTRY", str(registry))
    _redirect_downloads(monkeypatch, server)
    return World(registry, runtime, package, runtime_sha256, integrity, server)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    built = _world(tmp_path, monkeypatch)
    try:
        yield built
    finally:
        built.server.stop()


def _pull(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, object]]:
    code = main([*argv, "--json"])
    envelope = json.loads(capsys.readouterr().out)
    assert isinstance(envelope, dict)
    return code, envelope


def _data(envelope: dict[str, object]) -> dict[str, object]:
    data = envelope.get("data")
    assert isinstance(data, dict)
    return data


def _problems(world: World) -> set[tuple[str, str, str]]:
    return {
        (problem["file"], problem["key"], problem["problem"])
        for problem in validate_registry(world.registry)
    }


def _rewrite_manifest(world: World, old: str, new: str) -> None:
    manifest = world.registry / "packages" / "nodepkg" / "package.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def _nodepkg(world: World) -> Package:
    return load_registry(world.registry).packages["nodepkg"]


# --- validate: the node engine declaration and its five rejections.


def test_validate_accepts_a_node_engine_and_parses_it(world: World) -> None:
    assert _problems(world) == set()
    package = _nodepkg(world)
    assert package.engine_type == "node"
    assert package.python == ""
    assert package.node == Node(
        runtime=NODE_RUNTIME,
        runtime_sha256=world.runtime_sha256,
        packages=(
            NodePackage(
                name=NPM_NAME,
                version=NPM_VERSION,
                tarball=NPM_TARBALL_URL,
                integrity=world.integrity,
            ),
        ),
    )
    assert package.invocation_template == (
        "{{node}} {{entry}} {{verb}} --input {{input}} --result {{result}}"
    )
    assert main(["registry", "validate", str(world.registry)]) == 0


def test_validate_rejects_a_non_semver_runtime(world: World) -> None:
    _rewrite_manifest(world, f'runtime = "{NODE_RUNTIME}"', 'runtime = "latest"')
    assert ("packages/nodepkg/package.toml", "engine.node.runtime", "value") in _problems(world)


def test_validate_rejects_a_malformed_runtime_sha256(world: World) -> None:
    _rewrite_manifest(world, world.runtime_sha256, "not-a-sha256")
    assert (
        "packages/nodepkg/package.toml",
        "engine.node.runtime_sha256",
        "value",
    ) in _problems(world)


def test_validate_rejects_a_malformed_integrity(world: World) -> None:
    _rewrite_manifest(world, world.integrity, "md5-deadbeef")
    assert (
        "packages/nodepkg/package.toml",
        "engine.node.packages[0].integrity",
        "value",
    ) in _problems(world)


def test_validate_rejects_a_tarball_off_the_npm_registry(world: World) -> None:
    _rewrite_manifest(world, "https://registry.npmjs.org/", "https://example.invalid/")
    assert (
        "packages/nodepkg/package.toml",
        "engine.node.packages[0].tarball",
        "value",
    ) in _problems(world)


def test_validate_rejects_a_non_semver_package_version(world: World) -> None:
    _rewrite_manifest(world, f'version = "{NPM_VERSION}"\ntarball', 'version = "1.0"\ntarball')
    assert (
        "packages/nodepkg/package.toml",
        "engine.node.packages[0].version",
        "value",
    ) in _problems(world)


def test_validate_rejects_a_malformed_scoped_name(world: World) -> None:
    _rewrite_manifest(world, f'name = "{NPM_NAME}"', 'name = "@facet/"')
    assert (
        "packages/nodepkg/package.toml",
        "engine.node.packages[0].name",
        "value",
    ) in _problems(world)


def test_validate_rejects_engine_python_on_a_node_package(world: World) -> None:
    _rewrite_manifest(world, '[engine]\ntype = "node"', '[engine]\ntype = "node"\npython = "3.12"')
    assert ("packages/nodepkg/package.toml", "engine.python", "value") in _problems(world)


def test_validate_rejects_a_node_table_on_a_python_env_package(world: World) -> None:
    _rewrite_manifest(
        world, '[engine]\ntype = "node"', '[engine]\ntype = "python-env"\npython = "3.12"'
    )
    assert (
        "packages/nodepkg/package.toml",
        "engine.node",
        "requires node with transport cli",
    ) in _problems(world)


def test_validate_rejects_a_node_package_without_its_entry(world: World) -> None:
    (world.registry / "packages" / "nodepkg" / "cli.js").unlink()

    assert ("packages/nodepkg/cli.js", "", "missing") in _problems(world)


# --- pull: verified extractions, skipped when present, rebuilt when gone.


def test_pull_downloads_verifies_and_then_skips(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    home = Home.from_env()
    package = _nodepkg(world)

    code, envelope = _pull(capsys, "pull", "nodepkg")
    assert code == 0
    data = _data(envelope)
    assert data["already_installed"] is False
    assert data["downloaded_bytes"] == len(world.runtime) + len(world.package)
    runtime_dir = home.node_runtime_dir(NODE_RUNTIME)
    assert (runtime_dir / "bin" / "node").is_file()
    marker = (runtime_dir / env_module.NODE_MARKER).read_text(encoding="utf-8")
    assert marker.strip() == world.runtime_sha256
    package_dir = home.node_pkg_dir(NPM_NAME, NPM_VERSION)
    assert (package_dir / "cli.js").is_file()
    assert (package_dir / "facet-npm").is_file()
    marker = (package_dir / env_module.NODE_MARKER).read_text(encoding="utf-8")
    assert marker.strip() == world.integrity
    # The pull stages the entry and writes the source entry's sha256 marker.
    staged = home.env_dir("nodepkg", "0.1.0") / "package" / "cli.js"
    assert staged.is_file()
    marker = (home.env_dir("nodepkg", "0.1.0") / env_module.COMPLETE).read_text(encoding="utf-8")
    assert marker.strip() == hashlib.sha256(JS_ENTRY.encode("utf-8")).hexdigest()
    assert env_module.is_installed(package, home) is True

    hits = len(world.server.hits)
    code, envelope = _pull(capsys, "pull", "nodepkg")
    assert code == 0
    second = _data(envelope)
    assert second["already_installed"] is True
    assert second["downloaded_bytes"] == 0
    assert len(world.server.hits) == hits


def test_pull_stages_the_entry_with_its_sha_marker(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    home = Home.from_env()

    code, _envelope = _pull(capsys, "pull", "nodepkg")

    assert code == 0
    env_dir = home.env_dir("nodepkg", "0.1.0")
    assert (env_dir / "package" / "cli.js").read_text(encoding="utf-8") == JS_ENTRY
    # The whole package directory is staged, not just the entry.
    assert (env_dir / "package" / "skill" / "SKILL.md").is_file()
    marker = (env_dir / env_module.COMPLETE).read_text(encoding="utf-8")
    expected = hashlib.sha256(JS_ENTRY.encode("utf-8")).hexdigest()
    assert marker.strip() == expected


def test_pull_restages_the_entry_when_the_entry_changes(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    home = Home.from_env()
    assert _pull(capsys, "pull", "nodepkg")[0] == 0
    source = world.registry / "packages" / "nodepkg" / "cli.js"
    edited = JS_ENTRY + "// edited in the package directory\n"
    source.write_text(edited, encoding="utf-8")

    code, envelope = _pull(capsys, "pull", "nodepkg")

    assert code == 0
    data = _data(envelope)
    assert data["already_installed"] is False
    assert data["downloaded_bytes"] == 0
    env_dir = home.env_dir("nodepkg", "0.1.0")
    assert (env_dir / "package" / "cli.js").read_text(encoding="utf-8") == edited
    marker = (env_dir / env_module.COMPLETE).read_text(encoding="utf-8")
    assert marker.strip() == hashlib.sha256(edited.encode("utf-8")).hexdigest()


def test_pull_restages_when_a_non_entry_file_changes(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    home = Home.from_env()
    assert _pull(capsys, "pull", "nodepkg")[0] == 0
    env_dir = home.env_dir("nodepkg", "0.1.0")
    package = _nodepkg(world)
    marker = env_dir / env_module.PACKAGE_MARKER
    assert marker.read_text(encoding="utf-8").strip() == env_module.package_fingerprint(package)
    assert env_module.package_stale(package, home) is False

    skill = world.registry / "packages" / "nodepkg" / "skill" / "SKILL.md"
    skill.write_text(SKILL + "\nEdited in the registry.\n", encoding="utf-8")
    edited = _nodepkg(world)
    assert env_module.is_installed(edited, home) is True
    assert env_module.package_stale(edited, home) is True

    code, envelope = _pull(capsys, "pull", "nodepkg")

    assert code == 0
    assert _data(envelope)["already_installed"] is False
    assert (env_dir / "package" / "skill" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == skill.read_text(encoding="utf-8")
    expected = hashlib.sha256(JS_ENTRY.encode("utf-8")).hexdigest()
    assert (env_dir / env_module.COMPLETE).read_text(encoding="utf-8").strip() == expected
    assert env_module.package_stale(edited, home) is False


def test_pull_rebuilds_after_an_extraction_is_removed(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    home = Home.from_env()
    package = _nodepkg(world)
    _pull(capsys, "pull", "nodepkg")

    shutil.rmtree(home.node_runtime_dir(NODE_RUNTIME))
    assert env_module.node_present(package, home) is False
    code, envelope = _pull(capsys, "pull", "nodepkg")
    assert code == 0
    assert _data(envelope)["already_installed"] is False
    assert (home.node_runtime_dir(NODE_RUNTIME) / "bin" / "node").is_file()

    shutil.rmtree(home.node_pkgs_dir / NPM_NAME)
    code, envelope = _pull(capsys, "pull", "nodepkg")
    assert code == 0
    assert _data(envelope)["already_installed"] is False
    assert (home.node_pkg_dir(NPM_NAME, NPM_VERSION) / "cli.js").is_file()
    assert env_module.node_present(package, home) is True


def test_a_tampered_tarball_fails_with_download_failed(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.server.package = b"not the pinned tarball"

    code, envelope = _pull(capsys, "pull", "nodepkg")

    assert code == EXIT_DOWNLOAD_FAILED
    error = envelope["error"]
    assert isinstance(error, dict)
    assert error["code"] == "download_failed"
    home = Home.from_env()
    assert not (home.node_pkgs_dir / NPM_NAME).exists()
    assert env_module.node_present(_nodepkg(world), home) is False
    assert list(home.tmp_dir.iterdir()) == []


# --- run: the managed node binary, the entry package, the exported env.


def test_run_uses_the_managed_node_and_entry_package(
    world: World, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    piece = tmp_path / "piece.toml"
    piece.write_text('[inputs]\ntext = "hi"\n', encoding="utf-8")
    out = tmp_path / "out"

    code = main(["run", "nodepkg.echo", str(piece), "--out", str(out), "--json"])
    envelope = json.loads(capsys.readouterr().out)
    assert code == 0, envelope
    assert envelope["data"]["pulled"] is True

    home = Home.from_env()
    node_bin = home.node_runtime_dir(NODE_RUNTIME) / "bin" / "node"
    entry = home.env_dir("nodepkg", "0.1.0") / "package" / "cli.js"
    payload = json.loads((out / "node-out.txt").read_text(encoding="utf-8"))
    assert payload["text"] == "hi"
    seen = payload["seen"]
    assert seen["argv"][0] == str(node_bin)
    assert seen["argv"][1] == str(entry)
    assert seen["argv"][2] == "run"
    assert seen["facet_node"] == str(node_bin)
    assert seen["facet_node_path"] == str(home.node_pkg_dir(NPM_NAME, NPM_VERSION))


# --- doctor: the pull-fixable check and the machine_cannot split.


def test_doctor_reports_a_missing_extraction_with_the_pull_fix(world: World) -> None:
    home = Home.from_env()
    package = _nodepkg(world)

    checks, _failures = doctor_module.machine_checks(package, home)

    assert checks["installed"]["ok"] is False
    assert checks["installed"]["fix"] == "facet pull nodepkg"
    assert checks["node"]["ok"] is False
    assert checks["node"]["fix"] == "facet pull nodepkg"


def test_doctor_reports_a_stale_staged_runner(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _pull(capsys, "pull", "nodepkg")[0] == 0
    home = Home.from_env()
    package = _nodepkg(world)
    source = world.registry / "packages" / "nodepkg" / "cli.js"
    source.write_text(JS_ENTRY + "// edited after the pull\n", encoding="utf-8")

    checks, failures = doctor_module.machine_checks(package, home)

    assert failures == []
    assert checks["installed"]["ok"] is False
    assert checks["installed"]["fix"] == "facet pull nodepkg"
    assert "the staged runner is not current" in str(checks["installed"]["detail"])
    assert checks["node"]["ok"] is False
    assert checks["node"]["fix"] == "facet pull nodepkg"
    assert "the staged runner is not current" in str(checks["node"]["detail"])

    _checks, _failures, doctor_errors = doctor_module.run_check(package, home, "", "run-stale")
    assert doctor_errors == []


def test_doctor_reports_machine_cannot_when_the_binary_cannot_run(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _envelope = _pull(capsys, "pull", "nodepkg")
    assert code == 0
    home = Home.from_env()
    package = _nodepkg(world)
    node_bin = home.node_runtime_dir(NODE_RUNTIME) / "bin" / "node"
    node_bin.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

    checks, _failures, doctor_errors = doctor_module.run_check(package, home, "", "run-doctor")

    assert checks["node"]["ok"] is False
    assert checks["node"]["fix"] != "facet pull nodepkg"
    assert any(error.code == "machine_cannot" for error in doctor_errors)
