"""The facetMedia banner: DOS Rebel while it fits, ansiltr4 below that.

The resources are committed, so these tests read them as they ship.  The
ansiltr4 font is a monochrome TheDraw Block font and carries no colour codes;
the printer's SGR handling is exercised with an ANSI banner injected in place
of the resource.
"""

from __future__ import annotations

import hashlib
import os
import sys
from importlib import resources

import pytest

from facetmedia import banner


def _resource(name: str) -> bytes:
    return (resources.files("facetmedia") / name).read_bytes()


DOS_REBEL_BYTES = _resource("banner-dos-rebel.txt")
ANSILTR4_BYTES = _resource("banner-ansiltr4.txt")
DOS_REBEL = DOS_REBEL_BYTES.decode("utf-8")
ANSILTR4 = ANSILTR4_BYTES.decode("utf-8")
DOS_REBEL_SHA256 = "d3d877a0c6a8eca03379b8879151d56607e9158201e0ffabf77cd27d22afa22b"
ANSILTR4_SHA256 = "6f49c0f03256108a8ec550f1cb2c54f541ff2af12a35000b00acdce756757c89"
COLOURED = "\x1b[97;40mX\x1b[0m"
DOS_HEIGHT = 8
DOS_WIDTH = 100
ANSILTR4_HEIGHT = 4
ANSILTR4_WIDTH = 64


def test_resources_are_the_pinned_facetmedia_banners() -> None:
    assert hashlib.sha256(DOS_REBEL_BYTES).hexdigest() == DOS_REBEL_SHA256
    assert hashlib.sha256(ANSILTR4_BYTES).hexdigest() == ANSILTR4_SHA256


def _widest(text: str) -> int:
    return max(len(line) for line in text.splitlines())


def test_text_switches_at_the_dos_rebel_width() -> None:
    width = _widest(DOS_REBEL)
    assert banner.text(width) == DOS_REBEL
    assert banner.text(width - 1) == ANSILTR4


def test_text_is_always_one_of_the_two_resources() -> None:
    for width in (0, 20, 63, 64, 65, 99, 100, 101, 200):
        assert banner.text(width) in (DOS_REBEL, ANSILTR4)


def test_dos_rebel_is_one_line_as_wide_as_its_figure() -> None:
    rows = DOS_REBEL.splitlines()
    assert len(rows) == DOS_HEIGHT
    assert all(row.strip() for row in rows)
    assert _widest(DOS_REBEL) == DOS_WIDTH


def test_ansiltr4_is_one_narrower_line_at_its_figure_height() -> None:
    rows = ANSILTR4.splitlines()
    assert len(rows) == ANSILTR4_HEIGHT
    assert all(row.strip() for row in rows)
    assert _widest(ANSILTR4) == ANSILTR4_WIDTH
    assert _widest(ANSILTR4) < _widest(DOS_REBEL)


def test_neither_resource_carries_sgr_codes() -> None:
    assert "\x1b[" not in DOS_REBEL
    assert "\x1b[" not in ANSILTR4


def _printed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tty: bool, no_color: bool
) -> str:
    monkeypatch.setattr(banner, "text", lambda _width: COLOURED)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: tty)
    if no_color:
        monkeypatch.setenv("NO_COLOR", "")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)
    banner.print_text()
    return capsys.readouterr().out


def test_print_text_keeps_colours_on_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _printed(monkeypatch, capsys, tty=True, no_color=False) == COLOURED


def test_print_text_strips_colours_without_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _printed(monkeypatch, capsys, tty=False, no_color=False) == "X"


def test_print_text_strips_colours_under_no_color(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _printed(monkeypatch, capsys, tty=True, no_color=True) == "X"


def test_print_text_writes_the_banner_for_the_terminal_width(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(banner.shutil, "get_terminal_size", lambda: os.terminal_size((200, 24)))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    banner.print_text()
    assert capsys.readouterr().out == banner.text(200)


def test_print_text_uses_the_narrow_banner_for_a_narrow_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(banner.shutil, "get_terminal_size", lambda: os.terminal_size((60, 24)))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    banner.print_text()
    assert capsys.readouterr().out == banner.text(60)
