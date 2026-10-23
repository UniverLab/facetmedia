"""Stdlib runner for the termsy fixture package.

The full runner protocol lands in feat(run); this fixture only needs to be a
real file the registry validator can find.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    request = json.loads(Path(argv[1]).read_text())
    out_dir = Path(request["out_dir"])
    (out_dir / "termsy.txt").write_text("termsy\n")
    Path(argv[2]).write_text(json.dumps({"tool": {"name": "termsy", "version": "0.1.0"}}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
