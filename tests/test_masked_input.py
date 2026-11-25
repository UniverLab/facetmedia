"""``masked_input.read_masked`` against a fake terminal: pipe input, patched raw mode.

``termios.tcgetattr``/``tcsetattr`` and ``tty.setraw`` are stubbed so the
read loop runs without a real terminal; ``os.read`` consumes bytes from a
pipe whose read end the fake stdin exposes. The value channel stays exact
while the screen only ever holds ``*``.
"""

from __future__ import annotations

import getpass
import io
import os
import sys
import termios
import tty
from types import SimpleNamespace

import pytest

from facetmedia import masked_input

PROMPT = "a token: "


class _TtyOut:
    """A stdout that claims to be a terminal and records everything written."""

    def __init__(self) -> None:
        self.text = ""

    def isatty(self) -> bool:
        return True

    def write(self, data: str) -> int:
        self.text += data
        return len(data)

    def flush(self) -> None:
        pass


class _PipeIn:
    """A stdin fed by a pipe: a real fd for ``os.read``, ``isatty`` True."""

    def __init__(self, payload: bytes) -> None:
        self._fd, write_fd = os.pipe()
        os.write(write_fd, payload)
        os.close(write_fd)

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._fd

    def close(self) -> None:
        os.close(self._fd)


@pytest.fixture
def fake_terminal(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Stub raw mode so the loop runs against the pipe; record the restore."""
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: ["old"])
    restored: list[object] = []
    monkeypatch.setattr(termios, "tcsetattr", lambda fd, when, old: restored.append(old))
    raw_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(tty, "setraw", lambda fd, when: raw_calls.append((fd, when)))
    return SimpleNamespace(restored=restored, raw_calls=raw_calls)


def drive(
    monkeypatch: pytest.MonkeyPatch, payload: bytes, prompt: str = PROMPT
) -> tuple[str | BaseException, str]:
    """Feed ``payload`` through a pipe; return the value (or error) and screen."""
    stdin = _PipeIn(payload)
    stdout = _TtyOut()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    try:
        try:
            return masked_input.read_masked(prompt), stdout.text
        except (KeyboardInterrupt, EOFError) as exc:
            return exc, stdout.text
    finally:
        stdin.close()


def test_characters_become_asterisks(
    monkeypatch: pytest.MonkeyPatch, fake_terminal: SimpleNamespace
) -> None:
    value, screen = drive(monkeypatch, b"ab\r")
    assert len(fake_terminal.raw_calls) == 1
    assert value == "ab"
    assert screen == PROMPT + "**\n"


def test_backspace_removes_one_character(
    monkeypatch: pytest.MonkeyPatch, fake_terminal: SimpleNamespace
) -> None:
    value, screen = drive(monkeypatch, b"ab\x7fc\r")
    assert len(fake_terminal.raw_calls) == 1
    assert value == "ac"
    assert screen == PROMPT + "**\b \b*\n"


def test_ctrl_h_backspace_removes_one_character(
    monkeypatch: pytest.MonkeyPatch, fake_terminal: SimpleNamespace
) -> None:
    value, screen = drive(monkeypatch, b"ab\x08c\r")
    assert len(fake_terminal.raw_calls) == 1
    assert value == "ac"
    assert screen == PROMPT + "**\b \b*\n"


def test_backspace_on_empty_buffer_is_ignored(
    monkeypatch: pytest.MonkeyPatch, fake_terminal: SimpleNamespace
) -> None:
    value, screen = drive(monkeypatch, b"\x7fa\r")
    assert len(fake_terminal.raw_calls) == 1
    assert value == "a"
    assert screen == PROMPT + "*\n"


def test_sixty_character_paste_shows_sixty_asterisks(
    monkeypatch: pytest.MonkeyPatch, fake_terminal: SimpleNamespace
) -> None:
    pasted = "x" * 60
    value, screen = drive(monkeypatch, pasted.encode() + b"\r")
    assert len(fake_terminal.raw_calls) == 1
    assert value == pasted
    assert screen == PROMPT + "*" * 60 + "\n"


def test_bracketed_paste_markers_are_ignored(
    monkeypatch: pytest.MonkeyPatch, fake_terminal: SimpleNamespace
) -> None:
    value, screen = drive(monkeypatch, b"\x1b[200~secret\x1b[201~\r")
    assert len(fake_terminal.raw_calls) == 1
    assert value == "secret"
    assert screen == PROMPT + "******\n"


def test_arrow_key_escape_is_ignored(
    monkeypatch: pytest.MonkeyPatch, fake_terminal: SimpleNamespace
) -> None:
    value, screen = drive(monkeypatch, b"\x1b[Asecret\r")
    assert len(fake_terminal.raw_calls) == 1
    assert value == "secret"
    assert screen == PROMPT + "******\n"


def test_other_control_characters_are_ignored(
    monkeypatch: pytest.MonkeyPatch, fake_terminal: SimpleNamespace
) -> None:
    value, screen = drive(monkeypatch, b"a\x01\x02b\r")
    assert len(fake_terminal.raw_calls) == 1
    assert value == "ab"
    assert screen == PROMPT + "**\n"


def test_ctrl_c_raises_and_restores_the_terminal(
    monkeypatch: pytest.MonkeyPatch, fake_terminal: SimpleNamespace
) -> None:
    value, screen = drive(monkeypatch, b"ab\x03")
    assert len(fake_terminal.raw_calls) == 1
    assert isinstance(value, KeyboardInterrupt)
    assert screen == PROMPT + "**"
    assert fake_terminal.restored == [["old"]]


def test_ctrl_d_on_empty_buffer_raises_eof(
    monkeypatch: pytest.MonkeyPatch, fake_terminal: SimpleNamespace
) -> None:
    value, screen = drive(monkeypatch, b"\x04")
    assert len(fake_terminal.raw_calls) == 1
    assert isinstance(value, EOFError)
    assert screen == PROMPT


def test_ctrl_d_after_characters_is_ignored(
    monkeypatch: pytest.MonkeyPatch, fake_terminal: SimpleNamespace
) -> None:
    value, screen = drive(monkeypatch, b"ab\x04\r")
    assert len(fake_terminal.raw_calls) == 1
    assert value == "ab"
    assert screen == PROMPT + "**\n"


def test_success_restores_the_terminal(
    monkeypatch: pytest.MonkeyPatch, fake_terminal: SimpleNamespace
) -> None:
    value, _ = drive(monkeypatch, b"ok\r")
    assert len(fake_terminal.raw_calls) == 1
    assert value == "ok"
    assert fake_terminal.restored == [["old"]]


def test_value_never_reaches_the_screen(
    monkeypatch: pytest.MonkeyPatch, fake_terminal: SimpleNamespace
) -> None:
    value, screen = drive(monkeypatch, b"s3cr3t!\r", prompt="secret: ")
    assert len(fake_terminal.raw_calls) == 1
    assert value == "s3cr3t!"
    assert "s3cr3t!" not in screen
    assert screen == "secret: " + "*******\n"


def test_non_terminal_falls_back_to_getpass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    seen: list[str] = []
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": seen.append(str(prompt)) or "v")
    assert masked_input.read_masked(PROMPT) == "v"
    assert seen == [PROMPT]


def test_terminal_without_termios_falls_back_to_getpass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoFileno(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", NoFileno(""))
    monkeypatch.setattr(sys, "stdout", _TtyOut())
    seen: list[str] = []
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": seen.append(str(prompt)) or "v")
    assert masked_input.read_masked(PROMPT) == "v"
    assert seen == [PROMPT]


def test_prompt_is_printed_as_is(
    monkeypatch: pytest.MonkeyPatch, fake_terminal: SimpleNamespace
) -> None:
    styled = "\x1b[36ma token:\x1b[0m "
    value, screen = drive(monkeypatch, b"v\r", prompt=styled)
    assert len(fake_terminal.raw_calls) == 1
    assert value == "v"
    assert screen == styled + "*\n"


def test_returned_value_is_exact(
    monkeypatch: pytest.MonkeyPatch, fake_terminal: SimpleNamespace
) -> None:
    value, _ = drive(monkeypatch, b"  spaced  \r")
    assert len(fake_terminal.raw_calls) == 1
    assert value == "  spaced  "


def test_newline_also_ends_the_read(
    monkeypatch: pytest.MonkeyPatch, fake_terminal: SimpleNamespace
) -> None:
    value, screen = drive(monkeypatch, b"ab\n")
    assert len(fake_terminal.raw_calls) == 1
    assert value == "ab"
    assert screen == PROMPT + "**\n"
