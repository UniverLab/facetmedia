"""The ``publish`` facet and the ``post`` kind: manifest, gallery, registry.

The outward package has its own fixture registry (``outward-registry``) so
this contract's package never changes what every golden list in the other
tests sees.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from facetmedia.cli import main
from facetmedia.preview_folder import FolderAsset, collect, gallery_data, text_table
from facetmedia.registry import load_registry, validate_registry
from facetmedia.registry_types import CAPABILITY_KINDS, FACETS

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "outward-registry"
EXIT_OK = 0
EXIT_INVALID_INPUT = 2


def _problems(root: Path) -> set[tuple[str, str, str]]:
    return {(p["file"], p["key"], p["problem"]) for p in validate_registry(root)}


def test_publish_joins_the_facets_and_post_the_kinds() -> None:
    assert "publish" in FACETS
    assert "post" in CAPABILITY_KINDS


def test_the_outward_fixture_is_a_clean_registry() -> None:
    assert validate_registry(FIXTURES) == []


def test_an_outward_capability_parses_as_outward_and_kind_post() -> None:
    registry = load_registry(FIXTURES)
    package = registry.package("bluesky")
    assert package is not None
    assert package.facet == "publish"
    capability = registry.capability("bluesky.send")
    assert capability.kind == "post"
    assert capability.outward is True


def _write_manifest(tmp_path: Path, manifest: str) -> Path:
    """A one-package registry whose package.toml is ``manifest``."""
    (tmp_path / "index.toml").write_text(
        'version = 1\n[[packages]]\nname = "pkg"\n', encoding="utf-8"
    )
    package = tmp_path / "packages" / "pkg"
    package.mkdir(parents=True)
    (package / "package.toml").write_text(manifest, encoding="utf-8")
    (package / "pyproject.toml").write_text(
        '[project]\nname = "pkg"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (package / "uv.lock").write_text("", encoding="utf-8")
    skill = package / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: pkg\n"
        "description: A valid test skill for the pkg package fixture.\n"
        "---\n"
        "# Pkg\n\nBody mentions pkg.one.\n\n"
        "## When to use\n\nUse it in tests.\n\n"
        "## Pieces\n\n```toml\n[inputs]\n```\n\n"
        "## Judging the result\n\nVerify measures nothing.\n\n"
        "## Pitfalls\n\n- None.\n",
        encoding="utf-8",
    )
    (package / "runner.py").write_text(
        '''"""Stub runner: only the doctor decorator the validator looks for."""

from __future__ import annotations

from typing import Any

from facet_runner_sdk import Request, Runner

runner = Runner()


@runner.verb("doctor")
def doctor(req: Request) -> dict[str, Any]:
    return {"checks": [{"name": "runner", "ok": True, "detail": "ready", "fix": ""}]}


runner.main()
''',
        encoding="utf-8",
    )
    viewer = tmp_path / "viewers" / "generic"
    viewer.mkdir(parents=True)
    (viewer / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
    return package


BASE = """name = "pkg"
version = "0.1.0"
requires_facet = ">=0.1.0"
protocol = 1
provider = "test"
tool_name = "Pkg"
description = "test"
facet = "publish"

[engine]
type = "python-env"
python = "3.12"

[transport]
type = "cli"
entry = "runner.py"

[requirements]
gpu = false
ram_gb = 0.1
disk_gb = 0.01
exclusive = false
platforms = ["linux"]

