"""The outward side of a ``post`` capability: preview, receipt and read-back.

A ``post`` capability sends something out instead of writing a file: the
request cannot be undone, may cost money, and may leave the post in a state
other than public.  facet keeps those obligations in one place:

- the **dry run** is the runner's ``dry-run`` verb, which sends nothing and
  answers what would be sent;
- the **receipt** is the asset of a real run: a JSON file next to the piece's
  other outputs that says what left, which account, the post ids and public
  URLs, the state the network answered, the source assets with their record
  hashes, the cost, and when it was sent and verified;
- the **receipt-exists rule** refuses a second real run over the same output
  before anything is confirmed or sent;
- the **read-back** re-reads the post from the network and judges it against
  the receipt, so facet never reports more than the network holds.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from facetmedia import record, runner
from facetmedia.contract import FacetError

__all__ = [
    "DRY_RUN_VERB",
    "RECEIPT_KIND",
    "RECEIPT_MIME",
    "RECEIPT_SUFFIX",
    "STATES",
    "announce",
    "build_receipt",
    "exists",
    "judge",
    "parse_dry_run",
    "parse_post",
    "parse_read_back",
    "post_section",
    "receipt_name",
    "receipt_path",
    "refuse_existing",
    "run_asset",
    "salvage",
    "seal",
    "sources",
    "verdict",
    "write_receipt",
]

#: ``talk.toml`` -> ``talk.post.json``, beside the piece's other outputs.
RECEIPT_SUFFIX: Final[str] = ".post.json"
#: The runner verb a dry run calls: it sends nothing and writes nothing.
DRY_RUN_VERB: Final[str] = "dry-run"
RECEIPT_KIND: Final[str] = "post"
RECEIPT_MIME: Final[str] = "application/json"
RECEIPT_VERSION: Final[int] = 1
#: What the network may hold a post as; ``unknown`` is facet's, after a failure.
STATES: Final[tuple[str, ...]] = (
    "published",
    "draft",
    "private",
    "scheduled",
    "unknown",
)
#: What a read-back may answer: ``unknown`` is valid when the network forbids read-back.
READ_BACK_STATES: Final[tuple[str, ...]] = STATES
STAMP_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%SZ"
COST_FIELDS: Final[tuple[str, ...]] = ("amount", "currency")


def receipt_name(piece_path: Path | None, capability_id: str) -> str:
    """The receipt's file name, derived from the piece — never from the run id.

    A piece file ``talk.toml`` gives ``talk.post.json``; a piece read from
    stdin has no stem, so the capability's short name is used instead.  Both
    are stable, which is what makes the receipt-exists rule work at all.
    """
    stem = piece_path.stem if piece_path is not None else capability_id.split(".", 1)[-1]
    return f"{stem}{RECEIPT_SUFFIX}"


def receipt_path(out_dir: Path, name: str) -> Path:
    """Where a real run would write its receipt."""
    return out_dir / name


def exists(out_dir: Path, name: str) -> bool:
    """Whether this output already holds the receipt a real run would write."""
    return record.safe_is_file(receipt_path(out_dir, name))


def refuse_existing(out_dir: Path, name: str) -> None:
    """The receipt-exists rule: refuse a second real run before anything else.

    The hint says both ways to send it again on purpose, because a person who
    deleted a receipt or renamed the output has a lunch to eat.
    """
    target = receipt_path(out_dir, name)
    if not exists(out_dir, name):
        return
    raise FacetError(
        "invalid_input",
        f"{target}: this piece was already sent",
        hint=(
            "remove that receipt to send it again, or write it to another output folder with --out"
        ),
        details={"receipt": str(target)},
    )


def sources(
    paths: Sequence[Path], entries: Mapping[str, Mapping[str, str]]
) -> list[dict[str, object]]:
    """The source assets a post carries, each with its record hash.

    A source is ``generated`` when its own record lists models under
    ``provenance.models``; a source without a record is not generated and
    carries no id, because facet has nothing to cite.
    """
    carried: list[dict[str, object]] = []
    for path in paths:
        entry = entries.get(str(Path(os.path.abspath(path)))) or _input_entry(path)
        carried.append(
            {
                "path": entry["path"] if entry is not None else str(path),
                "id": entry.get("id") if entry is not None else None,
                "generated": _generated(path),
            }
        )
    return carried


def _input_entry(path: Path) -> dict[str, str] | None:
    """The record hash of a source, or ``None`` when facet cannot read it."""
    try:
        return record.input_entry(path, Path.cwd())
    except FacetError:
        return None


def _generated(path: Path) -> bool:
    """Whether the source asset's record says a model made it."""
    try:
        stored = record.read_record(path)
    except FacetError:
        return False
    provenance = stored.get("provenance")
    if not isinstance(provenance, Mapping):
        return False
    models = provenance.get("models")
    return isinstance(models, list) and bool(models)


