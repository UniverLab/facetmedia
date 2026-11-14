"""Progress lines for ``facet pull`` and ``facet update``, styled from the one theme.

``pull`` drives one line per phase through a :class:`Progress`: a spinner while
the phase has no known size and a bar when it does.  At a terminal that line is
live and transient: ``note`` erases it to write the package's final line, the
one that names the package with its version, size and time.  The detailed
working log (the plan, uv's resolution, the paths) stays in the run log and is
echoed to stderr only with ``--verbose``.

Three modes: ``tty`` redraws one carriage-return line, ``plain`` prints one
line per phase start and end for a pipe or a CI log, and ``off`` draws nothing
(a ``facet run`` pull, or ``--json``).

Only the finished lines (``_write_line``) go through the theme; the live
redraws stay raw text because :meth:`Progress._erase` measures them with
``len``, and an escape sequence would break that arithmetic.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import Final, Literal, TextIO

from rich.markup import escape

from facetmedia import theme
from facetmedia.contract import FacetError
from facetmedia.home import log_file

Mode = Literal["tty", "plain", "off"]

SPINNER: Final[str] = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BAR_WIDTH: Final[int] = 20
#: How often a tty line redraws while bytes stream in.
REFRESH_S: Final[float] = 0.05
MIB: Final[float] = 1024.0 * 1024.0
#: How many log lines a failed pull repeats on stderr.
TAIL_LINES: Final[int] = 20


class _OutputState:
    """Process-wide switch: the detailed working log is loud by default."""

    verbose: bool = True


_STATE = _OutputState()


def set_verbose(enabled: bool) -> None:
    """Whether the detailed working log is echoed to stderr."""
    _STATE.verbose = enabled


def verbose() -> bool:
    """Whether stderr echoes the detailed working log right now."""
    return _STATE.verbose


def _isatty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def _mib(size: float) -> str:
    return f"{size / MIB:.1f}"


class Progress:
    """One phase line; ``phase_start``, ``advance`` and ``phase_end`` drive it."""

    def __init__(
        self,
        mode: Mode,
        stream: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._mode = mode
        self._stream = stream
        self._clock = clock
        self._name = ""
        self._total: int | None = None
        self._done = 0
        self._started = 0.0
        self._last = 0.0
        self._frame = 0
        self._width = 0

    @classmethod
    def off(cls) -> Progress:
        """A reporter that draws nothing."""
        return cls("off")

    @classmethod
    def for_cli(cls, *, as_json: bool, verbose: bool, stream: TextIO | None = None) -> Progress:
        """The mode a pull verb uses: no line for ``--json`` or ``--verbose``."""
        if as_json or verbose:
            return cls("off", stream)
        out = stream if stream is not None else sys.stderr
        return cls("tty" if _isatty(out) else "plain", out)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def _out(self) -> TextIO:
        return self._stream if self._stream is not None else sys.stderr

    def phase_start(self, name: str, total: int | None = None) -> None:
        """Begin ``name``; ``total`` bytes make it a bar, ``None`` a spinner."""
        self._name = name
        self._total = total
        self._done = 0
        self._frame = 0
        self._started = self._clock()
        self._last = self._started
        if self._mode == "plain":
            self._write_line(f"start {name}")
        elif self._mode == "tty":
            self._draw()

    def phase_total(self, total: int) -> None:
        """Learn a phase's size while it runs; artifacts only discover it late."""
        if total <= 0:
            return
        self._total = (self._total or 0) + total
        if self._mode == "tty":
            self._draw()

    def advance(self, count: int) -> None:
        """Add ``count`` bytes to the phase; a pipe prints nothing per byte."""
        if self._mode == "off":
            return
        self._done += max(0, count)
        if self._mode != "tty":
            return
        now = self._clock()
        if now - self._last < REFRESH_S:
            return
        self._last = now
        self._draw()

    def phase_end(self, ok: bool = True, err: str = "") -> None:
        """Close the live line: a pipe gets ``end ✓ name (Xs)``, a tty just erases it.

        At a terminal the phase line is replaced by the package's summary line
        (see ``note``), so it must not land in the transcript of its own:
        else a sixteen-package pull ends in a column of nameless checks.
        """
        if self._mode == "off":
            return
        elapsed = self._clock() - self._started
        text = (
            f"[ok]✓[/] {escape(self._name)} [muted]({elapsed:.1f}s)[/]"
            if ok
            else f"[error]✗[/] {escape(self._name)}: {escape(err)}"
        )
        if self._mode == "plain":
            self._write_line(f"end {text}")
            return
        self._erase()

    def note(self, text: str) -> None:
        """Write one finished line, replacing the live one at a terminal."""
        if self._mode == "off":
            return
        if self._mode == "tty":
            self._erase()
        self._write_line(text)

    def _draw(self) -> None:
        elapsed = self._clock() - self._started
        text = self._bar_text(elapsed) if self._total else self._spinner_text(elapsed)
        self._erase()
        self._out.write(text)
        self._out.flush()
        self._width = len(text)

    def _bar_text(self, elapsed: float) -> str:
        total = self._total or 1
        percent = min(100.0, self._done / total * 100.0)
        filled = int(BAR_WIDTH * percent / 100.0)
        bar = "█" * filled + "░" * (BAR_WIDTH - filled)
        speed = self._done / elapsed if elapsed > 0 else 0.0
        return (
            f"{self._name} [{bar}] {percent:3.0f}% "
            f"{_mib(self._done)}/{_mib(total)} MB {_mib(speed)} MB/s"
        )

    def _spinner_text(self, elapsed: float) -> str:
        frame = SPINNER[self._frame % len(SPINNER)]
        self._frame += 1
        return f"{frame} {self._name} {elapsed:.1f}s {_mib(self._done)} MB"

    def _erase(self) -> None:
        if self._width:
            self._out.write("\r" + " " * self._width + "\r")
            self._width = 0

    def _write_line(self, text: str) -> None:
        """Write one finished line: markup through the theme, plain when piped."""
        theme.console(self._out).print(text, soft_wrap=True)
        self._out.flush()


def one_line(exc: BaseException) -> str:
    """The first line of a failure, for the ``✗`` progress and summary lines."""
    text = exc.message if isinstance(exc, FacetError) else str(exc)
    return text.splitlines()[0] if text else type(exc).__name__


def announce_failure(run_id: str) -> None:
    """Repeat the last log lines and the log path after a failed verb."""
    path = log_file(run_id)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    for line in text.splitlines()[-TAIL_LINES:]:
        print(line, file=sys.stderr)
    print(f"log: {path}", file=sys.stderr)
