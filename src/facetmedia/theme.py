"""One theme for every styled facet output.

All styling comes from this module and nowhere else: the named styles in
``STYLES`` and the console built by :func:`console`.  The facet brand manual
sets one accent, Señal, that marks what the person should act on (commands,
flags, prompts, the selected row) and what is verified (a check that passed, a
measure in range, an accepted licence), one secondary text colour, Hueso tenue,
for reasons, paths, sizes, times and hints, and otherwise the terminal's own
default foreground: the terminal's background is the person's.

Colours are written as hex and rich downgrades them to the terminal's own
palette; ``accent``, ``banner``, ``error`` and ``warn`` stay ``bold`` so the
CLI's identity remains textual.

Styling is on only when the stream being written is a TTY and ``NO_COLOR`` is
unset; everywhere else (pipes, ``--json``, agents) output is plain text
exactly as before.
"""

from __future__ import annotations

import os
import sys
from typing import Final, TextIO

import questionary
from rich.console import Console
from rich.theme import Theme

#: Señal, the brand's one accent: what to act on and what is verified.
SENAL: Final[str] = "#e8c547"
#: Hueso tenue, the secondary text colour.
HUESO_TENUE: Final[str] = "#9a938a"

STYLES: Final[dict[str, str]] = {
    "accent": f"bold {SENAL}",
    "muted": HUESO_TENUE,
    "ok": SENAL,
    "warn": "bold",
    "error": "bold",
    "banner": "bold",
}


def _isatty(stream: TextIO) -> bool:
    """Whether ``stream`` is a terminal; a closed or odd stream is not."""
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def styled(stream: TextIO | None = None) -> bool:
    """True when facet may style ``stream``: a TTY without ``NO_COLOR``."""
    out = stream if stream is not None else sys.stdout
    return "NO_COLOR" not in os.environ and _isatty(out)


def console(stream: TextIO | None = None) -> Console:
    """A rich console on ``stream``, colourless unless :func:`styled` holds.

    Highlighting is off: numbers and paths print literally, so output stays
    calm and greppable.
    """
    out = stream if stream is not None else sys.stdout
    on = styled(out)
    theme = Theme(dict(STYLES))
    for style in theme.styles.values():
        style._ansi = None
    return Console(
        file=out,
        theme=theme,
        no_color=not on,
        force_terminal=on,
        highlight=False,
    )


def menu_style() -> questionary.Style:
    """The questionary style: Señal on the actionable row, default elsewhere."""
    return questionary.Style(
        [
            ("pointer", f"fg:{SENAL} bold"),
            ("highlighted", f"fg:{SENAL} bold"),
            ("selected", f"fg:{SENAL}"),
            ("qmark", ""),
            ("question", ""),
            ("answer", ""),
            ("accent", f"fg:{SENAL} bold"),
            ("separator", ""),
            ("instruction", ""),
            ("text", ""),
        ]
    )


def render(markup: str, stream: TextIO | None = None) -> str:
    """Render ``markup`` for ``stream`` and return it as a string."""
    target = console(stream)
    with target.capture() as capture:
        target.print(markup, end="", soft_wrap=True)
    return capture.get()
