"""The ``document`` kind end to end: the paged fixture package.

``pull`` installs the stub bookbinder and fetches the shared browser it
declares; ``run`` binds a stub PDF whose page count is one integer input;
``preview`` renders page PNGs and the ``pages`` preview file (agent) and
opens the PDF itself (person); ``check`` trusts the package's ``verify``
verb to fail on a page-count mismatch.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from facetmedia import env as env_module
from facetmedia import preview as preview_module
from facetmedia.cli import main
from tests.test_pull import BROWSER_REVISION, FIXTURES, FakeBrowser

PAGE_COUNT = 11
SHORT_PAGES = 2
IMAGE_LIMIT = 10
EXIT_VERIFICATION_FAILED = 3
REVISION_REQUEST = f"/chromium/{BROWSER_REVISION}/chromium-linux.zip"


@dataclass
class Paged:
    """Everything one test needs from the paged fixture install."""

    home: Path
    registry: Path
    browser: FakeBrowser


@pytest.fixture(scope="module")
def paged_home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Paged]:
    """One home with paged installed against a tmp registry; one fake CDN."""
    home_root = tmp_path_factory.mktemp("facet-home-paged")
    registry_root = tmp_path_factory.mktemp("paged-registry") / "registry"
    server = FakeBrowser().start()
    patcher = pytest.MonkeyPatch()
    try:
        shutil.copytree(FIXTURES, registry_root)
        _write_paged_package(registry_root, server.sha256)
        patcher.setenv("FACET_HOME", str(home_root))
        patcher.setenv("FACET_REGISTRY", str(registry_root))
        patcher.setattr(env_module, "PLAYWRIGHT_CDN", server.base)
        assert main(["pull", "paged", "--json"]) == 0
        yield Paged(home=home_root, registry=registry_root, browser=server)
    finally:
        patcher.undo()
        server.stop()


@pytest.fixture
def facet_home(paged_home: Paged, monkeypatch: pytest.MonkeyPatch) -> Paged:
    """The module home, after the autouse per-test temp home has been set."""
    monkeypatch.setenv("FACET_HOME", str(paged_home.home))
    monkeypatch.setenv("FACET_REGISTRY", str(paged_home.registry))
    return paged_home


def _argv(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    """Run ``facet <argv> --json``; the exit code and its envelope data."""
    code = main([*argv, "--json"])
    envelope: Any = json.loads(capsys.readouterr().out)
    assert isinstance(envelope, dict)
    data = envelope.get("data")
    assert isinstance(data, dict)
    return code, data


# --- the paged fixture package, written once by the module fixture.


def _write_paged_package(registry_root: Path, browser_sha256: str) -> None:
    """The ``paged`` package: echo's layout, a document kind and ``[browser]``."""
    src = FIXTURES / "packages" / "echo"
    dst = registry_root / "packages" / "paged"
    shutil.copytree(src, dst)
    shutil.rmtree(dst / "skill")
    (dst / "skill").mkdir()
    (dst / "skill" / "SKILL.md").write_text(_skill_text(), encoding="utf-8")
    (dst / "runner.py").write_text(PAGED_RUNNER, encoding="utf-8")
    (dst / "pyproject.toml").write_text(
        (dst / "pyproject.toml").read_text(encoding="utf-8").replace("facet-echo", "facet-paged"),
        encoding="utf-8",
    )
    (dst / "uv.lock").write_text(
        (dst / "uv.lock").read_text(encoding="utf-8").replace("facet-echo", "facet-paged"),
        encoding="utf-8",
    )
    (dst / "package.toml").write_text(_manifest(browser_sha256), encoding="utf-8")
    index = registry_root / "index.toml"
    index.write_text(
        index.read_text(encoding="utf-8") + '[[packages]]\nname = "paged"\n',
        encoding="utf-8",
    )


def _manifest(browser_sha256: str) -> str:
    """The paged manifest: text facet, document kind, browser and preview."""
    return f"""name = "paged"
version = "0.1.0"
requires_facet = ">=0.1.0b1,<0.2"
protocol = 1
provider = "facet"
tool_name = "Paged"
description = "The document-kind fixture package; a stub bookbinder."
facet = "text"

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
host_libs = []
host_hint = ""

[[licenses]]
subject = "paged"
spdx = "Apache-2.0"

[capabilities.doc]
summary = "Bind a stub PDF of `pages` pages."
kind = "document"

[capabilities.doc.inputs.pages]
type = "integer"
default = {PAGE_COUNT}
min = 1
max = 50

[capabilities.doc.preview]
viewer = "generic"

[browser]
channel = "chromium"
revision = "{BROWSER_REVISION}"
sha256 = "{browser_sha256}"
"""


