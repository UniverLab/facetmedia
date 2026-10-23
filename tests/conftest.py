import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.registry_server import FakeGitHub

HEAVY_REASON = "needs network and real models; run with FACET_HEAVY=1"


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
