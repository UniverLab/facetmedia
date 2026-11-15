"""``facet preview <folder>``: the folder data, the gallery routes, the fallback."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from facetmedia import preview as preview_module
from facetmedia.cli import main
from facetmedia.preview_folder import FolderAsset, collect
from facetmedia.preview_server import GallerySite, make_gallery_server

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
EXIT_OK = 0
EXIT_INVALID_INPUT = 2
HTTP_OK = 200
HTTP_FOUND = 302
HTTP_NOT_FOUND = 404
BASE_MTIME = 1_700_000_000.0
DURATION_S = 12.5
ASSETS = ("a/first.png", "b/first.png", "c/doc.pdf", "d/notes.txt", "e/clip.txt")
STATES = ("fresh", "stale", "fresh", "fresh", "missing")
ENTRY_KEYS = {
    "asset",
    "bytes",
    "capability",
    "created",
    "duration_s",
    "facet",
    "kind",
    "preview",
    "preview_state",
    "verified",
}
CARD_KEYS = {
    "asset",
    "bytes",
    "capability",
    "created",
    "duration_s",
    "facet",
    "kind",
    "n",
    "name",
    "open",
    "package",
    "preview_state",
    "thumb",
    "verified",
}


def _touch(path: Path, offset: float) -> None:
    """Set both timestamps ``offset`` seconds from the shared base."""
    stamp = BASE_MTIME + offset
    os.utime(path, (stamp, stamp))


def _record(
    path: Path,
    kind: str,
    *,
    status: str = "passed",
    duration: float | None = None,
    capability: str = "echo.text",
) -> None:
    """Write a plain record beside ``path``; only the fields previews read."""
    properties: dict[str, object] = {}
    if duration is not None:
        properties["duration_s"] = duration
    payload: dict[str, object] = {
        "record": 1,
        "name": path.stem,
        "path": path.name,
        "kind": kind,
        "mime": "application/octet-stream",
        "bytes": path.stat().st_size,
        "created": "2026-11-12T10:00:00Z",
        "properties": properties,
        "provenance": {
            "capability": capability,
            "package": {"name": "echo", "version": "0.1.0"},
        },
        "verification": {"status": status},
    }
    path.parent.joinpath(path.name + ".facet.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _preview(asset: Path, payload: dict[str, object], offset: float) -> Path:
    """Write ``<asset>.preview.json`` and stamp it ``offset`` from the base."""
    target = asset.parent / (asset.name + ".preview.json")
    target.write_text(json.dumps(payload), encoding="utf-8")
    _touch(target, offset)
    return target


@pytest.fixture(autouse=True)
def _active_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Folder previews read the fixture registry, never the network."""
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))


@pytest.fixture
def pieces(tmp_path: Path) -> Path:
    """A folder of five recorded assets: fresh, stale, missing, nested, reused name."""
    root = tmp_path / "pieces"
    for name in ASSETS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 10)
        _touch(path, 0)
    _record(root / "a/first.png", "image")
    _preview(root / "a/first.png", {"preview": 1, "kind": "image", "summary": "a"}, 5)
    _record(root / "b/first.png", "image", status="failed", duration=DURATION_S)
    _preview(root / "b/first.png", {"preview": 1, "kind": "image", "summary": "b"}, -5)
    (root / "c" / "preview").mkdir(parents=True, exist_ok=True)
    (root / "c" / "preview" / "page-01.png").write_bytes(b"\x89PNG-page")
    _record(root / "c/doc.pdf", "document")
    _preview(
        root / "c/doc.pdf",
        {"preview": 1, "kind": "document", "page_count": 1, "pages": ["preview/page-01.png"]},
        5,
    )
    _record(root / "d/notes.txt", "text")
    _preview(root / "d/notes.txt", {"preview": 1, "kind": "text", "summary": "notes"}, 5)
    _record(root / "e/clip.txt", "text")
    (root / "stray.txt").write_text("no record", encoding="utf-8")
    return root


@pytest.fixture
def gallery_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A registry copy carrying a stub gallery viewer."""
    root = tmp_path / "registry"
    shutil.copytree(FIXTURES, root)
    page = root / "viewers" / "gallery"
    page.mkdir()
    (page / "index.html").write_text("<html>gallery</html>", encoding="utf-8")
    monkeypatch.setenv("FACET_REGISTRY", str(root))
    return root


def _site(pieces: Path, registry_root: Path) -> GallerySite:
    viewers = registry_root / "viewers"
    return GallerySite(
        gallery_root=viewers / "gallery",
        kit_root=viewers / "_kit",
        folder_name=pieces.name,
        assets=collect(pieces),
    )


@contextmanager
def _running(site: GallerySite) -> Iterator[int]:
    """Serve ``site`` on a free local port for the duration of the block."""
    server = make_gallery_server(0, site)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        thread.join(timeout=10)
        server.server_close()


def _get(port: int, path: str) -> tuple[int, dict[str, str], bytes]:
    """One raw GET, so redirects stay visible."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _envelope(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    parsed: Any = json.loads(capsys.readouterr().out)
    assert isinstance(parsed, dict)
    return parsed


