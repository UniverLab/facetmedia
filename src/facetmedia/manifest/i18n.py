"""Read and validate localized texts: capability summaries and facet labels.

A capability may carry ``[capabilities.<cap>.i18n.<lang>]`` with a ``summary``,
and ``index.toml`` may carry ``[facets.<facet>]`` with a ``label`` and an
optional ``[facets.<facet>.i18n.<lang>]`` label.  The English text stays
required and is the default.  The split mirrors ``facetmedia.manifest.ffmpeg``:
the manifest module keeps the package grammar, this one keeps the localized
grammar, and the loader passed in collects every problem instead of stopping
at the first one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from facetmedia.registry_types import FACETS, FacetLabel

if TYPE_CHECKING:
    from facetmedia.manifest import RegistryLoader

FACET_KEYS: Final[frozenset[str]] = frozenset({"label", "i18n"})
LABEL_LIMIT: Final[int] = 40
SUMMARY_LIMIT: Final[int] = 200
_LANG_RE: Final[re.Pattern[str]] = re.compile(r"[a-z]{2,3}\Z")


def _read_text(
    loader: RegistryLoader,
    item: Mapping[str, Any],
    file: str,
    prefix: str,
    *,
    key: str,
    limit: int,
) -> str | None:
    """One required localized string, 1 to ``limit`` characters."""

    value = item.get(key)
    if value is None:
        loader.add(file, f"{prefix}{key}", "required")
        return None
    if not isinstance(value, str):
        loader.add(file, f"{prefix}{key}", "type")
        return None
    if not 1 <= len(value) <= limit:
        loader.add(file, f"{prefix}{key}", f"value: 1 to {limit} characters")
        return None
    return value


def read_i18n(
    loader: RegistryLoader,
    raw: object,
    file: str,
    prefix: str,
    *,
    key: str,
    limit: int,
) -> Mapping[str, str]:
    """An optional ``[i18n]`` table: one ``key`` string per language tag."""

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        loader.add(file, prefix.rstrip("."), "type")
        return {}
    found: dict[str, str] = {}
    for lang in sorted(raw):
        item_prefix = f"{prefix}{lang}."
        if not _LANG_RE.fullmatch(lang):
            loader.add(file, f"{prefix}{lang}", "value: a lowercase language tag")
            continue
        item = raw[lang]
        if not isinstance(item, dict):
            loader.add(file, item_prefix.rstrip("."), "type")
            continue
        loader.unknown(item, frozenset({key}), file, item_prefix)
        value = _read_text(loader, item, file, item_prefix, key=key, limit=limit)
        if value is not None:
            found[lang] = value
    return found


def read_facets(
    loader: RegistryLoader,
    index: Mapping[str, Any],
    file: str,
) -> Mapping[str, FacetLabel]:
    """The optional ``[facets.<facet>]`` tables of ``index.toml``."""

    raw = index.get("facets")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        loader.add(file, "facets", "type")
        return {}
    found: dict[str, FacetLabel] = {}
    for name in sorted(raw):
        item = raw[name]
        if name not in FACETS:
            loader.add(file, f"facets.{name}", f"value: one of {', '.join(FACETS)}")
            continue
        if not isinstance(item, dict):
            loader.add(file, f"facets.{name}", "type")
            continue
        prefix = f"facets.{name}."
        loader.unknown(item, FACET_KEYS, file, prefix)
        label = _read_text(loader, item, file, prefix, key="label", limit=LABEL_LIMIT)
        i18n = read_i18n(
            loader, item.get("i18n"), file, f"{prefix}i18n.", key="label", limit=LABEL_LIMIT
        )
        if label is None:
            continue
        found[name] = FacetLabel(label=label, i18n=i18n)
    return found