def build_receipt(
    facts: Mapping[str, object],
    carried: Sequence[Mapping[str, object]],
    *,
    sent_at: str,
    verified_at: str | None = None,
) -> dict[str, object]:
    """The receipt's JSON object: what left, where, its state and its sources."""
    return {
        "receipt": RECEIPT_VERSION,
        "network": facts["network"],
        "account": facts["account"],
        "state": facts["state"],
        "posts": [dict(item) for item in _list(facts.get("posts"))],
        "sources": [dict(item) for item in carried],
        "cost": facts.get("cost"),
        "sent_at": sent_at,
        "verified_at": verified_at,
    }


def seal(receipt: Mapping[str, object]) -> dict[str, object]:
    """The receipt after a read-back that failed: the post may exist, unknown."""
    sealed = dict(receipt)
    sealed["state"] = "unknown"
    sealed["verified_at"] = None
    return sealed


def salvage(
    result: Mapping[str, object],
    carried: Sequence[Mapping[str, object]],
    *,
    network: str,
    account: str,
    sent_at: str,
) -> dict[str, object]:
    """The receipt for a send whose result facet could not read: ``unknown``.

    The runner reported that it ran, so a request may have left; the receipt
    exists to stop a second accidental send.  It claims nothing the runner did
    not say: the network and account fall back to what the piece was meant
    for, and the state is facet's ``unknown``.
    """
    raw = result.get("post")
    facts = raw if isinstance(raw, Mapping) else {}
    return build_receipt(
        {
            "network": _text_or(facts.get("network"), network),
            "account": _text_or(facts.get("account"), account),
            "state": "unknown",
            "posts": _salvage_posts(facts.get("posts")),
            "cost": facts.get("cost") if isinstance(facts.get("cost"), Mapping) else None,
        },
        carried,
        sent_at=sent_at,
    )