def _paths(entries: tuple[FolderAsset, ...]) -> list[str]:
    return [entry.asset for entry in entries]


# --- the folder data.


def test_folder_json_lists_every_recorded_asset(
    capsys: pytest.CaptureFixture[str], pieces: Path
) -> None:
    """Nested assets, a reused file name, the three states and the record fields."""
    assert main(["preview", str(pieces), "--json"]) == EXIT_OK
    data = _envelope(capsys)["data"]
    assert data["folder"] == "pieces"
    entries = data["assets"]
    assert [entry["asset"] for entry in entries] == list(ASSETS)
    assert "stray.txt" not in [entry["asset"] for entry in entries]
    assert [entry["preview_state"] for entry in entries] == list(STATES)
    assert [entry["kind"] for entry in entries] == [
        "image",
        "image",
        "document",
        "text",
        "text",
    ]
    assert [entry["verified"] for entry in entries] == [True, False, True, True, True]
    assert [entry["duration_s"] for entry in entries] == [
        None,
        DURATION_S,
        None,
        None,
        None,
    ]
    assert [entry["capability"] for entry in entries] == ["echo.text"] * 5
    assert [entry["bytes"] for entry in entries] == [10] * 5
    assert [entry["created"] for entry in entries] == ["2026-11-12T10:00:00Z"] * 5
    assert all(set(entry) == ENTRY_KEYS for entry in entries)
    assert entries[0]["preview"] == {"preview": 1, "kind": "image", "summary": "a"}
    assert entries[1]["preview"] is None
    assert entries[2]["preview"]["pages"] == ["preview/page-01.png"]
    assert entries[4]["preview"] is None


def test_folder_json_keeps_the_two_assets_that_share_a_name(
    capsys: pytest.CaptureFixture[str], pieces: Path
) -> None:
    """``a/first.png`` and ``b/first.png`` are two cards, not one."""
    assert main(["preview", str(pieces), "--json"]) == EXIT_OK
    entries = _envelope(capsys)["data"]["assets"]
    firsts = [entry["asset"] for entry in entries if Path(entry["asset"]).name == "first.png"]
    assert firsts == ["a/first.png", "b/first.png"]


