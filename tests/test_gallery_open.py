"""The gallery builds a missing or stale preview when its card is opened."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from facetmedia import runner as runner_module
from facetmedia.cli import main
from facetmedia.contract import FacetError
from facetmedia.preview_folder import collect
from facetmedia.preview_server import GallerySite, make_gallery_server
from tests import test_preview as _preview_tests
from tests.test_preview_folder import _get, _running

seed_home = _preview_tests.seed_home

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
BASE_MTIME = 1_700_000_000.0
HTTP_OK = 200
HTTP_FOUND = 302
HTTP_SERVER_ERROR = 500
EXIT_OK = 0


@pytest.fixture
def facet_home(seed_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The home with the echo package installed, against the fixture registry."""
    monkeypatch.setenv("FACET_HOME", str(seed_home))
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))
    return seed_home


@pytest.fixture
def bare_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A home with nothing installed, against the fixture registry."""
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))
    return tmp_path / "facet-home"


@pytest.fixture
def gallery(tmp_path: Path) -> Path:
    """One stale, two missing and one fresh asset, plus one without a package."""
    root = tmp_path / "gallery"
    assets = {
        "a/one.png": "image",
        "b/two.png": "image",
        "c/doc.pdf": "document",
        "d/nopkg.txt": "text",
        "e/fresh.txt": "image",
    }
    for name in assets:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 10)
        os.utime(path, (BASE_MTIME, BASE_MTIME))
    _write_record(root / "a/one.png", "image")
    _preview_file(root / "a/one.png", {"preview": 1, "kind": "image"}, BASE_MTIME - 5)
    _write_record(root / "b/two.png", "image")
    _write_record(root / "c/doc.pdf", "document")
    _write_record(root / "d/nopkg.txt", "text", package=False)
    _write_record(root / "e/fresh.txt", "image")
    _preview_file(root / "e/fresh.txt", {"preview": 1, "kind": "image"}, BASE_MTIME + 5)
    return root


def _write_record(path: Path, kind: str, *, package: bool = True) -> None:
    """A record the gallery reads, carrying empty params and the echo package."""
    provenance: dict[str, object] = {"capability": "echo.text", "params": {}}
    if package:
        provenance["package"] = {"name": "echo", "version": "0.1.0"}
    payload = {
        "record": 1,
        "name": path.stem,
        "path": path.name,
        "kind": kind,
        "mime": "application/octet-stream",
        "bytes": path.stat().st_size,
        "created": "2026-11-12T10:00:00Z",
        "properties": {},
        "provenance": provenance,
        "verification": {"status": "passed"},
    }
    path.parent.joinpath(path.name + ".facet.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _preview_file(asset: Path, payload: dict[str, object], stamp: float) -> None:
    """Write ``<asset>.preview.json`` stamped at ``stamp``."""
    target = asset.parent / (asset.name + ".preview.json")
    target.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(target, (stamp, stamp))


def _site(folder: Path) -> GallerySite:
    """The folder's cards over the fixture registry's viewer roots."""
    viewers = FIXTURES / "viewers"
    return GallerySite(
        gallery_root=viewers / "gallery",
        kit_root=viewers / "_kit",
        folder_name=folder.name,
        assets=collect(folder),
    )


def _cards(port: int) -> list[dict[str, Any]]:
    """The cards ``/gallery.json`` currently serves."""
    status, _headers, body = _get(port, "/gallery.json")
    assert status == HTTP_OK
    data: Any = json.loads(body)
    assert isinstance(data, dict)
    cards: Any = data["assets"]
    assert isinstance(cards, list)
    return cards


def _opens(port: int) -> list[str | None]:
    """The ``open`` route of every card, in order."""
    return [_card_open(card) for card in _cards(port)]


def _card_open(card: dict[str, Any]) -> str | None:
    value = card["open"]
    assert value is None or isinstance(value, str)
    return value


def _ok_calls(monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, object]]) -> None:
    """A runner stub that writes the preview the gallery build expects."""

    def _stub(invocation: object, verb: str, request: dict[str, object]) -> object:
        calls.append({"invocation": invocation, "verb": verb, "request": request})
        raw_assets = request["assets"]
        assert isinstance(raw_assets, list) and raw_assets
        asset = Path(str(raw_assets[0]))
        (asset.parent / (asset.name + ".preview.json")).write_text(
            json.dumps({"kind": "image", "images": ["shot.png"]}), encoding="utf-8"
        )
        (asset.parent / "shot.png").write_bytes(b"shot")
        return runner_module.Outcome({}, "")

    monkeypatch.setattr(runner_module, "call", _stub)


def test_installed_cards_open(facet_home: Path, gallery: Path) -> None:
    """Stale and missing cards of an installed package get an open route."""
    assert facet_home.is_dir()
    with _running(_site(gallery)) as port:
        assert [card["preview_state"] for card in _cards(port)] == [
            "stale",
            "missing",
            "missing",
            "missing",
            "fresh",
        ]
        assert _opens(port) == ["/open/0", "/open/1", "/open/2", None, "/open/4"]


def test_uninstalled_cards_stay_shut(bare_home: Path, gallery: Path) -> None:
    """Without the package only the fresh card opens; the rest have no route."""
    assert bare_home is not None
    with _running(_site(gallery)) as port:
        assert _opens(port) == [None, None, None, None, "/open/4"]


def test_open_builds_then_redirects(
    facet_home: Path, gallery: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/open/0`` builds the stale preview, then behaves like a fresh card."""
    assert facet_home.is_dir()
    calls: list[dict[str, object]] = []
    _ok_calls(monkeypatch, calls)
    with _running(_site(gallery)) as port:
        status, headers, _body = _get(port, "/open/0")
        assert status == HTTP_FOUND
        assert headers["Location"] == "/viewer/index.html?asset=one.png"
        cards = _cards(port)
        assert cards[0]["preview_state"] == "fresh"
        assert cards[0]["open"] == "/open/0"
        assert cards[0]["thumb"] == "/thumb/0"
        thumb_status, _headers, thumb_body = _get(port, "/thumb/0")
        assert thumb_status == HTTP_OK
        assert thumb_body == b"x" * 10
    assert len(calls) == 1
    assert calls[0]["verb"] == "preview"
    invocation = calls[0]["invocation"]
    assert isinstance(invocation, runner_module.Invocation)
    assert invocation.start_server is False


