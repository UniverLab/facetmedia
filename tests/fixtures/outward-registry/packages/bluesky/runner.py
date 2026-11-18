"""The bluesky fixture package's runner: one outward capability, no network.

Every verb keeps facet's outward contract without touching a real account:

- ``dry-run`` sends nothing and answers per post what would be sent: the
  rendered text, the media, the target account, its warnings and the cost;
- ``run`` sends nothing either, but reports it as sent: the network, the
  account, the state the network answered and one id and URL per post.
  ``FIXTURE_BAD_RUN`` answers without the ``post`` object, so a test can show
  a send whose result facet cannot read;
- ``verify`` re-reads the post back.  ``FIXTURE_READ_BACK`` changes the state
  the read-back answers, so a test can show a post facet sent but the network
  holds as a draft; ``FIXTURE_DROP_MEDIA`` reports no media, to show the count
  must match;
- ``preview`` writes the kind's small preview file;
- ``doctor`` answers at both depths.

``FIXTURE_CALL_LOG`` names a file every outward verb appends its name to, so
a test can show facet called ``run`` exactly once and never retried it.
``FIXTURE_RENEW_DRY_RUN`` and ``FIXTURE_RENEW_RUN`` make that verb renew an
undeclared secret, so a test can show the renewal warning survives the path.

The credential is read through the SDK, so a run without
``FACET_SECRET_APP_PASSWORD`` fails the way a real package would.
"""

from __future__ import annotations

import json
import os
from typing import Any

from facet_runner_sdk import Request, Runner, renew_secret, secret

runner = Runner()

NETWORK = "bluesky"
ACCOUNT = "lab.test"
POST_STATE = "published"
COST = {"amount": "0", "currency": "USD"}
MAX_TEXT = 300


def _logged(verb: str) -> None:
    """Append ``verb`` to ``FIXTURE_CALL_LOG`` when a test asked for a log."""
    path = os.environ.get("FIXTURE_CALL_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{verb}\n")


def _renew(verb: str) -> None:
    """Renew an undeclared secret when a test asked for the warning path."""
    flag = {"dry-run": "FIXTURE_RENEW_DRY_RUN", "run": "FIXTURE_RENEW_RUN"}.get(verb)
    if flag and os.environ.get(flag):
        renew_secret("rotated", f"{verb}-token")


@runner.verb("dry-run")
def dry_run(req: Request) -> dict[str, Any]:
    _logged("dry-run")
    _renew("dry-run")
    secret("app-password")
    text = str(req.inputs["text"])
    media = [str(item["image"]) for item in req.inputs.get("media", [])]
    warnings = [f"the text is over the network's {MAX_TEXT} limit"] if len(text) > MAX_TEXT else []
    return {
        "posts": [
            {
                "text": text,
                "account": ACCOUNT,
                "media": media,
                "warnings": warnings,
                "cost": COST,
            }
        ],
        "warnings": [],
    }


@runner.verb("run")
def run(req: Request) -> dict[str, Any]:
    _logged("run")
    _renew("run")
    secret("app-password")
    text = str(req.inputs["text"])
    state = os.environ.get("FIXTURE_POST_STATE", POST_STATE)
    post_id = f"at://{hash(text) & 0xFFFF:04x}"
    if os.environ.get("FIXTURE_BAD_RUN"):
        return {"tool": {"name": "bluesky", "version": "0.1.0"}, "assets": []}
    return {
        "tool": {"name": "bluesky", "version": "0.1.0"},
        "post": {
            "network": NETWORK,
            "account": ACCOUNT,
            "state": state,
            "posts": [{"id": post_id, "url": f"https://bsky.app/profile/{ACCOUNT}/post/{post_id}"}],
            "cost": COST,
        },
        "assets": [],
    }


@runner.verb("verify")
def verify(req: Request) -> dict[str, Any]:
    """The read-back: what the network holds now, and how many media it lists.

    The fixture's network holds what was sent, so its media is one entry per
    source; ``FIXTURE_READ_BACK`` changes the state it answers and
    ``FIXTURE_DROP_MEDIA`` makes it list none.
    """
    _logged("verify")
    state = os.environ.get("FIXTURE_READ_BACK", "published")
    media = [str(item.get("path")) for item in req.sources]
    if os.environ.get("FIXTURE_DROP_MEDIA"):
        media = []
    return {"state": state, "media": media}


@runner.verb("preview")
def preview(req: Request) -> dict[str, Any]:
    entries = []
    for path in req.assets:
        name = f"{path.name}.preview.json"
        (req.out_dir / name).write_text(
            json.dumps(
                {"preview": 1, "kind": "post", "summary": "one post", "network": NETWORK},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        entries.append({"file": path.name, "preview": name, "images": []})
    return {"assets": entries}


@runner.verb("doctor")
def doctor(req: Request) -> dict[str, Any]:
    checks: list[dict[str, Any]] = [
        {
            "name": "imports",
            "ok": True,
            "detail": f"{req.capability} bluesky runner imports ({req.depth})",
            "fix": "",
        }
    ]
    if req.depth == "full":
        checks.append({"name": "sign-in", "ok": True, "detail": "credential is set", "fix": ""})
    return {"checks": checks}


runner.main()
