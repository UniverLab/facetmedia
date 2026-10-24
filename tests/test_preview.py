"""``facet preview``: agent JSON, person viewer serving, openers, staleness."""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

import facetmedia.cli as cli_module
from facetmedia import preview as preview_module
from facetmedia import record as record_module
from facetmedia.cli import build_parser, main
from facetmedia.preview_server import Site, make_server, serve
from facetmedia.record import RecordSpec

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
EXIT_NOT_INSTALLED = 7
EXIT_INVALID_INPUT = 2
VIEWERS = FIXTURES / "viewers"
HTTP_OK = 200
HTTP_NOT_FOUND = 404
DEFAULT_IDLE_TIMEOUT_S = 600.0


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
def echo_asset(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    facet_home: Path,
) -> Path:
    """An echo asset built by ``facet run``; its preview is not yet made."""
    assert facet_home.is_dir()
    piece = tmp_path / "piece.toml"
    piece.write_text('[inputs]\ntext = "hello"\nrepeat = 2\n', encoding="utf-8")
    out = tmp_path / "out"
    assert main(["run", "echo.text", str(piece), "--out", str(out), "--json"]) == 0
    capsys.readouterr()
    return out / "echo.txt"


def _write_echo_record(asset: Path) -> None:
    spec = RecordSpec(
        name="echo",
        kind="text",
        mime="text/plain",
        facet="0.1.0b1",
        capability="echo.text",
        package={"name": "echo", "version": "0.1.0", "lock_sha256": "abc"},
        tool={"name": "echo", "version": "0.1.0"},
        params={"text": "hello", "repeat": 2},
    )
    asset.write_text("hellohello", encoding="utf-8")
    record_module.write_record(asset, record_module.build_record(asset, spec))


def _fail_if_called(*args: object, **kwargs: object) -> Any:
    raise AssertionError("no server may start for --json")