def test_folder_json_without_an_argument_reads_the_current_folder(
    capsys: pytest.CaptureFixture[str], pieces: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No argument means the current directory."""
    monkeypatch.chdir(pieces)
    assert main(["preview", "--json"]) == EXIT_OK
    data = _envelope(capsys)["data"]
    assert data["folder"] == "pieces"
    assert [entry["asset"] for entry in data["assets"]] == list(ASSETS)


def test_kind_keeps_only_that_kind(capsys: pytest.CaptureFixture[str], pieces: Path) -> None:
    assert main(["preview", str(pieces), "--kind", "image", "--json"]) == EXIT_OK
    data = _envelope(capsys)["data"]
    assert [entry["asset"] for entry in data["assets"]] == ["a/first.png", "b/first.png"]
    assert main(["preview", str(pieces), "--kind", "document", "--json"]) == EXIT_OK
    assert _paths_only(_envelope(capsys)) == ["c/doc.pdf"]


def _paths_only(envelope: dict[str, Any]) -> list[str]:
    data = envelope["data"]
    assert isinstance(data, dict)
    return [entry["asset"] for entry in data["assets"]]


def test_empty_folder_is_invalid_input_naming_it(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    empty = tmp_path / "nothing-here"
    empty.mkdir()
    assert main(["preview", str(empty), "--json"]) == EXIT_INVALID_INPUT
    error = _envelope(capsys)["error"]
    assert error["code"] == "invalid_input"
    assert str(empty) in error["message"]


def test_kind_filter_that_keeps_nothing_is_invalid_input(
    capsys: pytest.CaptureFixture[str], pieces: Path
) -> None:
    assert main(["preview", str(pieces), "--kind", "video", "--json"]) == EXIT_INVALID_INPUT
    error = _envelope(capsys)["error"]
    assert error["code"] == "invalid_input"
    assert str(pieces) in error["message"]


def test_a_fresh_preview_that_does_not_parse_is_invalid_input(
    capsys: pytest.CaptureFixture[str], pieces: Path
) -> None:
    """The file is named, as the single-asset form already does."""
    asset = pieces / "d/notes.txt"
    broken = asset.parent / (asset.name + ".preview.json")
    broken.write_text("not json", encoding="utf-8")
    _touch(broken, 5)
    assert main(["preview", str(pieces), "--json"]) == EXIT_INVALID_INPUT
    error = _envelope(capsys)["error"]
    assert error["code"] == "invalid_input"
    assert str(broken) in error["message"]


def test_a_record_without_a_capability_aborts_the_folder(
    capsys: pytest.CaptureFixture[str], pieces: Path
) -> None:
    record_path = pieces / "d/notes.txt.facet.json"
    stored = json.loads(record_path.read_text(encoding="utf-8"))
    stored["provenance"] = {"package": {"name": "echo", "version": "0.1.0"}}
    record_path.write_text(json.dumps(stored), encoding="utf-8")
    assert main(["preview", str(pieces), "--json"]) == EXIT_INVALID_INPUT
    assert _envelope(capsys)["error"]["code"] == "invalid_input"


# --- the gallery server's routes.


def test_gallery_json_carries_one_card_per_asset(pieces: Path, gallery_registry: Path) -> None:
    with _running(_site(pieces, gallery_registry)) as port:
        status, _headers, body = _get(port, "/gallery.json")
    assert status == HTTP_OK
    data = json.loads(body)
    assert data["folder"] == "pieces"
    cards = data["assets"]
    assert [card["n"] for card in cards] == [0, 1, 2, 3, 4]
    assert [card["asset"] for card in cards] == list(ASSETS)
    assert all(set(card) == CARD_KEYS for card in cards)
    assert [card["thumb"] for card in cards] == [
        "/thumb/0",
        "/thumb/1",
        "/thumb/2",
        None,
        None,
    ]
    assert [card["open"] for card in cards] == [
        "/open/0",
        None,
        "/open/2",
        "/open/3",
        None,
    ]
    assert [card["name"] for card in cards] == ["first", "first", "doc", "notes", "clip"]
    assert {card["package"] for card in cards} == {"echo"}
    assert [card["facet"] for card in cards] == ["text"] * 5
    assert cards[1]["verified"] is False
    assert cards[1]["duration_s"] == DURATION_S
    assert cards[1]["preview_state"] == "stale"


def test_folder_json_names_the_package_facet(
    capsys: pytest.CaptureFixture[str],
    pieces: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A known package names its facet id; an unknown package gives null."""
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))
    record_path = pieces / "e/clip.txt.facet.json"
    stored = json.loads(record_path.read_text(encoding="utf-8"))
    assert isinstance(stored["provenance"], dict)
    stored["provenance"]["package"] = {"name": "ghost", "version": "0.1.0"}
    record_path.write_text(json.dumps(stored), encoding="utf-8")
    assert main(["preview", str(pieces), "--json"]) == EXIT_OK
    entries = _envelope(capsys)["data"]["assets"]
    assert all(set(entry) == ENTRY_KEYS for entry in entries)
    assert [entry["facet"] for entry in entries[:4]] == ["text"] * 4
    assert entries[4]["facet"] is None


