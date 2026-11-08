"""The ``binary`` engine: a pinned zip, verified, run from the facet home.

No real binary is downloaded: the zip holds a stub executable (a Python
script in the binary's argv shape) built in the test, served by a loopback
``ThreadingHTTPServer``.  The manifest declares a real https host —
validation requires one — and an in-process ``urlopen`` rewrite points it
at the local server.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import sys
import threading
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import IO

import pytest

from facetmedia import doctor as doctor_module
from facetmedia import env as env_module
from facetmedia.cli import main
from facetmedia.env import binary as env_binary
from facetmedia.home import Home
from facetmedia.registry import load_registry, validate_registry
from facetmedia.registry_types import Binary, Package

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
BINARY_NAME = "stubtool"
BINARY_VERSION = "1.2.3"
ZIP_PATH = "/releases/stubtool-1.2.3.zip"
ZIP_URL = f"https://facetbins.example.com{ZIP_PATH}"
EXIT_DOWNLOAD_FAILED = 8
REDIRECTED_HOSTS = frozenset({"facetbins.example.com"})

#: The stub executable: answers ``--version`` and speaks the runner protocol
#: in Python, recording the argv, FACET_BIN and the PATH it was given.
STUB = '''#!{python}
"""The fixture binary: a Python script wearing a static binary's argv."""

import json
import os
import sys
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--version":
        print("stubtool {version}")
        return 0
    verb = args[0]
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
        "facet_bin": os.environ.get("FACET_BIN"),
        "path_head": os.environ.get("PATH", "").split(os.pathsep)[0],
    }


