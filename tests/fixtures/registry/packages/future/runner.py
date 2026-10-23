"""Stdlib runner placeholder for the future fixture package."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    Path(argv[2]).write_text(json.dumps({"tool": {"name": "future", "version": "0.1.0"}}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
