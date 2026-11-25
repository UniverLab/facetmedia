"""Read a secret with masked feedback: one ``*`` per character, never the value.

When stdin and stdout are both terminals, the prompt is printed as-is, the
terminal is put in raw mode, each accepted character shows one ``*``, and
the value itself is never echoed. Otherwise (pipes, redirected output, or a
platform without ``termios``) this falls back to :func:`getpass.getpass`
with no echo, exactly as before.
"""

from __future__ import annotations

import codecs
import getpass
import os
import sys

__all__ = ["read_masked"]


def _is_usable() -> bool:
    """Whether raw-mode masked reading works on this terminal right now."""
    try:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            return False
        import termios  # noqa: PLC0415 - optional terminal module, may be missing

        termios.tcgetattr(sys.stdin.fileno())
    except (OSError, ValueError, AttributeError, ImportError):
        return False
    return True


def _skip_csi(fd: int) -> None:
    """Consume one ``ESC [ ... final`` sequence: arrow keys, paste markers."""
    if os.read(fd, 1) != b"[":
        return
    while True:
        byte = os.read(fd, 1)
        if not byte or b"@" <= byte <= b"~":
            return


def read_masked(prompt: str) -> str:
    """Read one line with echo off, showing one ``*`` per accepted character.

    Backspace erases the last character and one ``*``; Enter ends the read
    and returns the accepted characters unchanged. Ctrl-C raises
    ``KeyboardInterrupt``; Ctrl-D on an empty buffer raises ``EOFError``.
    Escape sequences (including bracketed-paste markers) and other control
    characters are ignored. Falls back to :func:`getpass.getpass` when raw
    mode is unavailable.
    """
    if not _is_usable():
        return getpass.getpass(prompt)
    import termios  # noqa: PLC0415 - optional terminal module, may be missing
    import tty  # noqa: PLC0415 - optional terminal module, may be missing

    fd = sys.stdin.fileno()
    sys.stdout.write(prompt)
    sys.stdout.flush()
    decode = codecs.getincrementaldecoder("utf-8")()
    chars: list[str] = []
    old = termios.tcgetattr(fd)
    tty.setraw(fd, termios.TCSADRAIN)
    try:
        while True:
            byte = os.read(fd, 1)
            if not byte:
                raise EOFError
            if byte in (b"\r", b"\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                return "".join(chars)
            if byte == b"\x03":
                raise KeyboardInterrupt
            if byte == b"\x04":
                if not chars:
                    raise EOFError
                continue
            if byte in (b"\x7f", b"\x08"):
                if chars:
                    chars.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            if byte == b"\x1b":
                _skip_csi(fd)
                continue
            if byte < b"\x20":
                continue
            text = decode.decode(byte)
            if text:
                chars.append(text)
                sys.stdout.write("*")
                sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