def _run(request: dict) -> dict:
    out_dir = Path(request["out_dir"])
    payload = {"text": str(request["inputs"].get("text", "")), "seen": _seen()}
    (out_dir / "bin-out.txt").write_text(json.dumps(payload), encoding="utf-8")
    return {
        "tool": {"name": "stubtool", "version": "{version}"},
        "assets": [
            {
                "file": "bin-out.txt",
                "name": "bin-out",
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

BIN_TABLE = """[engine.binary]
name = "stubtool"
version = "1.2.3"
url = "@@URL@@"
sha256 = "@@SHA@@"
bin = "bin/stubtool"
version_arg = "--version"
"""

MANIFEST = """name = "binpkg"
version = "0.1.0"
requires_facet = ">=0.1.0b1,<0.2"
protocol = 1
provider = "facet"
tool_name = "BinPkg"
description = "A binary-engine fixture package."
facet = "text"

[engine]
type = "binary"

@@BIN_TABLE@@
[transport]
type = "cli"
invocation_template = "{{binary}} {{verb}} --input {{input}} --result {{result}}"

[requirements]
gpu = false
ram_gb = 0.1
disk_gb = 0.01
exclusive = false
platforms = ["linux"]
host_libs = []
host_hint = ""

[[licenses]]
subject = "stubtool"
spdx = "Apache-2.0"

[capabilities.echo]
summary = "Echo text back through the binary stub."
kind = "text"

[capabilities.echo.preview]
viewer = "generic"

[capabilities.echo.inputs.text]
type = "string"
required = true
"""

SKILL = """---
name: binpkg
description: A binary-engine fixture skill for pull and run tests.
---
# Binpkg

Body mentions binpkg.echo.

## When to use

Use it in tests.

## Pieces

```toml
[inputs]
text = "hi"
```

Pieces for binpkg.echo.

## Judging the result

Verify passes; read the generic preview fields.

## Pitfalls

- None.
"""


@dataclass(frozen=True)
class World:
    """The registry copy, the zip and the loopback server."""

    registry: Path
    archive: bytes
    sha256: str
    server: ZipServer


def _zip_bytes() -> bytes:
    """A deterministic ``.zip``: ``bin/stubtool`` plus a sibling library."""
    stub = (
        STUB.replace("{python}", sys.executable)
        .replace("{version}", BINARY_VERSION)
        .encode("utf-8")
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, data in {
            "bin/stubtool": stub,
            "bin/libstub.dat": b"sibling library\n",
        }.items():
            info = zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, data)
    return buffer.getvalue()


def _zip_without_the_bin() -> bytes:
    """A sha-valid zip whose declared ``bin`` is absent: the extraction must not land."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("bin/libstub.dat", b"sibling library only\n")
    return buffer.getvalue()


def _manifest(sha256: str) -> str:
    return (
        MANIFEST.replace("@@BIN_TABLE@@", BIN_TABLE)
        .replace("@@URL@@", ZIP_URL)
        .replace("@@SHA@@", sha256)
    )


class ZipServer:
    """The zip over loopback; ``hits`` counts what was asked."""

    def __init__(self, archive: bytes) -> None:
        self.archive = archive
        self.hits: list[str] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> ZipServer:
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
                if self.path != ZIP_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(server.archive)))
                self.end_headers()
                self.wfile.write(server.archive)

            def log_message(self, format: str, *args: object) -> None:
                return

        return Handler


def _redirect_downloads(monkeypatch: pytest.MonkeyPatch, server: ZipServer) -> None:
    """Point the declared host at the loopback server, in process only."""
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
    """A registry copy with ``binpkg`` added, its hash baked into the manifest."""
    registry = tmp_path / "registry"
    shutil.copytree(FIXTURES, registry)
    archive = _zip_bytes()
    sha256 = hashlib.sha256(archive).hexdigest()
    package_dir = registry / "packages" / "binpkg"
    package_dir.mkdir()
    (package_dir / "package.toml").write_text(_manifest(sha256), encoding="utf-8")
    skill_dir = package_dir / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(SKILL, encoding="utf-8")
    index = registry / "index.toml"
    index.write_text(
        index.read_text(encoding="utf-8") + '\n[[packages]]\nname = "binpkg"\n',
        encoding="utf-8",
    )
    server = ZipServer(archive).start()
    monkeypatch.setenv("FACET_REGISTRY", str(registry))
    _redirect_downloads(monkeypatch, server)
    return World(registry, archive, sha256, server)


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


def _rewrite_manifest(world: World, transform: Callable[[str], str]) -> None:
    manifest = world.registry / "packages" / "binpkg" / "package.toml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(transform(text), encoding="utf-8")


def _binpkg(world: World) -> Package:
    return load_registry(world.registry).packages["binpkg"]


# --- validate: the binary engine declaration and its rejections.


def test_validate_accepts_a_binary_engine_and_parses_it(world: World) -> None:
    assert _problems(world) == set()
    package = _binpkg(world)
    assert package.engine_type == "binary"
    assert package.python == ""
    assert package.entry == ""
    assert package.binary == Binary(
        name=BINARY_NAME,
        version=BINARY_VERSION,
        url=ZIP_URL,
        sha256=world.sha256,
        bin="bin/stubtool",
        version_arg="--version",
    )
    assert package.invocation_template == (
        "{{binary}} {{verb}} --input {{input}} --result {{result}}"
    )
    assert main(["registry", "validate", str(world.registry)]) == 0


def test_validate_rejects_a_missing_binary_table(world: World) -> None:
    _rewrite_manifest(
        world,
        lambda text: text[: text.find("[engine.binary]")] + text[text.find("[transport]") :],
    )
    assert ("packages/binpkg/package.toml", "engine.binary", "required") in _problems(world)


def test_validate_rejects_a_non_https_url(world: World) -> None:
    _rewrite_manifest(world, lambda text: text.replace("https://", "http://", 1))
    assert ("packages/binpkg/package.toml", "engine.binary.url", "value") in _problems(world)


def test_validate_rejects_a_malformed_sha256(world: World) -> None:
    _rewrite_manifest(world, lambda text: text.replace(world.sha256, "not-a-sha256"))
    assert ("packages/binpkg/package.toml", "engine.binary.sha256", "value") in _problems(world)


def test_validate_rejects_an_empty_bin_path(world: World) -> None:
    _rewrite_manifest(world, lambda text: text.replace('bin = "bin/stubtool"', 'bin = ""'))
    assert ("packages/binpkg/package.toml", "engine.binary.bin", "value") in _problems(world)


def test_validate_rejects_a_name_or_version_that_escapes_the_bin_dir(world: World) -> None:
    _rewrite_manifest(world, lambda text: text.replace('name = "stubtool"', 'name = "../stubtool"'))
    assert ("packages/binpkg/package.toml", "engine.binary.name", "value") in _problems(world)

    _rewrite_manifest(
        world, lambda text: text.replace('version = "1.2.3"', 'version = "../../1.2.3"')
    )
    assert ("packages/binpkg/package.toml", "engine.binary.version", "value") in _problems(world)


def test_validate_rejects_a_template_without_the_binary_token(world: World) -> None:
    _rewrite_manifest(
        world,
        lambda text: text.replace("{{binary}} {{verb}}", "{{verb}}"),
    )
    assert (
        "packages/binpkg/package.toml",
        "transport.invocation_template",
        "value",
    ) in _problems(world)


def test_validate_rejects_a_transport_entry_on_a_binary_package(world: World) -> None:
    _rewrite_manifest(
        world,
        lambda text: text.replace(
            '[transport]\ntype = "cli"', '[transport]\ntype = "cli"\nentry = "stubtool"'
        ),
    )
    assert ("packages/binpkg/package.toml", "transport.entry", "value") in _problems(world)


def test_validate_rejects_engine_python_on_a_binary_package(world: World) -> None:
    _rewrite_manifest(
        world,
        lambda text: text.replace(
            '[engine]\ntype = "binary"', '[engine]\ntype = "binary"\npython = "3.12"'
        ),
    )
    assert ("packages/binpkg/package.toml", "engine.python", "value") in _problems(world)


# --- pull: verified extraction, skipped when present, rebuilt when gone.


def test_pull_downloads_verifies_and_then_skips(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    home = Home.from_env()
    package = _binpkg(world)

    code, envelope = _pull(capsys, "pull", "binpkg")
    assert code == 0
    data = _data(envelope)
    assert data["already_installed"] is False
    assert data["downloaded_bytes"] == len(world.archive)
    target = home.bin_dir / BINARY_NAME / BINARY_VERSION
    resolved = target / "bin" / "stubtool"
    assert resolved.is_file()
    assert resolved.stat().st_mode & 0o111
    assert (target / "bin" / "libstub.dat").is_file()
    marker = (target / env_binary.BINARY_MARKER).read_text(encoding="utf-8")
    assert marker.strip() == world.sha256
    # No python environment is built for a binary package.
    assert not (home.env_dir("binpkg", "0.1.0") / ".complete").exists()
    assert env_module.is_installed(package, home) is True

    hits = len(world.server.hits)
    code, envelope = _pull(capsys, "pull", "binpkg")
    assert code == 0
    second = _data(envelope)
    assert second["already_installed"] is True
    assert second["downloaded_bytes"] == 0
    assert len(world.server.hits) == hits


def test_pull_rebuilds_after_the_extraction_is_removed(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    home = Home.from_env()
    package = _binpkg(world)
    binary = package.binary
    assert binary is not None
    _pull(capsys, "pull", "binpkg")

    shutil.rmtree(home.bin_dir / BINARY_NAME)
    assert env_module.binary_present(binary, home) is False
    code, envelope = _pull(capsys, "pull", "binpkg")
    assert code == 0
    assert _data(envelope)["already_installed"] is False
    assert (home.bin_dir / BINARY_NAME / BINARY_VERSION / "bin" / "stubtool").is_file()
    assert env_module.binary_present(binary, home) is True


def test_a_tampered_archive_fails_with_download_failed(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.server.archive = b"not the pinned zip"

    code, envelope = _pull(capsys, "pull", "binpkg")

    assert code == EXIT_DOWNLOAD_FAILED
    error = envelope["error"]
    assert isinstance(error, dict)
    assert error["code"] == "download_failed"
    home = Home.from_env()
    assert not (home.bin_dir / BINARY_NAME).exists()
    assert list(home.tmp_dir.iterdir()) == []


def test_an_archive_without_the_declared_bin_fails_with_download_failed(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = _zip_without_the_bin()
    world.server.archive = archive
    _rewrite_manifest(
        world,
        lambda text: text.replace(world.sha256, hashlib.sha256(archive).hexdigest()),
    )

    code, envelope = _pull(capsys, "pull", "binpkg")

    assert code == EXIT_DOWNLOAD_FAILED
    error = envelope["error"]
    assert isinstance(error, dict)
    assert error["code"] == "download_failed"
    home = Home.from_env()
    assert not (home.bin_dir / BINARY_NAME).exists()
    assert list(home.tmp_dir.iterdir()) == []


# --- run: argv[0] is the resolved binary, PATH its parent, FACET_BIN its path.


def test_run_uses_the_resolved_binary_and_exports_facets_env(
    world: World, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    piece = tmp_path / "piece.toml"
    piece.write_text('[inputs]\ntext = "hi"\n', encoding="utf-8")
    out = tmp_path / "out"

    code = main(["run", "binpkg.echo", str(piece), "--out", str(out), "--json"])
    envelope = json.loads(capsys.readouterr().out)
    assert code == 0, envelope
    assert envelope["data"]["pulled"] is True

    home = Home.from_env()
    resolved = home.bin_dir / BINARY_NAME / BINARY_VERSION / "bin" / "stubtool"
    payload = json.loads((out / "bin-out.txt").read_text(encoding="utf-8"))
    assert payload["text"] == "hi"
    seen = payload["seen"]
    assert seen["argv"][0] == str(resolved)
    assert seen["argv"][1] == "run"
    assert seen["facet_bin"] == str(resolved)
    assert seen["path_head"] == str(resolved.parent)


# --- doctor: the pull-fixable check and the machine_cannot split.


def test_doctor_reports_a_missing_extraction_with_the_pull_fix(world: World) -> None:
    home = Home.from_env()
    package = _binpkg(world)

    checks, _failures = doctor_module.machine_checks(package, home)

    assert checks["installed"]["ok"] is False
    assert str(home.bin_dir) in str(checks["installed"]["detail"])
    assert checks["installed"]["fix"] == "facet pull binpkg"


def test_doctor_reports_machine_cannot_when_the_version_is_wrong(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _envelope = _pull(capsys, "pull", "binpkg")
    assert code == 0
    home = Home.from_env()
    package = _binpkg(world)
    resolved = home.bin_dir / BINARY_NAME / BINARY_VERSION / "bin" / "stubtool"
    resolved.write_text(
        STUB.replace("{python}", sys.executable).replace("{version}", "9.9.9"),
        encoding="utf-8",
    )

    checks, _failures, doctor_errors = doctor_module.run_check(package, home, "", "run-doctor")

    assert checks["installed"]["ok"] is False
    assert "9.9.9" in str(checks["installed"]["detail"])
    assert "1.2.3" in str(checks["installed"]["detail"])
    assert checks["installed"]["fix"] != "facet pull binpkg"
    assert any(error.code == "machine_cannot" for error in doctor_errors)


def test_doctor_reports_machine_cannot_when_the_binary_cannot_run(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _envelope = _pull(capsys, "pull", "binpkg")
    assert code == 0
    home = Home.from_env()
    package = _binpkg(world)
    resolved = home.bin_dir / BINARY_NAME / BINARY_VERSION / "bin" / "stubtool"
    resolved.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

    checks, _failures, doctor_errors = doctor_module.run_check(package, home, "", "run-doctor")

    assert checks["installed"]["ok"] is False
    assert checks["installed"]["fix"] != "facet pull binpkg"
    assert any(error.code == "machine_cannot" for error in doctor_errors)