[[licenses]]
subject = "test"
spdx = "Apache-2.0"
"""

PREVIEW = '\n[capabilities.one.preview]\nviewer = "generic"\n'


def _capability(kind: str, *, outward: bool) -> str:
    flag = "\noutward = true" if outward else ""
    return BASE + f'\n[capabilities.one]\nsummary = "one"\nkind = "{kind}"{flag}\n' + PREVIEW


def test_an_outward_capability_of_another_kind_is_refused(tmp_path: Path) -> None:
    _write_manifest(tmp_path, _capability("text", outward=True))
    assert _problems(tmp_path) == {
        (
            "packages/pkg/package.toml",
            "capabilities.one.outward",
            "an outward capability must have kind post",
        )
    }


def test_a_post_capability_that_is_not_outward_is_refused(tmp_path: Path) -> None:
    _write_manifest(tmp_path, _capability("post", outward=False))
    assert _problems(tmp_path) == {
        (
            "packages/pkg/package.toml",
            "capabilities.one.outward",
            "a post capability must be outward",
        )
    }


def test_an_outward_post_capability_is_accepted(tmp_path: Path) -> None:
    _write_manifest(tmp_path, _capability("post", outward=True))
    assert _problems(tmp_path) == set()


# --- the folder gallery: one line with the network, the state and the URL.


def _post_record(directory: Path, name: str) -> Path:
    """A receipt asset with its record, as a real outward run writes them."""
    directory.mkdir(parents=True, exist_ok=True)
    receipt = directory / name
    receipt.write_text(
        json.dumps(
            {
                "receipt": 1,
                "network": "bluesky",
                "account": "lab.test",
                "state": "draft",
                "posts": [{"id": "at://1", "url": "https://bsky.app/profile/lab.test/post/at://1"}],
                "sources": [],
                "cost": None,
                "sent_at": "2026-11-17T10:00:00Z",
                "verified_at": None,
            }
        ),
        encoding="utf-8",
    )
    record = {
        "record": 1,
        "id": "sha256:0",
        "name": name.removesuffix(".post.json"),
        "path": name,
        "kind": "post",
        "mime": "application/json",
        "bytes": receipt.stat().st_size,
        "created": "2026-11-17T10:00:00Z",
        "properties": {
            "network": "bluesky",
            "state": "draft",
            "url": "https://bsky.app/profile/lab.test/post/at://1",
        },
        "provenance": {
            "capability": "bluesky.send",
            "package": {"name": "bluesky", "version": "0.1.0"},
            "params": {},
        },
        "verification": {"status": "passed"},
    }
    receipt.with_name(f"{name}.facet.json").write_text(json.dumps(record), encoding="utf-8")
    return receipt


def test_a_post_asset_names_its_network_state_and_url(tmp_path: Path) -> None:
    _post_record(tmp_path / "out", "talk.post.json")
    entries = collect(tmp_path / "out", frozenset({"post"}))
    assert len(entries) == 1
    entry = entries[0]
    assert isinstance(entry, FolderAsset)
    assert entry.kind == "post"
    assert entry.network == "bluesky"
    assert entry.post_state == "draft"
    assert entry.url == "https://bsky.app/profile/lab.test/post/at://1"


def test_the_gallery_card_carries_the_post_fields(tmp_path: Path) -> None:
    _post_record(tmp_path / "out", "talk.post.json")
    entries = collect(tmp_path / "out")
    card = gallery_data(entries, "out", openable=frozenset())["assets"][0]
    assert card["network"] == "bluesky"
    assert card["post_state"] == "draft"
    assert card["url"] == "https://bsky.app/profile/lab.test/post/at://1"


def test_the_text_table_gives_a_post_its_own_line(tmp_path: Path) -> None:
    _post_record(tmp_path / "out", "talk.post.json")
    table = text_table(collect(tmp_path / "out"), "out")
    lines = table.splitlines()
    assert "network" in lines[1] and "url" in lines[1]
    assert "bluesky" in lines[2]
    assert "draft" in lines[2]
    assert "https://bsky.app/profile/lab.test/post/at://1" in lines[2]


def test_the_kind_filter_keeps_only_posts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "out"
    _post_record(root, "talk.post.json")
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))
    assert main(["preview", str(root), "--kind", "post", "--json"]) == EXIT_OK
    data: Any = json.loads(capsys.readouterr().out)["data"]
    assert [entry["asset"] for entry in data["assets"]] == ["talk.post.json"]
    assert data["assets"][0]["post_state"] == "draft"


def test_a_kind_filter_that_keeps_nothing_is_invalid_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--kind post`` is accepted; a filter that leaves no asset still refuses."""
    root = tmp_path / "out"
    _post_record(root, "talk.post.json")
    text = root / "notes.txt"
    text.write_text("hello", encoding="utf-8")
    monkeypatch.setenv("FACET_REGISTRY", str(FIXTURES))
    assert main(["preview", str(root), "--kind", "video", "--json"]) == EXIT_INVALID_INPUT
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_input"