def test_folder_json_without_a_readable_registry_gives_null_facets(
    capsys: pytest.CaptureFixture[str],
    pieces: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A registry that cannot load leaves every facet null; nothing else changes."""
    monkeypatch.setenv("FACET_REGISTRY", str(tmp_path / "no-registry"))
    assert main(["preview", str(pieces), "--json"]) == EXIT_OK
    entries = _envelope(capsys)["data"]["assets"]
    assert [entry["facet"] for entry in entries] == [None] * len(entries)
    assert [entry["capability"] for entry in entries] == ["echo.text"] * len(entries)


def test_gallery_card_facet_is_null_for_an_unknown_package(
    pieces: Path, gallery_registry: Path
) -> None:
    """A card whose package left the snapshot carries a null facet."""
    record_path = pieces / "b/first.png.facet.json"
    stored = json.loads(record_path.read_text(encoding="utf-8"))
    assert isinstance(stored["provenance"], dict)
    stored["provenance"]["package"] = {"name": "ghost", "version": "0.1.0"}
    record_path.write_text(json.dumps(stored), encoding="utf-8")
    with _running(_site(pieces, gallery_registry)) as port:
        status, _headers, body = _get(port, "/gallery.json")
    assert status == HTTP_OK
    cards = json.loads(body)["assets"]
    assert [card["facet"] for card in cards] == ["text", None, "text", "text", "text"]


def test_gallery_serves_its_page_and_the_kit(pieces: Path, gallery_registry: Path) -> None:
    with _running(_site(pieces, gallery_registry)) as port:
        status, headers, body = _get(port, "/gallery/index.html")
        kit_status, _kit_headers, _kit_body = _get(port, "/kit/base.css")
    assert status == HTTP_OK
    assert body == b"<html>gallery</html>"
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert kit_status == HTTP_OK


def test_thumb_is_the_image_itself_for_an_image_asset(pieces: Path, gallery_registry: Path) -> None:
    with _running(_site(pieces, gallery_registry)) as port:
        status, _headers, body = _get(port, "/thumb/0")
    assert status == HTTP_OK
    assert body == b"x" * 10


def test_thumb_is_the_first_preview_image_for_a_fresh_asset(
    pieces: Path, gallery_registry: Path
) -> None:
    with _running(_site(pieces, gallery_registry)) as port:
        status, _headers, body = _get(port, "/thumb/2")
    assert status == HTTP_OK
    assert body == b"\x89PNG-page"


def test_thumb_without_a_picture_is_not_found(pieces: Path, gallery_registry: Path) -> None:
    with _running(_site(pieces, gallery_registry)) as port:
        for path in ("/thumb/3", "/thumb/4", "/thumb/99", "/thumb/abc", "/thumb/"):
            assert _get(port, path)[0] == HTTP_NOT_FOUND, path


def test_open_redirects_to_the_viewer_and_serves_the_asset(
    pieces: Path, gallery_registry: Path
) -> None:
    with _running(_site(pieces, gallery_registry)) as port:
        status, headers, _body = _get(port, "/open/3")
        viewer, _viewer_headers, viewer_body = _get(port, "/viewer/index.html")
        asset, _asset_headers, asset_body = _get(port, "/asset/notes.txt")
    assert status == HTTP_FOUND
    assert headers["Location"] == "/viewer/index.html?asset=notes.txt"
    assert viewer == HTTP_OK
    assert b"generic viewer" in viewer_body
    assert asset == HTTP_OK
    assert asset_body == b"x" * 10


def test_opening_another_card_replaces_the_current_asset(
    pieces: Path, gallery_registry: Path
) -> None:
    with _running(_site(pieces, gallery_registry)) as port:
        assert _get(port, "/open/0")[0] == HTTP_FOUND
        assert _get(port, "/asset/first.png")[0] == HTTP_OK
        assert _get(port, "/open/3")[0] == HTTP_FOUND
        assert _get(port, "/asset/first.png")[0] == HTTP_NOT_FOUND
        assert _get(port, "/asset/notes.txt")[0] == HTTP_OK


def test_viewer_and_asset_are_not_found_before_any_open(
    pieces: Path, gallery_registry: Path
) -> None:
    with _running(_site(pieces, gallery_registry)) as port:
        assert _get(port, "/viewer/index.html")[0] == HTTP_NOT_FOUND
        assert _get(port, "/asset/notes.txt")[0] == HTTP_NOT_FOUND
        assert _get(port, "/asset/a/first.png")[0] == HTTP_NOT_FOUND


def test_open_of_a_stale_or_missing_card_is_not_found(pieces: Path, gallery_registry: Path) -> None:
    with _running(_site(pieces, gallery_registry)) as port:
        for path in ("/open/1", "/open/4", "/open/99", "/open/-1", "/open/abc", "/open/"):
            assert _get(port, path)[0] == HTTP_NOT_FOUND, path


def test_open_and_thumb_reject_digits_they_cannot_read(
    pieces: Path, gallery_registry: Path
) -> None:
    """``²`` counts as a digit but not as a number: it is 404, not a crash."""
    with _running(_site(pieces, gallery_registry)) as port:
        for path in ("/open/%C2%B2", "/thumb/%C2%B2", "/open/1%C2%B2"):
            assert _get(port, path)[0] == HTTP_NOT_FOUND, path


def test_open_of_a_document_redirects_to_its_viewer(pieces: Path, gallery_registry: Path) -> None:
    with _running(_site(pieces, gallery_registry)) as port:
        status, headers, _body = _get(port, "/open/2")
        viewer, _viewer_headers, viewer_body = _get(port, "/viewer/index.html")
        pdf_status, pdf_headers, pdf_body = _get(port, "/asset/doc.pdf")
    assert status == HTTP_FOUND
    assert headers["Location"] == "/viewer/index.html?asset=doc.pdf"
    assert viewer == HTTP_OK
    assert b"generic viewer" in viewer_body
    assert pdf_status == HTTP_OK
    assert pdf_headers["Content-Type"] == "application/pdf"
    assert pdf_body == b"x" * 10


def test_open_a_document_serves_pages_under_preview(pieces: Path, gallery_registry: Path) -> None:
    """The deck runner writes its pages to ``preview/``: that path is served."""
    with _running(_site(pieces, gallery_registry)) as port:
        assert _get(port, "/open/2")[0] == HTTP_FOUND
        status, headers, body = _get(port, "/asset/preview/page-01.png")
        assert _get(port, "/asset/page-01.png")[0] == HTTP_NOT_FOUND
    assert status == HTTP_OK
    assert headers["Content-Type"] == "image/png"
    assert body == b"\x89PNG-page"


def test_gallery_path_traversal_is_not_found(pieces: Path, gallery_registry: Path) -> None:
    with _running(_site(pieces, gallery_registry)) as port:
        for path in (
            "/gallery/..%2f..%2fetc/hostname",
            "/gallery/%2e%2e/index.html",
            "/gallery/",
            "/gallery",
            "/gallery.json/",
            "/kit/..%2f..%2fetc/hostname",
            "/nope",
        ):
            assert _get(port, path)[0] == HTTP_NOT_FOUND, path


def test_asset_traversal_is_not_found_after_an_open(pieces: Path, gallery_registry: Path) -> None:
    with _running(_site(pieces, gallery_registry)) as port:
        assert _get(port, "/open/3")[0] == HTTP_FOUND
        for path in (
            "/asset/..%2fd/notes.txt",
            "/asset/%2e%2e/a/first.png",
            "/asset/stray.txt",
            "/asset/",
        ):
            assert _get(port, path)[0] == HTTP_NOT_FOUND, path


# --- the person form and the fallback.


def test_person_gets_the_gallery_url(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    pieces: Path,
    gallery_registry: Path,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(preview_module, "open_url", opened.append)
    monkeypatch.setattr(preview_module, "_is_tty", lambda: True)
    assert main(["preview", str(pieces), "--no-open", "--idle-timeout", "1"]) == EXIT_OK
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("http://127.0.0.1:")
    assert lines[0].endswith("/gallery/index.html")
    assert opened == []


def test_person_gallery_opens_the_browser_once(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    pieces: Path,
    gallery_registry: Path,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(preview_module, "open_url", opened.append)
    monkeypatch.setattr(preview_module, "_is_tty", lambda: True)
    assert main(["preview", str(pieces), "--idle-timeout", "1"]) == EXIT_OK
    lines = capsys.readouterr().out.splitlines()
    assert opened == lines


def test_gallery_port_conflict_is_invalid_input(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    pieces: Path,
    gallery_registry: Path,
) -> None:
    monkeypatch.setattr(preview_module, "_is_tty", lambda: True)
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        port = blocker.getsockname()[1]
        assert main(["preview", str(pieces), "--no-open", "--port", str(port)]) == (
            EXIT_INVALID_INPUT
        )
    finally:
        blocker.close()
    assert "cannot serve" in capsys.readouterr().err


def test_without_a_gallery_viewer_the_person_gets_a_table(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    pieces: Path,
    tmp_path: Path,
) -> None:
    root = tmp_path / "old-registry"
    shutil.copytree(FIXTURES, root)
    assert not (root / "viewers" / "gallery").exists()
    monkeypatch.setenv("FACET_REGISTRY", str(root))
    monkeypatch.setattr(preview_module, "_is_tty", lambda: True)
    monkeypatch.setattr(preview_module, "make_gallery_server", _no_server)

    assert main(["preview", str(pieces)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "pieces/" in out
    assert "a/first.png" in out and "e/clip.txt" in out
    assert "fresh" in out and "stale" in out and "missing" in out


def test_a_pipe_gets_the_cards_as_data(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    pieces: Path,
    gallery_registry: Path,
) -> None:
    """No terminal, no server: the cards go to stdout as JSON."""
    monkeypatch.setattr(preview_module, "_is_tty", lambda: False)
    monkeypatch.setattr(preview_module, "make_gallery_server", _no_server)
    assert main(["preview", str(pieces)]) == EXIT_OK
    out = capsys.readouterr().out
    data: Any = json.loads(out)
    assert data["folder"] == "pieces"
    assert data["assets"][0]["thumb"] == "/thumb/0"
    assert data["assets"][4]["open"] is None


def _no_server(*args: object, **kwargs: object) -> Any:
    raise AssertionError("no server may start here")
