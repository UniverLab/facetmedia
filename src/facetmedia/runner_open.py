"""Best-effort opening of a runner-announced sign-in address in a browser.

Split out of ``runner`` so that module stays under its line budget.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import webbrowser
from collections import deque
from collections.abc import Sequence

from facetmedia import secrets as secrets_module
from facetmedia.runner_sdk import ANNOUNCE_MARKER

logger = logging.getLogger("facetmedia")


def handle_line(line: str, tail: deque[str], values: Sequence[str], allow_open: bool) -> bool:
    """Handle one runner output line; True when it announced a sign-in address.

    An announced address is shown on stderr unmasked (never appended to the
    tail) and recorded in the tail and the log masked with the marker
    stripped, exactly as any other line.  It is then opened once in the
    browser when allowed.  Every other line is left for the caller to mask.
    """
    if not line.startswith(ANNOUNCE_MARKER):
        return False
    url = line[len(ANNOUNCE_MARKER) :].strip()
    masked = secrets_module.mask(url, values)
    tail.append(f"{masked}\n" if line.endswith("\n") else masked)
    logger.info("%s", masked)
    sys.stderr.write("open this address and sign in:\n")
    sys.stderr.write(f"{url}\n")
    sys.stderr.flush()
    if should_open(allow_open) and try_open(url):
        sys.stderr.write("opened in your browser\n")
        sys.stderr.flush()
    return True


def should_open(allow: bool) -> bool:
    """Whether facet may try to open an announced address right now."""
    return allow and sys.stderr.isatty()


def try_open(url: str) -> bool:
    """Open ``url`` once, silently; True when one route reports success."""
    try:
        if webbrowser.open(url):
            return True
    except Exception:
        return False
    if shutil.which("wslview") is None:
        return False
    try:
        completed = subprocess.run(
            ["wslview", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except Exception:
        return False
    return completed.returncode == 0
