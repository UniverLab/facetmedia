"""The facetMedia banner, and the terminal width it has to fit.

``text(width)`` returns the DOS Rebel word while its widest line fits
``width``, and the narrower TheDraw font ansiltr4 otherwise: there is no
third option.  ``print_text()`` writes that banner to stdout, stripping ANSI
colour codes unless stdout is a TTY and ``NO_COLOR`` is unset.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from functools import cache
from importlib import resources

DOS_REBEL = "banner-dos-rebel.txt"
ANSILTR4 = "banner-ansiltr4.txt"
SGR = re.compile(r"\x1b\[[0-9;]*m")


@cache
def _load(name: str) -> str:
    """One banner resource, read once from the package."""
    return (resources.files("facetmedia") / name).read_text(encoding="utf-8")


@cache
def _dos_rebel_width() -> int:
    """The columns DOS Rebel needs: its widest line."""
    return max(len(line) for line in _load(DOS_REBEL).splitlines())


def text(width: int) -> str:
    """The banner for a terminal ``width`` columns wide."""
    if width >= _dos_rebel_width():
        return _load(DOS_REBEL)
    return _load(ANSILTR4)


def _strip_sgr(value: str) -> str:
    """Drop ANSI colour codes."""
    return SGR.sub("", value)


def print_text() -> None:
    """Write the banner for the current terminal to stdout."""
    banner = text(shutil.get_terminal_size().columns)
    if "NO_COLOR" in os.environ or not sys.stdout.isatty():
        banner = _strip_sgr(banner)
    sys.stdout.write(banner)
