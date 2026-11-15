"""Señal marks only verified results, and stderr styling never leaks into a pipe.

Every snapshot here drives the real rendering point at a fake truecolor
terminal: a pull summary, a run result and a licence acceptance must carry
the brand accent on the verified word alone, while a failure, an installed
package and a total stay calm.  The pipe cases assert today's bytes exactly,
including the absence of the ``✗ `` and ``! `` prefixes.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Final

import pytest

from facetmedia import cli, contract, license_accept, licenses, plan, pull_summary, run, wizard
from facetmedia.contract import FacetError
from facetmedia.progress import Progress

SOURCE: Final[Path] = Path(__file__).resolve().parent.parent / "src" / "facetmedia"
MIB: Final[int] = 1024**2
SENAL: Final[str] = "\x1b[38;2;232;197;71m"
MUTED: Final[str] = "\x1b[38;2;154;147;138m"
BOLD: Final[str] = "\x1b[1m"
RESET: Final[str] = "\x1b[0m"
COLOUR_WORDS: Final[frozenset[str]] = frozenset(
    {
        "black",
        "red",
        "green",
        "yellow",
        "blue",
        "magenta",
        "cyan",
        "white",
        "grey",
        "gray",
    }
)
HEX_COLOUR: Final[re.Pattern[str]] = re.compile(r"#[0-9a-fA-F]{3,8}\b")
MARKUP_TAG: Final[re.Pattern[str]] = re.compile(r"\[([a-z0-9_./ ]+)\]")
STYLE_VALUE: Final[re.Pattern[str]] = re.compile(r"""style\s*=\s*["']([^"']*)["']""")


class _Tty(io.StringIO):
    """A stream that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


class FakeClock:
    """A monotonic clock that advances one step per call."""

    def __init__(self, step: float = 0.25) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


@dataclass(frozen=True)
class Entry:
    """A plan entry reduced to what the columns read."""

    name: str
    version: str
    model_download_bytes: int = 0


