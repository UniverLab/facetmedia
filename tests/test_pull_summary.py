"""The one-line-per-package summary of ``facet pull``: format, columns, totals.

The sizes follow the spec's rule (MB below 1 GB with no decimals, GB from
there up with one), the columns line up across packages, and every builder
omits what it does not have: a failure carries no size, an installed package
no bytes at all.  A reporter whose mode is ``off`` draws nothing.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import pytest
from rich.text import Text

from facetmedia import pull_summary
from facetmedia.progress import Progress

MIB = 1024**2
GIB = 1024**3
SHORT_NAME = "kokoro"
SHORT_VERSION = "0.1.2"


def plain(markup: str) -> str:
    """The line a pipe receives: the markup stripped, as ``_write_line`` renders it."""
    return Text.from_markup(markup).plain


@dataclass(frozen=True)
class Entry:
    """A plan entry reduced to what the columns read."""

    name: str
    version: str
    model_download_bytes: int = 0


class FakeClock:
    """A monotonic clock that advances one step per call."""

    def __init__(self, step: float = 0.25) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


@pytest.mark.parametrize(
    ("downloaded", "text"),
    [
        (0, "already present"),
        (812 * MIB, "812 MB"),
        (1023 * MIB, "1023 MB"),
        (GIB - 1, "1023 MB"),
        (GIB, "1.0 GB"),
        (int(10.5 * GIB), "10.5 GB"),
    ],
)
def test_size_text_is_mb_below_a_gigabyte_and_gb_from_there_on(downloaded: int, text: str) -> None:
    assert pull_summary.size_text(downloaded) == text


def test_total_size_text_says_zero_mb_for_a_pull_that_downloaded_nothing() -> None:
    assert pull_summary.total_size_text(0) == "0 MB"
    assert pull_summary.total_size_text(int(14.2 * GIB)) == "14.2 GB"


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(27.6, "27.6s"), (59.94, "59.9s"), (60.0, "1m 00s"), (761.0, "12m 41s")],
)
def test_time_text_switches_to_minutes_and_pads_the_seconds(seconds: float, text: str) -> None:
    assert pull_summary.time_text(seconds) == text


def test_the_columns_line_up_across_packages() -> None:
    entries = [
        Entry(SHORT_NAME, SHORT_VERSION, 812 * MIB),
        Entry("musicgen-small", "1.0.0", int(1.5 * GIB)),
    ]

    widths = pull_summary.plan_widths(entries)
    first = plain(pull_summary.collapse_line(widths, SHORT_NAME, SHORT_VERSION, "812 MB", 27.6))
    second = plain(pull_summary.collapse_line(widths, "musicgen-small", "1.0.0", "1.5 GB", 4.0))

    assert widths.name == len("musicgen-small")
    assert widths.version == len(SHORT_VERSION)
    assert widths.size == len("1.5 GB")
    assert first.index("27.6s") == second.index("4.0s"), "the time column starts at one place"
    assert first.startswith("✓ kokoro ")
    assert first.endswith("812 MB   27.6s")
    assert second.startswith("✓ musicgen-small 1.0.0")


def test_a_collapsed_line_reads_name_version_size_and_time() -> None:
    line = pull_summary.collapse_line(
        pull_summary.Widths(), SHORT_NAME, SHORT_VERSION, "812 MB", 27.6
    )

    assert plain(line) == "✓ kokoro 0.1.2   812 MB   27.6s"


def test_the_size_column_ignores_packages_that_download_nothing() -> None:
    widths = pull_summary.plan_widths([Entry("a", "0.1.0"), Entry("b", "0.1.0", 812 * MIB)])

    assert widths.size == len("812 MB"), "'already present' does not widen every line"


def test_the_failure_line_omits_the_size_and_keeps_the_error() -> None:
    line = pull_summary.failed_line(
        pull_summary.Widths(), SHORT_NAME, SHORT_VERSION, 12.3, "HTTP 404 on the model"
    )

    assert plain(line) == "✗ kokoro 0.1.2   failed in 12.3s: HTTP 404 on the model"


def test_the_installed_line_says_already_installed() -> None:
    line = pull_summary.noop_line(pull_summary.Widths(), SHORT_NAME, SHORT_VERSION)

    assert plain(line) == "· kokoro 0.1.2   already installed"


def test_the_total_line_counts_packages_bytes_and_time() -> None:
    assert plain(pull_summary.total_line(16, int(14.2 * GIB), 761.0)) == (
        "16 packages · 14.2 GB · 12m 41s"
    )
    assert plain(pull_summary.total_line(1, 0, 12.5)) == "1 package · 0 MB · 12.5s"


def test_tty_replaces_the_live_phase_row_with_the_package_line() -> None:
    stream = io.StringIO()
    reporter = Progress("tty", stream, FakeClock())
    summary = pull_summary.PullSummary(reporter)
    summary.plan([Entry(SHORT_NAME, SHORT_VERSION, 812 * MIB)])

    summary.begin(SHORT_NAME, SHORT_VERSION)
    reporter.phase_start("models", total=100)
    reporter.advance(100)
    reporter.phase_end(ok=True)
    summary.collapse(SHORT_NAME, SHORT_VERSION, 812 * MIB, 27.6)

    lines = stream.getvalue().splitlines()
    assert "→ kokoro 0.1.2" in lines
    assert "✓ kokoro 0.1.2   812 MB   27.6s" in lines
    assert not any(line.startswith("✓ models") for line in lines)


def test_plain_mode_writes_the_same_lines_once_each_package_finishes() -> None:
    stream = io.StringIO()
    summary = pull_summary.PullSummary(Progress("plain", stream))

    summary.begin(SHORT_NAME, SHORT_VERSION)
    summary.collapse(SHORT_NAME, SHORT_VERSION, 812 * MIB, 27.6)

    assert stream.getvalue().splitlines() == [
        "→ kokoro 0.1.2",
        "✓ kokoro 0.1.2   812 MB   27.6s",
    ]


def test_a_silent_reporter_draws_no_line_at_all() -> None:
    stream = io.StringIO()
    summary = pull_summary.PullSummary(Progress("off", stream))

    summary.begin(SHORT_NAME, SHORT_VERSION)
    summary.collapse(SHORT_NAME, SHORT_VERSION, 812 * MIB, 27.6)
    summary.noop(SHORT_NAME, SHORT_VERSION)
    summary.failed(SHORT_NAME, SHORT_VERSION, 1.0, "boom")
    summary.total(2, 812 * MIB, 27.6)

    assert summary.enabled is False
    assert stream.getvalue() == ""


def test_without_a_reporter_nothing_is_drawn() -> None:
    assert pull_summary.PullSummary().enabled is False
