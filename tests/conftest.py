import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.registry_server import FakeGitHub

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "registry"
HEAVY_REASON = "needs network and real models; run with FACET_HEAVY=1"


@pytest.fixture
def ahead_registry(tmp_path: Path) -> Path:
    """A registry with a future facet, label and otherwise unknown package."""
    root = tmp_path / "ahead-registry"
    shutil.copytree(FIXTURE_ROOT, root)
    index = root / "index.toml"
    with index.open("a", encoding="utf-8") as handle:
        handle.write(
            '\n[facets.hologram]\nlabel = "Hologram"\n'
            '[facets.hologram.i18n.es]\nlabel = "Holograma"\n'
            '\n[[packages]]\nname = "ahead"\n'
        )
    package = root / "packages" / "ahead"
    package.mkdir()
    (package / "package.toml").write_text(
        'name = "ahead"\nrequires_facet = ">=9"\nfacet = "hologram"\n'
        '[capabilities.future]\nkind = "hologram"\nunknown = true\n'
        "[future.section]\nunknown = true\n",
        encoding="utf-8",
    )
    return root


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", f"heavy: {HEAVY_REASON}")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.environ.get("FACET_HEAVY") == "1":
        return
    skip_heavy = pytest.mark.skip(reason=HEAVY_REASON)
    for item in items:
        if "heavy" in item.keywords:
            item.add_marker(skip_heavy)


@pytest.fixture(autouse=True)
def _facet_home_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACET_HOME", str(tmp_path / "facet-home"))
    # No test may reach the network by accident: the update check is off unless
    # a test deletes this variable to exercise it against a fake server.
    monkeypatch.setenv("FACET_NO_UPDATE_CHECK", "1")


@pytest.fixture
def fake_github(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHub]:
    """Serve both GitHub endpoints locally and point facet at them."""
    server = FakeGitHub().start()
    monkeypatch.setenv("FACET_GITHUB_API", server.base)
    monkeypatch.setenv("FACET_GITHUB_CODELOAD", server.base)
    monkeypatch.delenv("FACET_REGISTRY", raising=False)
    monkeypatch.delenv("FACET_REGISTRY_REF", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    try:
        yield server
    finally:
        server.stop()
