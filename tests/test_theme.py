"""The one theme: styling only on a styled TTY, nowhere else."""

from __future__ import annotations

import io
import sys

import pytest

from facetmedia import theme

ALLOWED_STYLES = {"bold", "dim", "green", "yellow", "red", "default"}


class _TtyProxy:
    """Wrap a stream so ``isatty()`` reports what the test needs."""

    def __init__(self, wrapped: object, tty: bool) -> None:
        self._wrapped = wrapped
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def __getattr__(self, name: str) -> object:
        return getattr(self._wrapped, name)


def fake_stdout(monkeypatch: pytest.MonkeyPatch, tty: bool) -> None:
    """Report a TTY or a pipe for standard output."""
    monkeypatch.setattr(sys, "stdout", _TtyProxy(sys.stdout, tty))


def test_styling_needs_a_tty_without_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    fake_stdout(monkeypatch, True)
    assert theme.styled() is True


def test_no_color_turns_styling_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    fake_stdout(monkeypatch, True)
    assert theme.styled() is False


def test_a_pipe_turns_styling_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    fake_stdout(monkeypatch, False)
    assert theme.styled() is False


def test_console_has_no_color_unless_styled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    fake_stdout(monkeypatch, True)
    assert theme.console().no_color is False
    fake_stdout(monkeypatch, False)
    assert theme.console().no_color is True


def test_styles_are_calm_and_cover_the_named_roles() -> None:
    assert set(theme.STYLES) == {"accent", "muted", "ok", "warn", "error", "banner"}
    assert set(theme.STYLES.values()) <= ALLOWED_STYLES


def test_console_writes_plain_text_without_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    fake_stdout(monkeypatch, False)
    assert theme.styled() is False
    theme.console().print("plain 12.0 GB text")
    assert capsys.readouterr().out == "plain 12.0 GB text\n"
