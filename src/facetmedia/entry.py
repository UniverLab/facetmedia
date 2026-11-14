"""The console entry point: refuse an unsupported platform before importing.

The CLI imports POSIX-only modules (``fcntl``), so native Windows would crash
with a traceback before ``cli.main`` could refuse it. This module imports
only what every platform has.
"""

from __future__ import annotations

import sys

from facetmedia.contract import FacetError, emit, failure
from facetmedia.home import require_linux


def main(argv: list[str] | None = None) -> int:
    """Refuse an unsupported platform, else run the CLI."""
    try:
        require_linux()
    except FacetError as exc:
        emit(failure(exc), "--json" in (argv if argv is not None else sys.argv))
        return exc.exit_code
    from facetmedia.cli import main as cli_main  # noqa: PLC0415 - imports fcntl

    return cli_main(argv)
