import os

import pytest

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
