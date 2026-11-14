"""One theme for every styled facet output.

All styling comes from this module and nowhere else: the named styles in
``STYLES`` and the console built by :func:`console`.  Until the facet brand
manual exists the styles use only the terminal's default foreground plus
``bold`` and ``dim``, and the standard green, yellow and red for ok, warn
and error.  A later spec sets the brand colours here.

Styling is on only when standard output is a TTY and ``NO_COLOR`` is unset;
everywhere else (pipes, ``--json``, agents) output is plain text exactly as
before.
"""

from __future__ import annotations

import os
import sys
from typing import Final

from rich.console import Console
from rich.theme import Theme

STYLES: Final[dict[str, str]] = {
    "accent": "bold",
    "muted": "dim",
    "ok": "green",
    "warn": "yellow",
    "error": "red",
    "banner": "bold",
}


def styled() -> bool:
    """True when facet may style its output: a TTY without ``NO_COLOR``."""
    return "NO_COLOR" not in os.environ and sys.stdout.isatty()


def console() -> Console:
    """A rich console on stdout, colourless unless :func:`styled` holds.

    Highlighting is off: numbers and paths print literally, so output stays
    calm and greppable.
    """
    return Console(theme=Theme(dict(STYLES)), no_color=not styled(), highlight=False)