def _envelope(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    """The single JSON document facet printed on stdout."""
    parsed: Any = json.loads(capsys.readouterr().out)
    assert isinstance(parsed, dict)
    return parsed


def _preview_path(asset: Path) -> Path:
    return asset.parent / (asset.name + ".preview.json")


# --- T1: --json returns the preview and starts no server.


def test_json_returns_preview_and_starts_no_server(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, echo_asset: Path
) -> None:
    monkeypatch.setattr(preview_module, "serve", _fail_if_called)
    monkeypatch.setattr(preview_module, "open_url", _fail_if_called)

    assert main(["preview", str(echo_asset), "--json"]) == 0
    envelope = _envelope(capsys)
    assert envelope["ok"] is True
    data = envelope["data"]
    assert isinstance(data, dict)
    assert data["asset"] == Path(os.path.relpath(echo_asset, Path.cwd())).as_posix()
    stored = json.loads((echo_asset.parent / "echo.txt.facet.json").read_text(encoding="utf-8"))
    assert data["record"] == stored
    assert data["preview"] == json.loads(_preview_path(echo_asset).read_text(encoding="utf-8"))
    assert data["preview"]["summary"] == "hellohello"
    assert data["images"] == []


def test_json_regenerates_a_stale_preview(
    capsys: pytest.CaptureFixture[str], echo_asset: Path
) -> None:
    assert main(["preview", str(echo_asset), "--json"]) == 0
    _envelope(capsys)
    preview_file = _preview_path(echo_asset)
    first = json.loads(preview_file.read_text(encoding="utf-8"))
    assert first["summary"] == "hellohello"

    echo_asset.write_text("changed", encoding="utf-8")
    fresh = preview_file.stat().st_mtime + 5
    os.utime(echo_asset, (fresh, fresh))

    assert main(["preview", str(echo_asset), "--json"]) == 0
    second = _envelope(capsys)["data"]
    assert isinstance(second, dict)
    assert second["preview"] == json.loads(preview_file.read_text(encoding="utf-8"))
    assert second["preview"]["summary"] == "changed"


# --- T2: a person gets one URL line; --no-open never opens.


def test_person_opens_the_browser_once(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, echo_asset: Path
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(preview_module, "open_url", opened.append)

    assert main(["preview", str(echo_asset), "--idle-timeout", "1"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("http://127.0.0.1:")
    assert opened == lines


def test_person_no_open_never_opens(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, echo_asset: Path
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(preview_module, "open_url", opened.append)

    assert main(["preview", str(echo_asset), "--no-open", "--idle-timeout", "1"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("http://127.0.0.1:")
    assert opened == []


# --- T3: the server serves only the three roots.


def _trio_site(directory: Path) -> Site:
    """An asset dir with the echo trio, two listed images and an unlisted file."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "echo.txt").write_text("hellohello", encoding="utf-8")
    (directory / "echo.txt.facet.json").write_text('{"record": 1}', encoding="utf-8")
    preview = {
        "preview": 1,
        "kind": "text",
        "summary": "hello",
        "images": ["shot.png", "gone.png"],
        "views": [{"file": "plate.png"}],
    }
    (directory / "echo.txt.preview.json").write_text(json.dumps(preview), encoding="utf-8")
    (directory / "shot.png").write_bytes(b"\x89PNG-shot")
    (directory / "plate.png").write_bytes(b"\x89PNG-plate")
    (directory / "other.txt").write_text("unlisted", encoding="utf-8")
    return Site(
        viewer_root=VIEWERS / "generic",
        kit_root=VIEWERS / "_kit",
        asset_dir=directory,
        asset_names=frozenset(
            {
                "echo.txt",
                "echo.txt.facet.json",
                "echo.txt.preview.json",
                "shot.png",
                "plate.png",
            }
        ),
    )


def _get(port: int, path: str) -> int:
    """The status of one GET against the test server."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _get_type(port: int, path: str) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
        response.read()
        content_type = response.headers.get("Content-Type")
        assert isinstance(content_type, str)
        return content_type


def test_server_serves_only_its_three_roots(tmp_path: Path) -> None:
    site = _trio_site(tmp_path / "assets")
    server = make_server(0, site)
    assert server.server_address[0] == "127.0.0.1"
    port = server.server_address[1]
    assert port != 0
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        for path in (
            "/viewer/index.html",
            "/kit/base.css",
            "/asset/echo.txt",
            "/asset/echo.txt.facet.json",
            "/asset/echo.txt.preview.json",
            "/asset/shot.png",
            "/asset/plate.png",
        ):
            assert _get(port, path) == HTTP_OK, path
        assert _get_type(port, "/viewer/index.html") == "text/html; charset=utf-8"
        assert _get_type(port, "/kit/base.css") == "text/css; charset=utf-8"
        for path in (
            "/asset/other.txt",
            "/asset/gone.png",
            "/asset/",
            "/asset",
            "/nope",
            "/viewer/",
            "/asset/..%2fother.txt",
            "/asset/%2e%2e/other.txt",
            "/..%2f..%2fetc/hostname",
        ):
            assert _get(port, path) == HTTP_NOT_FOUND, path
    finally:
        server.shutdown()
        thread.join(timeout=10)
        server.server_close()


# --- T4: the server stops on idle timeout.


def test_serve_stops_on_idle_timeout(tmp_path: Path) -> None:
    directory = tmp_path / "assets"
    directory.mkdir()
    (directory / "a.txt").write_text("a", encoding="utf-8")
    site = Site(
        viewer_root=VIEWERS / "generic",
        kit_root=VIEWERS / "_kit",
        asset_dir=directory,
        asset_names=frozenset({"a.txt"}),
    )
    server = make_server(0, site)
    thread = threading.Thread(target=serve, args=(server, 1.0), daemon=True)
    thread.start()
    assert _get(server.server_address[1], "/asset/a.txt") == HTTP_OK
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert server.fileno() == -1


# --- T5: the opener matrix, with patched platform probes.


def test_opener_prefers_wslview_inside_wsl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preview_module, "_is_wsl", lambda: True)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/wslview")
    assert preview_module.opener_argv("http://127.0.0.1:9/") == [
        "wslview",
        "http://127.0.0.1:9/",
    ]


def test_opener_falls_back_to_explorer_inside_wsl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preview_module, "_is_wsl", lambda: True)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    assert preview_module.opener_argv("http://127.0.0.1:9/") == [
        "explorer.exe",
        "http://127.0.0.1:9/",
    ]


def test_opener_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preview_module, "_is_wsl", lambda: False)
    monkeypatch.setattr(sys, "platform", "linux")
    assert preview_module.opener_argv("http://127.0.0.1:9/") == [
        "xdg-open",
        "http://127.0.0.1:9/",
    ]


def test_opener_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preview_module, "_is_wsl", lambda: False)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert preview_module.opener_argv("http://127.0.0.1:9/") == ["open", "http://127.0.0.1:9/"]


def test_is_wsl_reads_both_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preview_module, "_wsl_interop", lambda: False)
    monkeypatch.setattr(preview_module, "_proc_version", lambda: "Linux Microsoft x86_64")
    assert preview_module._is_wsl() is True
    monkeypatch.setattr(preview_module, "_proc_version", lambda: "Linux generic x86_64")
    assert preview_module._is_wsl() is False
    monkeypatch.setattr(preview_module, "_wsl_interop", lambda: True)
    assert preview_module._is_wsl() is True


def test_open_url_without_opener_keeps_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    def _missing(_argv: list[str], **_kwargs: object) -> Any:
        raise FileNotFoundError("xdg-open")

    monkeypatch.setattr(preview_module.subprocess, "Popen", _missing)
    preview_module.open_url("http://127.0.0.1:9/")


def test_preview_accepts_the_record_path(
    capsys: pytest.CaptureFixture[str], echo_asset: Path
) -> None:
    assert main(["preview", str(echo_asset), "--json"]) == 0
    _envelope(capsys)
    assert main(["preview", f"{echo_asset}.facet.json", "--json"]) == 0
    data = _envelope(capsys)["data"]
    assert isinstance(data, dict)
    assert data["asset"] == Path(os.path.relpath(echo_asset, Path.cwd())).as_posix()


def _bare_record(asset: Path, payload: dict[str, object]) -> None:
    asset.write_text("hellohello", encoding="utf-8")
    asset.parent.joinpath(asset.name + ".facet.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_preview_record_without_provenance_is_invalid_input(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, facet_home: Path
) -> None:
    assert facet_home.is_dir()
    _bare_record(tmp_path / "echo.txt", {"record": 1})
    assert main(["preview", str(tmp_path / "echo.txt"), "--json"]) == EXIT_INVALID_INPUT
    assert _envelope(capsys)["error"]["code"] == "invalid_input"


def test_preview_record_without_capability_or_params_is_invalid_input(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, facet_home: Path
) -> None:
    assert facet_home.is_dir()
    asset = tmp_path / "echo.txt"
    _bare_record(asset, {"record": 1, "provenance": {"params": {"text": "hello"}}})
    assert main(["preview", str(asset), "--json"]) == EXIT_INVALID_INPUT
    assert _envelope(capsys)["error"]["code"] == "invalid_input"
    _bare_record(asset, {"record": 1, "provenance": {"capability": "echo.text"}})
    assert main(["preview", str(asset), "--json"]) == EXIT_INVALID_INPUT
    assert _envelope(capsys)["error"]["code"] == "invalid_input"


def test_preview_missing_asset_is_invalid_input(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, facet_home: Path
) -> None:
    assert facet_home.is_dir()
    asset = tmp_path / "echo.txt"
    _write_echo_record(asset)
    asset.unlink()
    assert main(["preview", str(asset), "--json"]) == EXIT_INVALID_INPUT
    assert _envelope(capsys)["error"]["code"] == "invalid_input"


def test_preview_unreadable_file_is_invalid_input(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, facet_home: Path
) -> None:
    assert facet_home.is_dir()
    asset = tmp_path / "echo.txt"
    _write_echo_record(asset)
    preview_file = _preview_path(asset)
    preview_file.write_text("not json", encoding="utf-8")
    newer = asset.stat().st_mtime + 5
    os.utime(preview_file, (newer, newer))
    assert main(["preview", str(asset), "--json"]) == EXIT_INVALID_INPUT
    assert _envelope(capsys)["error"]["code"] == "invalid_input"
    preview_file.write_text("[1, 2]", encoding="utf-8")
    os.utime(preview_file, (newer, newer))
    assert main(["preview", str(asset), "--json"]) == EXIT_INVALID_INPUT
    assert _envelope(capsys)["error"]["code"] == "invalid_input"


def test_preview_port_conflict_is_invalid_input(
    capsys: pytest.CaptureFixture[str], echo_asset: Path
) -> None:
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        port = blocker.getsockname()[1]
        assert main(["preview", str(echo_asset), "--no-open", "--port", str(port)]) == (
            EXIT_INVALID_INPUT
        )
    finally:
        blocker.close()
    assert "cannot serve" in capsys.readouterr().err


def test_images_keeps_only_files_inside_the_asset_dir(tmp_path: Path) -> None:
    asset_dir = tmp_path / "assets"
    asset_dir.mkdir()
    (asset_dir / "ok.png").write_bytes(b"ok")
    (asset_dir / "sub").mkdir()
    (asset_dir / "sub" / "n.png").write_bytes(b"n")
    outside = tmp_path / "out.png"
    outside.write_bytes(b"outside but listed")
    preview = {
        "images": [
            "ok.png",
            "ok.png",
            "../out.png",
            "/abs.png",
            "",
            "missing.png",
            42,
            "sub/n.png",
        ],
        "views": [{"file": "ok.png"}, {"file": "../out.png"}, {"nofile": "x"}, "oops", 7],
    }
    base = asset_dir.resolve()
    assert preview_module._images(preview, asset_dir) == [base / "ok.png", base / "sub" / "n.png"]
    assert preview_module._images({"images": "shot.png"}, asset_dir) == []
    assert preview_module._images({"views": {"file": "ok.png"}}, asset_dir) == []


def test_proc_probes_read_the_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert isinstance(preview_module._proc_version(), str)
    assert isinstance(preview_module._wsl_interop(), bool)

    def _missing(self: object, *args: object, **kwargs: object) -> str:
        raise OSError("no proc")

    monkeypatch.setattr("pathlib.Path.read_text", _missing)
    assert preview_module._proc_version() == ""


# --- T6: a record whose package is gone gives not_installed.


def test_package_gone_is_not_installed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FACET_HOME", str(tmp_path / "fresh-home"))
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))
    asset = tmp_path / "echo.txt"
    _write_echo_record(asset)

    assert main(["preview", str(asset), "--json"]) == EXIT_NOT_INSTALLED
    envelope = _envelope(capsys)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "not_installed"
    assert envelope["error"]["hint"] == "facet pull echo"


