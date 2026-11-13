"""Regenerate the two facetMedia banner resources.

Both are generated once, offline, and committed.  Nothing here is a runtime
dependency of facet.

DOS Rebel
    pyfiglet's ``dos_rebel`` font, rendered at a raised width so the word
    stays on one line (pyfiglet's default width 80 wraps it)::

        uv run --with pyfiglet python scripts/make_banner.py

ansiltr4
    The TheDraw font ``unused-fonts/ansiltr4.tdf`` from tat3r/tdfiglet at
    commit 0225b8881d06773b92d9db2c7881f03ff0bdece7::

        https://github.com/tat3r/tdfiglet

    The repository declares no licence.

    That file is a TheDraw *Block* font: header byte 41 is 0x01 (the TDF
    format defines 00 outline, 01 block, 02 colour).  tdfiglet at that
    commit loads only *Color* fonts (type 0x02) and reads two bytes per
    cell, so it rejects this font as "Invalid font file".  A Block font
    stores one byte per cell and carries no colour data at all.  The script
    therefore reads the font as the TDF format describes, and prints each
    cell as its CP437 character with the font's own spacing between glyphs
    and one newline per row: the 64 by 4 figure the font draws.

Pass a local copy of the font file as the first argument to skip the
download::

    uv run --with pyfiglet python scripts/make_banner.py path/to/ansiltr4.tdf
"""

from __future__ import annotations

import pathlib
import sys
from urllib.request import urlopen

import pyfiglet

DOS_REBEL_FONT = "dos_rebel"
DOS_REBEL_WIDTH = 200
BANNER_TEXT = "facetMedia"

ANSILTR4_COMMIT = "0225b8881d06773b92d9db2c7881f03ff0bdece7"
ANSILTR4_URL = (
    f"https://raw.githubusercontent.com/tat3r/tdfiglet/{ANSILTR4_COMMIT}/unused-fonts/ansiltr4.tdf"
)

TARGET = pathlib.Path(__file__).resolve().parents[1] / "src" / "facetmedia"

HEADER_SIZE = 233
CHAR_TABLE = 45
CHAR_COUNT = 94
FIRST_CHAR = 33
SPACING_OFFSET = 42
CARRIAGE_RETURN = 13


def dos_rebel() -> str:
    """The word in DOS Rebel, one line, with pyfiglet's blank tail trimmed."""
    figure = pyfiglet.figlet_format(BANNER_TEXT, font=DOS_REBEL_FONT, width=DOS_REBEL_WIDTH)
    rows = [line.rstrip() for line in figure.splitlines()]
    while rows and not rows[-1]:
        rows.pop()
    return "\n".join(rows) + "\n"


def glyph_rows(font: bytes, char: str) -> list[str]:
    """One block-font glyph: its rows, each padded to the glyph's width."""
    offsets = [
        int.from_bytes(font[CHAR_TABLE + 2 * index : CHAR_TABLE + 2 * index + 2], "little")
        for index in range(CHAR_COUNT)
    ]
    start = HEADER_SIZE + offsets[ord(char) - FIRST_CHAR]
    width, height = font[start], font[start + 1]
    rows: list[str] = []
    row: list[int] = []
    position = start + 2
    while font[position] != 0:
        if font[position] == CARRIAGE_RETURN:
            rows.append(bytes(row).decode("cp437").ljust(width))
            row = []
        else:
            row.append(font[position])
        position += 1
    if row:
        rows.append(bytes(row).decode("cp437").ljust(width))
    return rows[:height]


def ansiltr4(font: bytes) -> str:
    """The word in ansiltr4: one block-font figure, one newline per row."""
    spacing = " " * font[SPACING_OFFSET]
    glyphs = [glyph_rows(font, char) for char in BANNER_TEXT]
    height = max(len(rows) for rows in glyphs)
    lines: list[str] = []
    for row in range(height):
        cells = [rows[row] if row < len(rows) else " " * len(rows[0]) for rows in glyphs]
        lines.append(spacing.join(cells).rstrip())
    return "\n".join(lines) + "\n"


def read_font(argv: list[str]) -> bytes:
    """The pinned font from a local path, or fetched from its commit."""
    if argv:
        return pathlib.Path(argv[0]).read_bytes()
    with urlopen(ANSILTR4_URL) as response:
        return response.read()


def main(argv: list[str]) -> int:
    """Write both banner resources next to the package."""
    (TARGET / "banner-dos-rebel.txt").write_text(dos_rebel(), encoding="utf-8")
    (TARGET / "banner-ansiltr4.txt").write_text(ansiltr4(read_font(argv)), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