def test_failed_build_is_500_and_keeps_the_card(
    facet_home: Path, gallery: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing build names the asset and the runner error, and changes nothing."""

    def _fail(invocation: object, verb: str, request: dict[str, object]) -> object:
        raise FacetError("runner_failed", "runner preview failed with exit code 3")

    monkeypatch.setattr(runner_module, "call", _fail)
    with _running(_site(gallery)) as port:
        status, headers, body = _get(port, "/open/1")
        assert status == HTTP_SERVER_ERROR
        assert headers["Content-Type"] == "text/plain; charset=utf-8"
        text = body.decode("utf-8")
        assert "b/two.png" in text
        assert "exit code 3" in text
        cards = _cards(port)
        assert cards[1]["preview_state"] == "missing"
        assert cards[1]["open"] == "/open/1"


def test_build_that_cannot_read_the_record_is_500(
    facet_home: Path, gallery: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build that fails before the runner runs still answers 500, card intact."""
    assert facet_home.is_dir()
    calls: list[dict[str, object]] = []
    _ok_calls(monkeypatch, calls)
    with _running(_site(gallery)) as port:
        (gallery / "b/two.png.facet.json").unlink()
        status, headers, body = _get(port, "/open/1")
        assert status == HTTP_SERVER_ERROR
        assert headers["Content-Type"] == "text/plain; charset=utf-8"
        assert "b/two.png" in body.decode("utf-8")
        assert _cards(port)[1]["preview_state"] == "missing"
    assert calls == []


def test_concurrent_opens_serialize(
    facet_home: Path, gallery: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two opens arriving together build one at a time and both redirect."""
    assert facet_home.is_dir()
    guard = threading.Lock()
    state = {"active": 0, "peak": 0, "calls": 0}

    def _slow(invocation: object, verb: str, request: dict[str, object]) -> object:
        with guard:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            state["calls"] += 1
        try:
            time.sleep(0.2)
            raw_assets = request["assets"]
            assert isinstance(raw_assets, list) and raw_assets
            asset = Path(str(raw_assets[0]))
            (asset.parent / (asset.name + ".preview.json")).write_text(
                json.dumps({"kind": "image", "images": ["shot.png"]}), encoding="utf-8"
            )
            (asset.parent / "shot.png").write_bytes(b"shot")
        finally:
            with guard:
                state["active"] -= 1
        return runner_module.Outcome({}, "")

    monkeypatch.setattr(runner_module, "call", _slow)
    with _running(_site(gallery)) as port:
        results: dict[str, int] = {}
        threads = [
            threading.Thread(target=lambda n=n: results.setdefault(n, _get(port, n)[0]))
            for n in ("/open/0", "/open/1")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert results == {"/open/0": HTTP_FOUND, "/open/1": HTTP_FOUND}
    assert state == {"active": 0, "peak": 1, "calls": 2}


def test_open_of_a_document_redirects_to_its_viewer(
    facet_home: Path, gallery: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A built document card redirects to its viewer like every other kind."""
    assert facet_home.is_dir()
    calls: list[dict[str, object]] = []
    _ok_calls(monkeypatch, calls)
    with _running(_site(gallery)) as port:
        status, headers, _body = _get(port, "/open/2")
        viewer, _viewer_headers, viewer_body = _get(port, "/viewer/index.html")
        pdf, pdf_headers, pdf_body = _get(port, "/asset/doc.pdf")
        assert status == HTTP_FOUND
        assert headers["Location"] == "/viewer/index.html?asset=doc.pdf"
        assert viewer == HTTP_OK
        assert b"generic viewer" in viewer_body
        assert pdf == HTTP_OK
        assert pdf_headers["Content-Type"] == "application/pdf"
        assert pdf_body == b"x" * 10
        assert _cards(port)[2]["preview_state"] == "fresh"


def test_folder_json_runs_no_runner(
    capsys: pytest.CaptureFixture[str],
    bare_home: Path,
    gallery: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--json`` classifies the cards and never reaches the runner."""
    assert bare_home is not None

    def _forbidden(invocation: object, verb: str, request: dict[str, object]) -> object:
        raise AssertionError("no runner may run for --json")

    monkeypatch.setattr(runner_module, "call", _forbidden)
    assert main(["preview", str(gallery), "--json"]) == EXIT_OK
    data: Any = json.loads(capsys.readouterr().out)["data"]
    assert [entry["preview_state"] for entry in data["assets"]] == [
        "stale",
        "missing",
        "missing",
        "missing",
        "fresh",
    ]


def test_open_build_enters_trapped_on_a_worker_thread(
    facet_home: Path, gallery: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The build runs ``_trapped`` on the server's worker thread and still redirects.

    The real ``runner.call`` wraps the run in ``_trapped``; before the fix,
    ``signal.signal`` raised ``ValueError`` off the main thread and the card
    answered 500 instead of redirecting.
    """
    assert facet_home.is_dir()

    def _stub(invocation: object, verb: str, request: dict[str, object]) -> object:
        with runner_module._trapped():
            raw_assets = request["assets"]
            assert isinstance(raw_assets, list) and raw_assets
            asset = Path(str(raw_assets[0]))
            (asset.parent / (asset.name + ".preview.json")).write_text(
                json.dumps({"kind": "image", "images": ["shot.png"]}), encoding="utf-8"
            )
            (asset.parent / "shot.png").write_bytes(b"shot")
            return runner_module.Outcome({}, "")

    monkeypatch.setattr(runner_module, "call", _stub)
    with _running(_site(gallery)) as port:
        status, headers, _body = _get(port, "/open/1")
        assert status == HTTP_FOUND
        assert headers["Location"] == "/viewer/index.html?asset=two.png"


def test_unexpected_build_error_is_500(
    facet_home: Path,
    gallery: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-``FacetError`` from the runner answers 500, logged, stderr silent."""
    assert facet_home.is_dir()

    def _boom(invocation: object, verb: str, request: dict[str, object]) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(runner_module, "call", _boom)
    with caplog.at_level(logging.DEBUG, logger="facetmedia"), _running(_site(gallery)) as port:
        status, headers, body = _get(port, "/open/1")
    assert status == HTTP_SERVER_ERROR
    assert headers["Content-Type"] == "text/plain; charset=utf-8"
    assert "open" in body.decode("utf-8")
    assert capsys.readouterr().err == ""
    assert "boom" in caplog.text
    assert any(entry.exc_info is not None for entry in caplog.records)


def test_handle_error_logs_not_stderr(
    gallery: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure escaping the handler is logged to the run log, never stderr."""
    server = make_gallery_server(0, _site(gallery))
    try:
        with caplog.at_level(logging.DEBUG, logger="facetmedia"):
            try:
                raise RuntimeError("boom")
            except RuntimeError:
                server.handle_error("r", ("127.0.0.1", 1))
        assert capsys.readouterr().err == ""
        assert "boom" in caplog.text
        assert any(entry.exc_info is not None for entry in caplog.records)
    finally:
        server.server_close()
