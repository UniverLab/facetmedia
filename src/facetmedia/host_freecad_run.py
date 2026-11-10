"""Runs a FreeCAD script inside freecadcmd and turns its outcome into an exit code.

Ported from the lab's ``tools/freecad_run.py``.  freecadcmd exits 0 and prints
nothing when a script raises, so this wrapper executes the target with
``runpy``, prints the traceback, and exits 1 on failure.  The runner calls it as
``<freecadcmd> host_freecad_run.py --pass <script.py> [args...]``; inside the
target script ``sys.argv`` is ``[script.py, args...]``.
"""

from __future__ import annotations

import contextlib
import os
import runpy
import sys
import traceback
from typing import Final

USAGE: Final[str] = "usage: host_freecad_run.py --pass <script.py> [args...]"
PASS_FLAG: Final[str] = "--pass"


def main(argv: list[str]) -> int:
    """Run the script named after ``--pass`` and return the exit code to report."""
    args = argv[argv.index(PASS_FLAG) + 1 :] if PASS_FLAG in argv else []
    if not args:
        print(USAGE, flush=True)
        return 2
    sys.argv = args
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError):
                reconfigure(encoding="utf-8")
    try:
        runpy.run_path(args[0], run_name="__main__")
    except SystemExit as exit_request:
        code = exit_request.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    except BaseException:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        return 1
    sys.stdout.flush()
    sys.stderr.flush()
    return 0


# freecadcmd executes a script with ``__name__`` set to the file's stem, not
# ``__main__``; imported as ``facetmedia.host_freecad_run`` it stays inert.
if __name__ in ("__main__", "host_freecad_run"):
    os._exit(main(sys.argv[1:]))
