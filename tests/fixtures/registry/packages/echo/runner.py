"""Stdlib runner for the echo fixture package.

The full runner protocol lands in feat(run); this fixture only needs to be a
real file the registry validator can find.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    request = json.loads(Path(argv[1]).read_text())
    text = str(request.get("inputs", {}).get("text", ""))
    repeat = int(request.get("inputs", {}).get("repeat", 1))
    out_dir = Path(request["out_dir"])
    (out_dir / "echo.txt").write_text(text * repeat)
    Path(argv[2]).write_text(json.dumps({"tool": {"name": "echo", "version": "0.1.0"}}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
