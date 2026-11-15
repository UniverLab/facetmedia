"""Runner for the future fixture package, written on the runner SDK.

The package is incompatible with every current facet release, but its entry
still registers the verbs ``facet registry validate`` requires.
"""

from __future__ import annotations

from typing import Any

from facet_runner_sdk import Request, Runner

runner = Runner()


@runner.verb("run")
def run(req: Request) -> dict[str, Any]:
    return {"tool": {"name": "future", "version": "0.1.0"}, "assets": []}


@runner.verb("doctor")
def doctor(req: Request) -> dict[str, Any]:
    return {
        "checks": [
            {
                "name": "runner",
                "ok": True,
                "detail": f"future doctor ready ({req.depth})",
                "fix": "",
            }
        ]
    }


runner.main()
