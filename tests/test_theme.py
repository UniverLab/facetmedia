"""The one theme: Señal marks verified results, everything else stays calm.

The styles are the brand manual's, the same colours the manual sets; styling
is decided per stream, so stdout and stderr each answer for themselves; and a
piped stream receives plain bytes with the markup stripped.
"""

from __future__ import annotations

import io
import sys

import pytest

from facetmedia import theme

SENAL = "#e8c547"
HUESO_TENUE = "#9a938a"
EXPECTED_STYLES = {
    "accent": "bold",
    "banner": "bold",
    "error": "bold",
    "muted": HUESO_TENUE,
    "ok": SENAL,
    "warn": "bold",
}
SENAL_FOREGROUND = "\x1b[38;2;232;197;71m"
MUTED_FOREGROUND = "\x1b[38;2;154;147;138m"
RESET = "\x1b[0m"


class _TtyProxy:
    """Wrap a stream so ``isatty()`` reports what the test needs."""

    def __init__(self, wrapped: object, tty: bool) -> None:
        self._wrapped = wrapped
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def __getattr__(self, name: str) -> object:
        return getattr(self._wrapped, name)


class _TtyBuffer(io.StringIO):
    """A stream that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


def fake_stream(monkeypatch: pytest.MonkeyPatch, name: str, tty: bool) -> None:
    """Report a TTY or a pipe for ``stdout`` or ``stderr``."""
    monkeypatch.setattr(sys, name, _TtyProxy(getattr(sys, name), tty))


@pytest.fixture(autouse=True)
def _no_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with ``NO_COLOR`` unset."""
    monkeypatch.delenv("NO_COLOR", raising=False)


def test_styles_are_the_brand_manual_ones() -> None:
    assert theme.STYLES == EXPECTED_STYLES


def test_the_brand_hexes_are_the_manual_values() -> None:
    assert theme.SENAL == SENAL
    assert theme.HUESO_TENUE == HUESO_TENUE


def test_styling_needs_a_tty_without_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_stream(monkeypatch, "stdout", True)
    assert theme.styled() is True


def test_no_color_turns_styling_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    fake_stream(monkeypatch, "stdout", True)
    assert theme.styled() is False


def test_a_pipe_turns_styling_off(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_stream(monkeypatch, "stdout", False)
    assert theme.styled() is False


def test_styled_answers_for_each_stream_on_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_stream(monkeypatch, "stdout", True)
    fake_stream(monkeypatch, "stderr", False)

    assert theme.styled() is True, "the default is standard output"
    assert theme.styled(sys.stdout) is True
    assert theme.styled(sys.stderr) is False

    fake_stream(monkeypatch, "stdout", False)
    fake_stream(monkeypatch, "stderr", True)
    assert theme.styled(sys.stdout) is False
    assert theme.styled(sys.stderr) is True


def test_no_color_silences_every_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    fake_stream(monkeypatch, "stdout", True)
    fake_stream(monkeypatch, "stderr", True)
    assert theme.styled(sys.stdout) is False
    assert theme.styled(sys.stderr) is False


def test_console_has_no_color_unless_styled(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_stream(monkeypatch, "stdout", True)
    assert theme.console().no_color is False
    fake_stream(monkeypatch, "stdout", False)
    assert theme.console().no_color is True


def test_console_is_built_for_the_stream_it_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_stream(monkeypatch, "stdout", False)
    assert theme.console().no_color is True
    assert theme.console(_TtyBuffer()).no_color is False


def test_senal_renders_as_the_brand_hex_on_a_truecolor_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLORTERM", "truecolor")
    stream = _TtyBuffer()

    theme.console(stream).print("[ok]passed[/]")

    assert stream.getvalue() == f"{SENAL_FOREGROUND}passed{RESET}\n"


def test_hueso_tenue_renders_as_the_brand_hex_on_a_truecolor_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLORTERM", "truecolor")
    stream = _TtyBuffer()

    theme.console(stream).print("[muted]27.6s[/]")

    assert stream.getvalue() == f"{MUTED_FOREGROUND}27.6s{RESET}\n"


def test_console_writes_plain_text_without_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    fake_stream(monkeypatch, "stdout", False)
    assert theme.styled() is False
    theme.console().print("plain 12.0 GB text")
    assert capsys.readouterr().out == "plain 12.0 GB text\n"


def test_console_strips_the_markup_without_a_tty() -> None:
    stream = io.StringIO()

    theme.console(stream).print("[ok]✓[/] kokoro [muted]812 MB[/]")

    assert stream.getvalue() == "✓ kokoro 812 MB\n"
