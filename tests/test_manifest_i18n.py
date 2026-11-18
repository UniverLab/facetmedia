"""Localized capability summaries and facet labels, as manifests read them."""

from __future__ import annotations

import shutil
from pathlib import Path

from facetmedia.registry import load_registry, validate_registry

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"

SPANISH_SUMMARY = """
[capabilities.text.i18n.es]
summary = "Repite el texto."
"""

SPANISH_LABEL = """version = 1

[facets.text]
label = "Text"

[facets.text.i18n.es]
label = "Texto"

[[packages]]
name = "echo"

[[packages]]
name = "termsy"

[[packages]]
name = "future"
"""


def _copied_registry(tmp_path: Path) -> Path:
    registry = tmp_path / "registry"
    shutil.copytree(FIXTURES, registry)
    return registry


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def test_capability_i18n_summary_is_read(tmp_path: Path) -> None:
    registry = _copied_registry(tmp_path)
    _append(registry / "packages" / "echo" / "package.toml", SPANISH_SUMMARY)
    (capability,) = load_registry(registry).package("echo").capabilities
    assert capability.summary == "Echo text back, repeated."
    assert capability.i18n == {"es": "Repite el texto."}


def test_capability_i18n_absent_is_empty(tmp_path: Path) -> None:
    registry = _copied_registry(tmp_path)
    (capability,) = load_registry(registry).package("echo").capabilities
    assert capability.i18n == {}


def test_facet_label_and_i18n_are_read(tmp_path: Path) -> None:
    registry = _copied_registry(tmp_path)
    (registry / "index.toml").write_text(SPANISH_LABEL, encoding="utf-8")
    loaded = load_registry(registry)
    assert loaded.facets["text"].label == "Text"
    assert loaded.facets["text"].i18n == {"es": "Texto"}


def test_facet_label_without_i18n_is_read(tmp_path: Path) -> None:
    registry = _copied_registry(tmp_path)
    index = SPANISH_LABEL.replace('\n[facets.text.i18n.es]\nlabel = "Texto"\n', "")
    (registry / "index.toml").write_text(index, encoding="utf-8")
    loaded = load_registry(registry)
    assert loaded.facets["text"].label == "Text"
    assert loaded.facets["text"].i18n == {}


def test_unknown_facet_label_and_i18n_are_ignored_on_user_load(ahead_registry: Path) -> None:
    registry = load_registry(ahead_registry)
    assert "hologram" not in registry.facets
    assert registry.skipped[0].name == "ahead"


def test_strict_validation_reports_unknown_facet_label(ahead_registry: Path) -> None:
    problems = validate_registry(ahead_registry)
    found = {(item["file"], item["key"], item["problem"]) for item in problems}
    assert ("index.toml", "facets.hologram", "unknown to facet 0.1.0b2") in found


def test_facets_absent_is_empty(tmp_path: Path) -> None:
    registry = _copied_registry(tmp_path)
    assert load_registry(registry).facets == {}
