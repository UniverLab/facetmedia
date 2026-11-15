"""A shared module the fixture packages never declare.

Requires: stdlib
"""

from __future__ import annotations


def unused() -> str:
    """Return a marker string; nothing imports this module."""
    return "unused"
