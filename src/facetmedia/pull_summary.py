"""One line per package: the name, version, size and time of each install.

While a package installs, its phases draw one live line at a terminal.  When
the package finishes that block collapses into a single line that says which
package the check belonged to, so a pull of sixteen packages ends in sixteen
lines that name their package instead of forty-eight nameless phase checks::

    ✓ kokoro 0.1.2   812 MB   27.6s

The size is the bytes this pull downloaded for that package, ``already
present`` when it downloaded nothing; under 1 GB it reads in MB with no
decimals, from 1 GB up in GB with one.  A package that needed nothing reads
``· kokoro 0.1.2   already installed`` and a failure carries its one-line
error.  After the last package comes the total.

Everything here draws nothing when the reporter's mode is ``off``: ``--json``
and ``--verbose`` keep their output byte for byte.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Final, Protocol

from rich.markup import escape

from facetmedia.progress import Progress

MIB: Final[float] = 1024.0 * 1024.0
GIB: Final[float] = 1024.0 * MIB
#: The size column's text when the pull downloaded nothing for a package.
PRESENT: Final[str] = "already present"
#: What separates one column of a summary line from the next.
GAP: Final[str] = "   "
SECONDS_PER_MINUTE: Final[int] = 60


class Planned(Protocol):
    """What the columns need to know about one entry of the plan."""

    @property
    def name(self) -> str:
        """The package's name."""
        ...

    @property
    def version(self) -> str:
        """The package's version."""
        ...

    @property
    def model_download_bytes(self) -> int:
        """The declared bytes it still has to download."""
        ...


def size_text(downloaded: int) -> str:
    """``812 MB`` under 1 GB, ``10.5 GB`` from there up, ``already present`` at zero.

    The MB count floors: a download just under 1 GB reads ``1023 MB``, never
    a ``1024 MB`` that belongs on the other side of the boundary.
    """
    if downloaded <= 0:
        return PRESENT
    if downloaded < GIB:
        return f"{downloaded // int(MIB)} MB"
    return f"{downloaded / GIB:.1f} GB"


def total_size_text(downloaded: int) -> str:
    """The total's size: the same rule, with ``0 MB`` for a pull that fetched nothing."""
    return size_text(downloaded) if downloaded > 0 else "0 MB"


def time_text(seconds: float) -> str:
    """``27.6s`` under a minute, ``12m 41s`` from there up."""
    if seconds < SECONDS_PER_MINUTE:
        return f"{seconds:.1f}s"
    whole = int(seconds)
    return f"{whole // SECONDS_PER_MINUTE}m {whole % SECONDS_PER_MINUTE:02d}s"


@dataclass(frozen=True)
class Widths:
    """The column widths shared by every line; zero leaves the column at its own width."""

    name: int = 0
    version: int = 0
    size: int = 0


def plan_widths(entries: Iterable[Planned]) -> Widths:
    """The widest name, version and declared size, so the columns line up."""
    items = list(entries)
    return Widths(
        name=max((len(item.name) for item in items), default=0),
        version=max((len(item.version) for item in items), default=0),
        size=max(
            (
                len(size_text(item.model_download_bytes))
                for item in items
                if item.model_download_bytes
            ),
            default=0,
        ),
    )


def _muted(text: str) -> str:
    """Secondary text: a size, a time, a reason."""
    return f"[muted]{escape(text)}[/]"


def _head(widths: Widths, mark: str, name: str, version: str) -> str:
    """``✓ kokoro 0.1.2`` with both columns padded, then the gap before the size.

    The mark carries the outcome's style; the name and version are the package
    the line belongs to.  Every builder returns markup: ``_write_line`` renders
    it once, so a pipe receives the same bytes with the tags stripped.
    """
    labelled = escape(f"{name.ljust(widths.name)} {version.ljust(widths.version)}")
    return f"{mark} [accent]{labelled}[/]{GAP}"


def collapse_line(widths: Widths, name: str, version: str, size: str, seconds: float) -> str:
    """The line a finished package leaves behind: ``✓ kokoro 0.1.2   812 MB   27.6s``."""
    head = _head(widths, "[ok]✓[/]", name, version)
    tail = _muted(f"{size.ljust(widths.size)}{GAP}{time_text(seconds)}")
    return f"{head}{tail}".rstrip()


def noop_line(widths: Widths, name: str, version: str) -> str:
    """A package the plan found installed: ``· kokoro 0.1.2   already installed``."""
    return f"{_head(widths, '·', name, version)}{_muted('already installed')}".rstrip()


def failed_line(widths: Widths, name: str, version: str, seconds: float, err: str) -> str:
    """A package that broke: ``✗ kokoro 0.1.2   failed in 12.3s: <one line>``."""
    head = _head(widths, "[error]✗[/]", name, version)
    return f"{head}{_muted(f'failed in {time_text(seconds)}')}: {escape(err)}".rstrip()


def total_line(count: int, downloaded: int, seconds: float) -> str:
    """The pull's closing line: ``16 packages · 14.2 GB · 12m 41s``."""
    noun = "package" if count == 1 else "packages"
    return _muted(f"{count} {noun} · {total_size_text(downloaded)} · {time_text(seconds)}")


class PullSummary:
    """Draws the per-package lines and the total for one pull.

    ``reporter`` is the pull's :class:`~facetmedia.progress.Progress`: its
    mode decides whether anything is drawn, and its stream receives the lines.
    """

    def __init__(self, reporter: Progress | None = None) -> None:
        self._reporter = reporter
        self._widths = Widths()

    @property
    def enabled(self) -> bool:
        """Whether this reporter draws lines at all."""
        return self._reporter is not None and self._reporter.mode != "off"

    def plan(self, entries: Iterable[Planned]) -> None:
        """Fix the columns from the whole plan, before the first package starts."""
        if self.enabled:
            self._widths = plan_widths(entries)

    def begin(self, name: str, version: str) -> None:
        """The header the live phase line draws under."""
        if self.enabled:
            self._note(f"→ [accent]{escape(f'{name} {version}')}[/]")

    def collapse(self, name: str, version: str, downloaded: int, seconds: float) -> None:
        """Replace the finished package's block with its one line."""
        if not self.enabled:
            return
        size = size_text(downloaded)
        self._widths = replace(self._widths, size=max(self._widths.size, len(size)))
        self._note(collapse_line(self._widths, name, version, size, seconds))

    def noop(self, name: str, version: str) -> None:
        """One line for a package the plan found already installed."""
        if self.enabled:
            self._note(noop_line(self._widths, name, version))

    def failed(self, name: str, version: str, seconds: float, err: str) -> None:
        """One line for a package that broke, with its one-line error."""
        if self.enabled:
            self._note(failed_line(self._widths, name, version, seconds, err))

    def total(self, count: int, downloaded: int, seconds: float) -> None:
        """The closing line: every planned package, the bytes and the time."""
        if self.enabled:
            self._note(total_line(count, downloaded, seconds))

    def _note(self, text: str) -> None:
        assert self._reporter is not None
        self._reporter.note(text)