def _skill_text() -> str:
    """The paged skill, valid for the ``paged.doc`` capability."""
    return """---
name: paged
description: The document-kind fixture package, a stub bookbinder.
---
# Paged

The paged package binds a stub PDF whose page count is its input.

## When to use

Use paged.doc to exercise the document kind: run, verify, preview and
check on an asset with pages.

## Pieces

One complete piece per capability, in a fenced `toml` block:

```toml
[inputs]
pages = 3
```

The `paged.doc` capability binds `pages` pages.

## Judging the result

`verify` counts the rendered page PNGs beside the asset, or falls back
to the PDF's own `pages=` marker, and compares against the record.

## Pitfalls

- A page PNG deleted from the asset directory fails the next check.
"""


# The paged runner's source, written once by the module fixture: run,
# verify, preview and doctor, SDK-only.
PAGED_RUNNER = '''"""The paged fixture package's runner: the document-kind stub.

`run` binds `doc.pdf`, a stub PDF whose last data line is its page
count; `verify` counts rendered page PNGs beside the asset (falling
back to the PDF's marker) and compares against the record; `preview`
renders one stub page PNG per page and the `pages` preview file;
`doctor` answers at both depths.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from facet_runner_sdk import Request, Runner

runner = Runner()


@runner.verb("run")
def run(req: Request) -> dict[str, Any]:
    pages = int(req.inputs.get("pages", 11))
    (req.out_dir / "doc.pdf").write_text(_pdf(pages), encoding="utf-8")
    return {
        "tool": {"name": "paged", "version": "0.1.0"},
        "assets": [
            {
                "file": "doc.pdf",
                "name": "book",
                "kind": "document",
                "mime": "application/pdf",
                "properties": {"page_count": pages},
                "params": dict(req.inputs),
                "seed": None,
                "inputs": [],
            }
        ],
    }


@runner.verb("verify")
def verify(req: Request) -> dict[str, Any]:
    entries = []
    for path in req.assets:
        fresh = _fresh_pages(path)
        recorded = _recorded_pages(path)
        entries.append(
            {
                "file": path.name,
                "passed": recorded is None or recorded == fresh,
                "metrics": {"page_count": fresh},
                "thresholds": {},
                "verifier": "paged-pages",
            }
        )
    return {"assets": entries}


@runner.verb("preview")
def preview(req: Request) -> dict[str, Any]:
    entries = []
    for path in req.assets:
        pages = _marker_pages(path)
        names = [f"page-{number:02d}.png" for number in range(1, pages + 1)]
        for name in names:
            (req.out_dir / name).write_bytes(b"\\x89PNG stub\\n")
        preview_file = f"{path.name}.preview.json"
        (req.out_dir / preview_file).write_text(
            json.dumps(
                {
                    "preview": 1,
                    "kind": "document",
                    "summary": f"a stub book of {pages} pages",
                    "page_count": pages,
                    "pages": names,
                }
            ),
            encoding="utf-8",
        )
        entries.append({"file": path.name, "preview": preview_file, "images": []})
    return {"assets": entries}


@runner.verb("doctor")
def doctor(req: Request) -> dict[str, Any]:
    checks = [
        {
            "name": "imports",
            "ok": True,
            "detail": f"{req.capability} paged runner imports ({req.depth})",
            "fix": "",
        }
    ]
    if req.depth == "full":
        (req.out_dir / "doctor-word.txt").write_text("paged\\n", encoding="utf-8")
        checks.append(
            {"name": "synthesis", "ok": True, "detail": "wrote one throwaway word", "fix": ""}
        )
    return {"checks": checks}


def _pdf(pages: int) -> str:
    """A stub PDF whose data is one line per page plus a `pages=` marker."""
    parts = ["%PDF-1.4\\n"]
    parts.extend(f"% page {number}\\n" for number in range(1, pages + 1))
    parts.append(f"pages={pages}\\n%%EOF\\n")
    return "".join(parts)


def _fresh_pages(path: Path) -> int:
    """The stub's page count: rendered page PNGs when there are any."""
    rendered = sorted(path.parent.glob("page-*.png"))
    if rendered:
        return len(rendered)
    return _marker_pages(path)


def _marker_pages(path: Path) -> int:
    """The page count the stub PDF self-reports on its `pages=` line."""
    match = re.search(rb"^pages=(\\d+)", path.read_bytes(), re.MULTILINE)
    return int(match.group(1)) if match else 0


def _recorded_pages(path: Path) -> int | None:
    """The `page_count` the record beside the asset recorded, if any."""
    try:
        text = Path(f"{path}.facet.json").read_text(encoding="utf-8")
    except OSError:
        return None
    loaded: Any = json.loads(text)
    verification = loaded.get("verification")
    metrics = verification.get("metrics") if isinstance(verification, dict) else {}
    value = metrics.get("page_count") if isinstance(metrics, dict) else None
    return int(value) if value is not None else None


runner.main()
'''