# --- T7: without any viewer, the preview JSON is printed and nothing opens.


def test_missing_viewer_prints_preview_json(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, echo_asset: Path
) -> None:
    unveiled = echo_asset.parent / "unveiled"
    shutil.copytree(FIXTURES, unveiled / "reg")
    shutil.rmtree(unveiled / "reg" / "viewers")
    monkeypatch.setenv("FACET_REGISTRY", str(unveiled / "reg"))
    opened: list[str] = []
    monkeypatch.setattr(preview_module, "open_url", opened.append)

    assert main(["preview", str(echo_asset)]) == 0
    assert opened == []
    assert json.loads(capsys.readouterr().out) == json.loads(
        _preview_path(echo_asset).read_text(encoding="utf-8")
    )


def test_preview_takes_no_lock(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, echo_asset: Path
) -> None:
    """``preview --json`` never touches the facet lock."""
    real = cli_module.lock.hold

    def _fail(*args: object, **kwargs: object) -> Any:
        raise AssertionError("preview takes no lock")

    monkeypatch.setattr(cli_module.lock, "hold", _fail)
    try:
        assert main(["preview", str(echo_asset), "--json"]) == 0
    finally:
        monkeypatch.setattr(cli_module.lock, "hold", real)
    _envelope(capsys)


def test_preview_flags_default_to_the_spec() -> None:
    args = build_parser().parse_args(["preview", "echo.txt"])
    assert args.asset == "echo.txt"
    assert args.no_open is False
    assert args.port is None
    assert args.idle_timeout == DEFAULT_IDLE_TIMEOUT_S
    assert args.json is False


def test_last_activity_starts_at_bind(tmp_path: Path) -> None:
    before = time.monotonic()
    server = make_server(0, _trio_site(tmp_path / "assets"))
    try:
        assert before <= server.last_activity <= time.monotonic()
    finally:
        server.server_close()
