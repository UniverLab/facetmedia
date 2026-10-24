"""The echo fixture package's runner, written on the runner SDK.

`run` writes ``echo.txt`` holding ``text`` repeated ``repeat`` times;
``verify`` passes when the file's content equals the expected text;
``preview`` writes the text preview file; ``probe`` answers ok.
"""

from __future__ import annotations

import json
from typing import Any

from facet_runner_sdk import Request, Runner

runner = Runner()


@runner.verb("run")
def run(req: Request) -> dict[str, Any]:
    text = str(req.inputs["text"])
    repeat = int(req.inputs.get("repeat", 1))
    (req.out_dir / "echo.txt").write_text(text * repeat, encoding="utf-8")
    return {
        "tool": {"name": "echo", "version": "0.1.0"},
        "assets": [
            {
                "file": "echo.txt",
                "name": "echo",
                "kind": "text",
                "mime": "text/plain",
                "properties": {"chars": len(text) * repeat},
                "params": dict(req.inputs),
                "seed": None,
                "inputs": [],
            }
        ],
    }


@runner.verb("verify")
def verify(req: Request) -> dict[str, Any]:
    expected = str(req.inputs["text"]) * int(req.inputs.get("repeat", 1))
    entries = []
    for path in req.assets:
        matched = path.read_text(encoding="utf-8") == expected
        entries.append(
            {
                "file": path.name,
                "passed": matched,
                "metrics": {"match": 1 if matched else 0},
                "thresholds": {},
                "verifier": "echo-content",
            }
        )
    return {"assets": entries}


@runner.verb("preview")
def preview(req: Request) -> dict[str, Any]:
    entries = []
    for path in req.assets:
        content = path.read_text(encoding="utf-8")
        name = f"{path.name}.preview.json"
        preview_file = req.out_dir / name
        preview_file.write_text(
            json.dumps(
                {
                    "preview": 1,
                    "kind": "text",
                    "summary": content[:80],
                    "chars": len(content),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        entries.append({"file": path.name, "preview": name, "images": []})
    return {"assets": entries}


@runner.verb("probe")
def probe(req: Request) -> dict[str, Any]:
    return {"ok": True, "detail": f"{req.capability} echo runner is ready"}


runner.main()