# --- the tests.


def _book(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    pages: int,
    *,
    preview: bool = False,
) -> Path:
    """One paged asset bound into ``tmp_path/out``, freshly previewed or not."""
    piece = tmp_path / "piece.toml"
    piece.write_text(f"[inputs]\npages = {pages}\n", encoding="utf-8")
    out = tmp_path / "out"
    code, _ = _argv(capsys, "run", "paged.doc", str(piece), "--out", str(out))
    assert code == 0, capsys.readouterr().err or tmp_path
    capsys.readouterr()
    if preview:
        assert _argv(capsys, "preview", str(out / "doc.pdf"))[0] == 0
    return out / "doc.pdf"


def test_pull_fetched_the_declared_browser(facet_home: Paged) -> None:
    """The package's ``[browser]`` revision came from the shared CDN endpoint."""
    assert REVISION_REQUEST in facet_home.browser.requests


def test_run_of_a_document_capability_builds_a_pdf_asset(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, facet_home: Paged
) -> None:
    """The record's asset is the document kind, mime and page property."""
    asset = _book(capsys, tmp_path, PAGE_COUNT)
    assert asset.name == "doc.pdf"
    record = json.loads((out_record(asset)).read_text(encoding="utf-8"))
    assert record["kind"] == "document"
    assert record["mime"] == "application/pdf"
    assert record["properties"]["page_count"] == PAGE_COUNT


def test_preview_json_carries_pages_and_truncates(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, facet_home: Paged
) -> None:
    """An 11-page book: 10 image paths, the full pages list, truncated true."""
    asset = _book(capsys, tmp_path, PAGE_COUNT)
    code, data = _argv(capsys, "preview", str(asset))
    assert code == 0
    assert len(data["images"]) == IMAGE_LIMIT
    preview = data["preview"]
    assert isinstance(preview, dict)
    assert preview["page_count"] == PAGE_COUNT
    assert len(preview["pages"]) == PAGE_COUNT
    assert preview["pages"][0] == "page-01.png"
    assert preview["truncated"] is True
    images = [Path(str(name)) for name in data["images"]]
    assert all(path.is_file() for path in images)


def test_preview_json_of_a_short_book_is_not_truncated(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, facet_home: Paged
) -> None:
    """Two pages fit inside the limit: no truncation flag, both images."""
    asset = _book(capsys, tmp_path, SHORT_PAGES)
    _code, data = _argv(capsys, "preview", str(asset))
    assert data["images"] != []
    preview = data["preview"]
    assert isinstance(preview, dict)
    assert len(preview["pages"]) == SHORT_PAGES
    assert preview["truncated"] is False


def test_person_preview_opens_the_pdf_itself(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    facet_home: Paged,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A document opens the asset's file URL; no viewer server starts."""
    opened: list[str] = []

    def _fail_serve(*args: object, **kwargs: object) -> Any:
        raise AssertionError("a document never starts a viewer server")

    monkeypatch.setattr(preview_module, "open_url", lambda url: opened.append(str(url)))
    monkeypatch.setattr(preview_module, "serve", _fail_serve)
    asset = _book(capsys, tmp_path, PAGE_COUNT, preview=True)
    assert main(["preview", str(asset)]) == 0
    assert len(opened) == 1
    assert opened[0].startswith("file://") and opened[0].endswith("/doc.pdf")


def test_check_fails_after_a_page_goes_missing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, facet_home: Paged
) -> None:
    """Verify counts the rendered pages; a missing page fails the verdict."""
    asset = _book(capsys, tmp_path, PAGE_COUNT, preview=True)
    (asset.parent / "page-11.png").unlink()
    code, data = _argv(capsys, "check", str(asset))
    assert code == EXIT_VERIFICATION_FAILED
    assert data["passed"] is False
    assert data["fresh"]["metrics"] == {"page_count": PAGE_COUNT - 1}
    assert data["recorded"]["metrics"] == {"page_count": PAGE_COUNT}


def test_check_passes_when_every_page_is_there(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, facet_home: Paged
) -> None:
    """The untouched, freshly-previewed book still re-verifies green."""
    asset = _book(capsys, tmp_path, PAGE_COUNT, preview=True)
    code, data = _argv(capsys, "check", str(asset))
    assert code == 0
    assert data["passed"] is True
    assert data["recorded"]["metrics"] == {"page_count": PAGE_COUNT}


def out_record(asset: Path) -> Path:
    """The record file a document asset carries beside it."""
    return Path(f"{asset}.facet.json")
