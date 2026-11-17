"""The poster fixture's runner: it posts with a declared secret.

``run`` prints the app password it was given — on purpose, so facet's mask
can be observed — and writes ``post.txt``. ``verify`` compares the file with
the text. ``auth`` returns the two secrets the manifest declares, which is
how ``facet auth`` gets something to store.
"""

from __future__ import annotations

import os
from typing import Any

from facet_runner_sdk import Request, Runner, secret

runner = Runner()


@runner.verb("run")
def run(req: Request) -> dict[str, Any]:
    password = secret("app-password")
    handle = os.environ.get("FACET_SECRET_HANDLE", "")
    text = str(req.inputs["text"])
    print(f"posting {text!r} as {handle} with {password}")
    (req.out_dir / "post.txt").write_text(text, encoding="utf-8")
    return {
        "tool": {"name": "poster", "version": "0.1.0"},
        "assets": [
            {
                "file": "post.txt",
                "name": "post",
                "kind": "text",
                "mime": "text/plain",
                "properties": {"chars": len(text)},
                "params": dict(req.inputs),
                "seed": None,
                "inputs": [],
            }
        ],
    }


@runner.verb("verify")
def verify(req: Request) -> dict[str, Any]:
    expected = str(req.inputs["text"])
    return {
        "assets": [
            {
                "file": path.name,
                "passed": path.read_text(encoding="utf-8") == expected,
                "metrics": {},
                "thresholds": {},
                "verifier": "poster-text",
            }
            for path in req.assets
        ]
    }


@runner.verb("auth")
def auth(req: Request) -> dict[str, Any]:
    print("open http://127.0.0.1:1/authorize")
    return {"secrets": {"app-password": "returned-password", "handle": "labbrand"}}


@runner.verb("doctor")
def doctor(req: Request) -> dict[str, Any]:
    return {"checks": [{"name": "runner", "ok": True, "detail": "ready", "fix": ""}]}


runner.main()
