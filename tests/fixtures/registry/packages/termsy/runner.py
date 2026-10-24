"""Runner for the termsy fixture package, written on the runner SDK.

The full runner protocol lands in feat(run); this fixture only needs to be
a real runner the registry validator accepts and the doctor can call.
"""

from __future__ import annotations

from typing import Any

from facet_runner_sdk import Request, Runner

runner = Runner()


@runner.verb("run")
def run(req: Request) -> dict[str, Any]:
    (req.out_dir / "termsy.txt").write_text("termsy\n", encoding="utf-8")
    return {
        "tool": {"name": "termsy", "version": "0.1.0"},
        "assets": [
            {
                "file": "termsy.txt",
                "name": "termsy",
                "kind": "text",
                "mime": "text/plain",
                "properties": {},
                "params": dict(req.inputs),
                "seed": None,
                "inputs": [],
            }
        ],
    }


@runner.verb("doctor")
def doctor(req: Request) -> dict[str, Any]:
    return {
        "checks": [
            {
                "name": "runner",
                "ok": True,
                "detail": f"termsy doctor ready ({req.depth})",
                "fix": "",
            }
        ]
    }


runner.main()