@pytest.fixture(autouse=True)
def _truecolor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A terminal rich resolves to truecolor, so the brand hexes come through."""
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.delenv("NO_COLOR", raising=False)


def _summary(stream: io.StringIO) -> pull_summary.PullSummary:
    summary = pull_summary.PullSummary(Progress("tty", stream, FakeClock()))
    summary.plan([Entry("kokoro", "0.1.2", 812 * MIB)])
    return summary


def _lines(stream: io.StringIO) -> list[str]:
    return stream.getvalue().splitlines()


# --- a pull summary.


def test_a_finished_package_marks_only_its_check() -> None:
    stream = _Tty()
    summary = _summary(stream)

    summary.begin("kokoro", "0.1.2")
    summary.collapse("kokoro", "0.1.2", 812 * MIB, 27.6)

    assert _lines(stream) == [
        f"→ {BOLD}kokoro 0.1.2{RESET}",
        f"{SENAL}✓{RESET} {BOLD}kokoro 0.1.2{RESET}   {MUTED}812 MB   27.6s{RESET}",
    ]


def test_a_failed_package_carries_no_senal() -> None:
    stream = _Tty()
    summary = _summary(stream)
    summary.plan([Entry("kokoro", "0.1.2")])

    summary.failed("kokoro", "0.1.2", 12.3, "HTTP 404 on the model")

    line = _lines(stream)[-1]
    assert line == (
        f"{BOLD}✗{RESET} {BOLD}kokoro 0.1.2{RESET}   "
        f"{MUTED}failed in 12.3s{RESET}: HTTP 404 on the model"
    )
    assert SENAL not in stream.getvalue()


def test_an_installed_package_and_the_total_stay_calm() -> None:
    stream = _Tty()
    summary = _summary(stream)

    summary.noop("kokoro", "0.1.2")
    summary.total(1, 812 * MIB, 27.6)

    text = stream.getvalue()
    assert SENAL not in text
    assert _lines(stream) == [
        f"· {BOLD}kokoro 0.1.2{RESET}   {MUTED}already installed{RESET}",
        f"{MUTED}1 package · 812 MB · 27.6s{RESET}",
    ]


# --- facet run and facet check results.


def test_a_run_result_marks_passed_and_leaves_failed_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _Tty()
    monkeypatch.setattr(sys, "stdout", stream)
    data = {
        "assets": [
            {"path": "/out/shot.mp4", "name": "shot", "verification": {"status": "passed"}},
            {"path": "/out/clip.mp4", "name": "clip", "verification": {"status": "failed"}},
        ],
        "out_dir": "/out",
    }

    cli._announce_run(data)

    assert _lines(stream) == [
        f"/out/shot.mp4 shot {SENAL}passed{RESET}",
        "/out/clip.mp4 clip failed",
        "out /out",
    ]


def test_a_check_reverdict_marks_passed_only(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _Tty()
    monkeypatch.setattr(sys, "stdout", stream)

    cli._announce_reverify({"asset": "/out/shot.mp4", "passed": True})

    assert stream.getvalue() == f"/out/shot.mp4 {SENAL}passed{RESET}\n"


# --- a licence acceptance.


def test_an_accepted_licence_marks_only_the_word_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _Tty()
    monkeypatch.setattr(sys, "stdout", stream)
    outcome = licenses.Acceptance(licenses.ACCEPTED, {"model": "echo-weights"})
    args = argparse.Namespace(json=False)

    assert license_accept._report(outcome, "echo-weights", args) == 0

    assert stream.getvalue() == f"echo-weights {SENAL}accepted{RESET}\n"


# --- stderr: styled at a terminal, byte-identical in a pipe.


def test_a_piped_error_keeps_todays_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)

    contract.emit(contract.failure(FacetError("invalid_input", "bad thing", hint="do this")), False)

    assert stream.getvalue() == "bad thing\ndo this\n"
    assert "\x1b" not in stream.getvalue()
    assert "✗" not in stream.getvalue()


def test_a_styled_error_prefixes_the_message_and_mutes_the_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _Tty()
    monkeypatch.setattr(sys, "stderr", stream)

    contract.emit(contract.failure(FacetError("invalid_input", "bad thing", hint="do this")), False)

    assert stream.getvalue() == f"{BOLD}✗ bad thing{RESET}\n{MUTED}do this{RESET}\n"
    assert SENAL not in stream.getvalue()


def test_a_run_gate_warning_is_marked_only_at_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    piped = io.StringIO()
    monkeypatch.setattr(sys, "stderr", piped)
    run._warn("doctor timed out")

    assert piped.getvalue() == "doctor timed out\n"

    terminal = _Tty()
    monkeypatch.setattr(sys, "stderr", terminal)
    run._warn("doctor timed out")

    assert terminal.getvalue() == f"{BOLD}! doctor timed out{RESET}\n"


def test_a_piped_pull_line_keeps_todays_bytes() -> None:
    stream = io.StringIO()
    summary = pull_summary.PullSummary(Progress("plain", stream))
    summary.plan([Entry("kokoro", "0.1.2", 812 * MIB)])

    summary.begin("kokoro", "0.1.2")
    summary.collapse("kokoro", "0.1.2", 812 * MIB, 27.6)
    summary.total(1, 812 * MIB, 27.6)

    assert _lines(stream) == [
        "→ kokoro 0.1.2",
        "✓ kokoro 0.1.2   812 MB   27.6s",
        "1 package · 812 MB · 27.6s",
    ]
    assert "\x1b" not in stream.getvalue()


# --- one source of colour.


def test_no_colour_is_named_outside_the_theme() -> None:
    offenders: list[str] = []
    for path in sorted(SOURCE.rglob("*.py")):
        if path.name == theme_name():
            continue
        text = path.read_text(encoding="utf-8")
        if HEX_COLOUR.search(text):
            offenders.append(f"{path.name}: names a hex colour")
        for tag in MARKUP_TAG.findall(text):
            words = set(re.split(r"[ _./]+", tag))
            if words & COLOUR_WORDS:
                offenders.append(f"{path.name}: markup tag [{tag}]")
        for value in STYLE_VALUE.findall(text):
            words = set(re.split(r"[ _|/,+]+", value.lower()))
            if words & COLOUR_WORDS:
                offenders.append(f'{path.name}: style="{value}"')

    assert offenders == []


def test_summary_builders_carry_their_marks() -> None:
    widths = pull_summary.Widths()
    collapsed = pull_summary.collapse_line(widths, "kokoro", "0.1.2", "812 MB", 27.6)
    assert "[ok]" in collapsed
    assert "[accent]" in collapsed
    assert "[muted]" in collapsed
    failed = pull_summary.failed_line(widths, "kokoro", "0.1.2", 12.3, "HTTP 404")
    assert "[error]" in failed
    assert "[accent]" in failed
    assert "[ok]" not in failed
    assert "[muted]" in pull_summary.noop_line(widths, "kokoro", "0.1.2")
    assert "[muted]" in pull_summary.total_line(1, 0, 12.5)


def test_the_wizard_marks_each_step(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _Tty()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(wizard, "_pick", lambda *args: ["kokoro"])
    monkeypatch.setattr(wizard, "_machine_text", lambda *args: "a test machine")
    monkeypatch.setattr(plan, "resolve", lambda *args: [])
    monkeypatch.setattr(plan, "build_plan", lambda *args, **kwargs: [])
    monkeypatch.setattr(plan, "disk_refusal", lambda *args: None)

    assert wizard.choose(SimpleNamespace(), SimpleNamespace(), argparse.Namespace()) == ["kokoro"]

    text = stream.getvalue()
    for step in ("Step 1 of 4", "Step 2 of 4", "Step 3 of 4", "Step 4 of 4"):
        assert f"{BOLD}{step}" in text
    assert SENAL not in text


def theme_name() -> str:
    """The one module allowed to name a colour."""
    return "theme.py"