def _text_or(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and value else fallback


def _salvage_posts(value: object) -> list[dict[str, str]]:
    """The post ids and URLs the result did state, if any."""
    return [
        {"id": str(item["id"]), "url": str(item["url"])}
        for item in _list(value)
        if isinstance(item.get("id"), str) and isinstance(item.get("url"), str)
    ]


def judge(receipt: Mapping[str, object], measured: Mapping[str, object]) -> bool:
    """Whether the read-back holds exactly what the receipt claims."""
    return measured.get("state") == receipt.get("state") and _media_count(measured) == len(
        _list(receipt.get("sources"))
    )


def verdict(
    receipt: Mapping[str, object],
    measured: Mapping[str, object],
    thresholds: Mapping[str, float],
) -> runner.VerifyResult:
    """The read-back as a verdict; a mismatch fails and never reports more."""
    return runner.VerifyResult(
        file=str(receipt.get("path", "")),
        passed=judge(receipt, measured),
        metrics={"state": measured.get("state"), "media": _media_count(measured)},
        thresholds=dict(thresholds),
        verifier="post-read-back",
    )


def post_section(facts: Mapping[str, object]) -> dict[str, object]:
    """The post facts a read-back needs, without the receipt's own bookkeeping."""
    return {
        "network": facts.get("network"),
        "account": facts.get("account"),
        "posts": [dict(item) for item in _list(facts.get("posts"))],
        "state": facts.get("state"),
    }


def run_asset(
    name: str, receipt: Mapping[str, object], carried_paths: tuple[Path, ...]
) -> runner.RunAsset:
    """The receipt as the run's one asset: ``post``, its network, state and URL."""
    return runner.RunAsset(
        file=name,
        name=Path(name).stem,
        kind=RECEIPT_KIND,
        mime=RECEIPT_MIME,
        properties={
            "network": receipt.get("network"),
            "state": receipt.get("state"),
            "url": _first_url(receipt),
        },
        inputs=tuple(str(path) for path in carried_paths),
    )


def write_receipt(path: Path, receipt: Mapping[str, object]) -> None:
    """Write the receipt file, which ``_place`` then moves out with its record."""
    path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def parse_dry_run(result: Mapping[str, object], temp_dir: Path) -> dict[str, object]:
    """The dry-run result: one entry per post, what would be sent.

    The group's own ``warnings`` ride along: the runner may have something to
    say about the whole send rather than about one post.
    """
    posts = result.get("posts")
    if not isinstance(posts, list):
        raise _protocol(temp_dir, "the dry-run result has no posts list")
    warnings = _warnings(result.get("warnings", []), temp_dir)
    return {"posts": [_dry_post(item, temp_dir) for item in posts], "warnings": warnings}


def _warnings(value: object, temp_dir: Path) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
        raise _protocol(temp_dir, "the dry-run warnings are not a list of strings")
    return list(value)


def _dry_post(item: object, temp_dir: Path) -> dict[str, object]:
    if not isinstance(item, Mapping):
        raise _protocol(temp_dir, "a dry-run post is not a JSON object")
    media = item.get("media")
    if not isinstance(media, list) or not all(isinstance(entry, str) for entry in media):
        raise _protocol(temp_dir, "a dry-run post has media that is not a list of paths")
    return {
        "text": _text(item.get("text"), temp_dir, "a dry-run post has no text"),
        "account": _text(item.get("account"), temp_dir, "a dry-run post has no account"),
        "media": media,
        "warnings": _warnings(item.get("warnings", []), temp_dir),
        "cost": _cost(item.get("cost"), temp_dir),
    }


def parse_post(result: Mapping[str, object], temp_dir: Path) -> dict[str, object]:
    """The run result of an outward capability: what the network answered."""
    post = result.get("post")
    if not isinstance(post, Mapping):
        raise _protocol(temp_dir, "the run result has no post object")
    state = post.get("state")
    if not isinstance(state, str) or state not in STATES:
        raise _protocol(
            temp_dir,
            f"the run result's post state must be one of {', '.join(STATES)}",
        )
    posts = post.get("posts")
    if not isinstance(posts, list) or not posts:
        raise _protocol(temp_dir, "the run result's post has no posts list")
    return {
        "network": _text(post.get("network"), temp_dir, "the post names no network"),
        "account": _text(post.get("account"), temp_dir, "the post names no account"),
        "state": state,
        "posts": [_post_ref(item, temp_dir) for item in posts],
        "cost": _cost(post.get("cost"), temp_dir),
    }


def _post_ref(item: object, temp_dir: Path) -> dict[str, str]:
    if not isinstance(item, Mapping):
        raise _protocol(temp_dir, "a post id is not a JSON object")
    return {
        "id": _text(item.get("id"), temp_dir, "a post has no id"),
        "url": _text(item.get("url"), temp_dir, "a post has no url"),
    }


def parse_read_back(result: Mapping[str, object], temp_dir: Path) -> dict[str, object]:
    """What the network holds now: the state and the media it lists."""
    state = result.get("state")
    if not isinstance(state, str) or state not in READ_BACK_STATES:
        raise _protocol(
            temp_dir,
            f"the read-back state must be one of {', '.join(READ_BACK_STATES)}",
        )
    media = result.get("media")
    if not isinstance(media, list):
        raise _protocol(temp_dir, "the read-back has no media list")
    return {"state": state, "media": media}


def announce(preview: Mapping[str, object]) -> None:
    """The dry-run preview as plain lines: what would be sent, one block a post."""
    posts = _list(preview.get("posts"))
    print(f"dry run: {len(posts)} post(s), nothing sent")
    for index, item in enumerate(posts, 1):
        media = _strings(item.get("media"))
        print(
            f"{index}  {item.get('account')}  media {len(media)}  "
            f"cost {_cost_text(item.get('cost'))}  {item.get('text')}"
        )
        for warning in _strings(item.get("warnings")):
            print(f"   ! {warning}")
    for warning in _strings(preview.get("warnings")):
        print(f"! {warning}")


def _cost_text(cost: object) -> str:
    if not isinstance(cost, Mapping):
        return "-"
    return f"{cost.get('amount')} {cost.get('currency')}"


def _first_url(receipt: Mapping[str, object]) -> str | None:
    posts = _list(receipt.get("posts"))
    if not posts:
        return None
    url = posts[0].get("url")
    return url if isinstance(url, str) else None


def _media_count(measured: Mapping[str, object]) -> int:
    media = measured.get("media")
    return len(media) if isinstance(media, list) else 0


def _cost(value: object, temp_dir: Path) -> Mapping[str, object] | None:
    """The runner's cost estimate, passed through as it reported it."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or any(field not in value for field in COST_FIELDS):
        raise _protocol(temp_dir, "a cost must be an object with an amount and a currency")
    return dict(value)


def _text(value: object, temp_dir: Path, message: str) -> str:
    if not isinstance(value, str) or not value:
        raise _protocol(temp_dir, message)
    return value


def _list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item if isinstance(item, Mapping) else {} for item in value]


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _protocol(temp_dir: Path, message: str) -> FacetError:
    """A runner that broke the post protocol, with the temp dir to inspect."""
    return FacetError("runner_failed", message, details={"temp": str(temp_dir)})


def now() -> str:
    """ISO 8601 in UTC, the stamp a receipt's ``sent_at`` and ``verified_at`` use."""
    return datetime.now(UTC).strftime(STAMP_FORMAT)
